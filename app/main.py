import asyncio
import json
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jose import JWTError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import strategy_config as cfg
from app.auth import ensure_seed_admin, get_current_user, get_user_from_token
from app.config import get_cors_origins, get_database_url, get_seed_admin
from app.dashboard_state import build_dashboard_state, build_position_risk_tick
from app.db import SessionLocal, engine, get_db
from app.delta_product import fetch_product
from app.market_ws import DeltaMarketClient
from app.models import (
    Base,
    Candle,
    MarketSnapshot,
    OrderBookSnapshot,
    PaperOrder,
    Position,
    Signal,
    TradeEvent,
    User,
)
from app.routers import auth as auth_router
from app.signal_engine import (
    build_key_levels,
    generate_signal_for_symbol,
    latest_decision_summary,
    manage_open_positions,
    paper_account_summary,
    paper_trade_from_signal,
)
from app.timeutil import funding_countdown, iso_ist, ist_date_str, now_ist
from app.ws_hubs import candle_hub, dashboard_hub

app = FastAPI(title="AutoTrade BTCUSD Paper", version=cfg.STRATEGY_VERSION)
app.include_router(auth_router.router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins() + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if STATIC_DIR.exists():
    app.mount("/legacy-static", StaticFiles(directory=str(STATIC_DIR)), name="legacy-static")

market_client = DeltaMarketClient()


def _add_column_if_missing(conn, table: str, column: str, ddl: str, existing_tables: set) -> None:
    if table not in existing_tables:
        return
    columns = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def ensure_schema_compatibility() -> None:
    with engine.begin() as conn:
        if not str(engine.url).startswith("sqlite"):
            return
        table_infos = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in table_infos.fetchall()}
        for table, cols in {
            "paper_account": {"user_id": "INTEGER"},
            "positions": {"user_id": "INTEGER"},
            "paper_orders": {"user_id": "INTEGER"},
            "signals": {"user_id": "INTEGER"},
            "risk_events": {"user_id": "INTEGER"},
            "users": {},
        }.items():
            for col, ddl in cols.items():
                _add_column_if_missing(conn, table, col, ddl, existing_tables)


async def signal_loop() -> None:
    next_signal_check = 0.0
    while True:
        manage_open_positions(cfg.SYMBOL)
        now = time.monotonic()
        if now >= next_signal_check:
            # Market setup only — per-user open/cooldown gates applied at fill time
            signal = generate_signal_for_symbol(cfg.SYMBOL, market_only=True)
            if signal and signal.get("decision") in {"LONG", "SHORT"}:
                with SessionLocal() as session:
                    auto_users = session.query(User).filter(User.auto_trade.is_(True), User.is_active.is_(True)).all()
                    user_ids = [u.id for u in auto_users]
                for uid in user_ids:
                    paper_trade_from_signal(cfg.SYMBOL, signal, user_id=uid)
            next_signal_check = now + 8
        await asyncio.sleep(2)


async def dashboard_broadcast_loop() -> None:
    while True:
        try:
            manage_open_positions(cfg.SYMBOL)
            for user_id in list(dashboard_hub.connected_user_ids()):
                payload = await asyncio.to_thread(build_dashboard_state, True, user_id)
                await dashboard_hub.send_user(user_id, payload)
        except Exception as exc:  # pragma: no cover
            print(f"dashboard broadcast error: {exc}")
        await asyncio.sleep(1.0)


async def position_risk_broadcast_loop() -> None:
    """Faster WebSocket ticks for live mark / unrealized P&L / equity."""
    while True:
        try:
            user_ids = list(dashboard_hub.connected_user_ids())
            if user_ids:
                manage_open_positions(cfg.SYMBOL)
                for user_id in user_ids:
                    payload = await asyncio.to_thread(build_position_risk_tick, user_id)
                    await dashboard_hub.send_user(user_id, payload)
        except Exception as exc:  # pragma: no cover
            print(f"position risk broadcast error: {exc}")
        await asyncio.sleep(0.35)


@app.on_event("startup")
async def startup_event() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema_compatibility()
    email, password = get_seed_admin()
    with SessionLocal() as session:
        if email and password:
            ensure_seed_admin(session, email, password)
        # Paper engine: auto-fill qualified setups for all active accounts (Requ)
        session.query(User).filter(User.is_active.is_(True)).update(
            {User.auto_trade: True}, synchronize_session=False
        )
        session.commit()
    candle_hub.bind_loop(asyncio.get_running_loop())
    app.state.ws_task = asyncio.create_task(market_client.run())
    app.state.signal_task = asyncio.create_task(signal_loop())
    app.state.dashboard_task = asyncio.create_task(dashboard_broadcast_loop())
    app.state.position_task = asyncio.create_task(position_risk_broadcast_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    for name in ("ws_task", "signal_task", "dashboard_task", "position_task"):
        task = getattr(app.state, name, None)
        if task:
            task.cancel()


def _ws_token(websocket: WebSocket) -> Optional[str]:
    return websocket.query_params.get("token")


@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    token = _ws_token(websocket)
    user_id: Optional[int] = None
    try:
        if not token:
            await websocket.accept()
            first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            try:
                data = json.loads(first)
                if isinstance(data, dict) and data.get("type") == "auth":
                    token = data.get("token")
            except json.JSONDecodeError:
                token = first
            if not token:
                await websocket.close(code=4401)
                return
            with SessionLocal() as session:
                user = get_user_from_token(session, token)
                user_id = user.id
            # already accepted — register manually
            dashboard_hub.clients.setdefault(user_id, set()).add(websocket)
            payload = await asyncio.to_thread(build_dashboard_state, True, user_id)
            await websocket.send_text(json.dumps(payload, default=str))
        else:
            with SessionLocal() as session:
                user = get_user_from_token(session, token)
                user_id = user.id
            await dashboard_hub.connect(websocket, user_id)
            payload = await asyncio.to_thread(build_dashboard_state, True, user_id)
            await websocket.send_text(json.dumps(payload, default=str))

        assert user_id is not None
        while True:
            message = await websocket.receive_text()
            low = message.strip().lower()
            if low in {"ping", '"ping"'}:
                await websocket.send_text(json.dumps({"type": "pong", "ist_now": iso_ist(now_ist())}))
            elif low in {"refresh", '"refresh"'}:
                payload = await asyncio.to_thread(build_dashboard_state, True, user_id)
                await websocket.send_text(json.dumps(payload, default=str))
    except (WebSocketDisconnect, JWTError, HTTPException, asyncio.TimeoutError):
        if user_id is not None:
            dashboard_hub.disconnect(websocket, user_id)
        else:
            dashboard_hub.disconnect(websocket)
    except Exception:
        if user_id is not None:
            dashboard_hub.disconnect(websocket, user_id)


@app.websocket("/ws/candles")
async def candles_ws(
    websocket: WebSocket,
    token: str = Query(...),
    symbol: str = Query(cfg.SYMBOL),
    interval: str = Query("1m"),
    limit: int = Query(300),
) -> None:
    allowed = set(cfg.TIMEFRAMES) | {"1h", "60m"}
    if interval == "60m":
        interval = "1h"
    if interval not in allowed:
        interval = "1m"
    try:
        with SessionLocal() as session:
            get_user_from_token(session, token)
    except Exception:
        await websocket.close(code=4401)
        return

    await candle_hub.connect(websocket, symbol, interval)
    try:
        with SessionLocal() as session:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.interval == interval)
                .order_by(Candle.candle_time.desc())
                .limit(min(limit, 500))
                .all()
            )
        rows.reverse()
        history = []
        for c in rows:
            ts = int(c.candle_time.timestamp()) if c.candle_time else 0
            # candle_time stored as naive IST — convert carefully
            from app.timeutil import IST

            aware = c.candle_time.replace(tzinfo=IST) if c.candle_time.tzinfo is None else c.candle_time
            ts = int(aware.timestamp())
            history.append(
                {
                    "time": ts,
                    "open": float(c.open_price),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": float(c.volume),
                    "closed": bool(c.closed),
                }
            )
        await websocket.send_text(
            json.dumps(
                {"type": "history", "symbol": symbol, "interval": interval, "candles": history},
                default=str,
            )
        )
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() in {"ping", '"ping"'}:
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        candle_hub.disconnect(websocket, symbol, interval)
    except Exception:
        candle_hub.disconnect(websocket, symbol, interval)


@app.get("/api/health")
async def health() -> Dict[str, object]:
    return {
        "status": "ok",
        "strategy_version": cfg.STRATEGY_VERSION,
        "initial_balance": cfg.INITIAL_BALANCE,
        "leverage": cfg.LEVERAGE,
        "contract_value_btc": cfg.CONTRACT_VALUE_BTC,
        "tick_size": cfg.TICK_SIZE,
        "taker_fee_rate": cfg.TAKER_FEE_RATE,
        "risk_per_trade": cfg.RISK_PER_TRADE,
        "max_daily_loss": cfg.MAX_DAILY_LOSS,
        "timezone": cfg.TIMEZONE,
        "ist_now": iso_ist(now_ist()),
        "ist_date": ist_date_str(),
        "funding": funding_countdown(),
        "database": "postgres" if "postgresql" in get_database_url() else "sqlite",
        "ws_dashboard_users": len(dashboard_hub.clients),
        "timeframes": list(cfg.TIMEFRAMES),
    }


@app.get("/api/config")
async def public_config() -> Dict[str, object]:
    return {
        "symbol": cfg.SYMBOL,
        "leverage": cfg.LEVERAGE,
        "contract_value_btc": cfg.CONTRACT_VALUE_BTC,
        "tick_size": cfg.TICK_SIZE,
        "taker_fee_rate": cfg.TAKER_FEE_RATE,
        "risk_per_trade": cfg.RISK_PER_TRADE,
        "max_daily_loss": cfg.MAX_DAILY_LOSS,
        "initial_balance": cfg.INITIAL_BALANCE,
        "timezone": cfg.TIMEZONE,
        "timeframes": list(cfg.TIMEFRAMES),
        "strategy_version": cfg.STRATEGY_VERSION,
    }


@app.get("/api/dashboard")
async def dashboard_snapshot(user: User = Depends(get_current_user)) -> Dict[str, object]:
    return build_dashboard_state(manage=True, user_id=user.id)


@app.get("/api/product")
async def product_specs() -> Dict[str, object]:
    return fetch_product(force=False)


@app.get("/api/account")
async def account_summary(user: User = Depends(get_current_user)) -> Dict[str, object]:
    manage_open_positions(cfg.SYMBOL, user_id=user.id)
    return paper_account_summary(user_id=user.id)


@app.get("/api/decision")
async def decision_summary(user: User = Depends(get_current_user)) -> Dict[str, object]:
    return latest_decision_summary(cfg.SYMBOL, user_id=user.id)


@app.get("/api/levels")
async def key_levels() -> Dict[str, object]:
    return {"levels": build_key_levels(cfg.SYMBOL)}


@app.post("/api/generate-signal/{symbol}")
async def trigger_signal(symbol: str, user: User = Depends(get_current_user)):
    signal = generate_signal_for_symbol(symbol.upper())
    if not signal:
        raise HTTPException(status_code=404, detail="Unable to evaluate market")
    trade = None
    if signal.get("decision") in {"LONG", "SHORT"}:
        trade = paper_trade_from_signal(symbol.upper(), signal, user_id=user.id)
    return {"signal": signal, "paper_trade": trade}


@app.get("/api/candles/{interval}")
async def candles_by_interval(interval: str, limit: int = 300) -> Dict[str, object]:
    tf = "1h" if interval == "60m" else interval
    allowed = set(cfg.TIMEFRAMES) | {"1h"}
    if tf not in allowed:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(allowed)}")
    with SessionLocal() as session:
        candles = (
            session.query(Candle)
            .filter(Candle.symbol == cfg.SYMBOL, Candle.interval == tf)
            .order_by(Candle.candle_time.desc())
            .limit(min(limit, 500))
            .all()
        )
    from app.timeutil import IST

    out = []
    for c in candles[::-1]:
        aware = c.candle_time.replace(tzinfo=IST) if c.candle_time.tzinfo is None else c.candle_time
        out.append(
            {
                "time": int(aware.timestamp()),
                "timestamp": iso_ist(c.candle_time),
                "open": c.open_price,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "closed": c.closed,
            }
        )
    return {"interval": tf, "candles": out, "timezone": cfg.TIMEZONE}


@app.get("/api/positions")
async def positions(user: User = Depends(get_current_user)) -> Dict[str, object]:
    with SessionLocal() as session:
        rows = (
            session.query(Position)
            .filter(Position.user_id == user.id)
            .order_by(Position.updated_at.desc())
            .all()
        )
    return {
        "positions": [
            {
                "id": row.id,
                "symbol": row.symbol,
                "side": row.side,
                "size": row.size,
                "entry_price": row.entry_price,
                "mark_price": row.mark_price,
                "stop_price": row.stop_price,
                "target_price": row.target_price,
                "pnl": row.pnl,
                "realized_pnl": row.realized_pnl,
                "leverage": row.leverage,
                "margin": row.margin,
                "setup_name": row.setup_name,
                "is_open": row.is_open,
                "closed_at": iso_ist(row.closed_at),
                "close_reason": row.close_reason,
                "updated_at": iso_ist(row.updated_at),
                "opened_at": iso_ist(row.opened_at) if row.opened_at else None,
            }
            for row in rows
        ],
        "timezone": cfg.TIMEZONE,
    }


@app.get("/api/orderbook")
async def orderbook() -> Dict[str, object]:
    with SessionLocal() as session:
        row = session.query(OrderBookSnapshot).order_by(OrderBookSnapshot.updated_at.desc()).first()
    if row is None:
        return {"symbol": cfg.SYMBOL, "best_bid": None, "best_ask": None, "spread": None, "bids": [], "asks": []}
    return {
        "symbol": row.symbol,
        "best_bid": row.best_bid,
        "best_ask": row.best_ask,
        "spread": row.spread,
        "bids": row.bids,
        "asks": row.asks,
    }


@app.get("/api/heartbeat")
async def heartbeat() -> Dict[str, object]:
    return {
        "tick": int(now_ist().timestamp()),
        "ist_now": iso_ist(now_ist()),
        "timezone": cfg.TIMEZONE,
        "funding": funding_countdown(),
    }


@app.post("/api/reset-paper")
async def reset_paper(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Dict[str, object]:
    from app.paper_reset import reset_paper_account_for_user

    result = reset_paper_account_for_user(user.id)
    return {"status": "reset", **result, "account": paper_account_summary(user_id=user.id)}


# Serve React SPA if built
if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="spa-assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("ws/"):
            raise HTTPException(status_code=404)
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"detail": "Frontend not built"}, status_code=404)
else:

    @app.get("/")
    async def root_hint():
        return {
            "message": "API running. Build frontend or use Vite at :5173",
            "health": "/api/health",
            "auth": "/api/auth/login",
        }
