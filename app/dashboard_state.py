"""Aggregated dashboard snapshot for REST + WebSocket live push."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app import strategy_config as cfg
from app.db import SessionLocal
from app.models import Candle, MarketSnapshot, OrderBookSnapshot, Position, Signal
from app.signal_engine import latest_decision_summary, manage_open_positions, paper_account_summary
from app.timeutil import funding_countdown, iso_ist, ist_date_str, now_ist


def _parse_book(raw: Optional[str]) -> List[List[float]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out = []
    for level in data[:12]:
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            out.append([float(level[0]), float(level[1])])
    return out


def build_dashboard_state(manage: bool = True, user_id: Optional[int] = None) -> Dict[str, Any]:
    if manage:
        manage_open_positions(cfg.SYMBOL, user_id=user_id)

    account = paper_account_summary(user_id=user_id)
    decision = latest_decision_summary(cfg.SYMBOL, user_id=user_id)
    funding = funding_countdown()

    with SessionLocal() as session:
        snapshot = (
            session.query(MarketSnapshot)
            .filter(MarketSnapshot.symbol == cfg.SYMBOL)
            .order_by(MarketSnapshot.updated_at.desc())
            .first()
        )
        book = (
            session.query(OrderBookSnapshot)
            .filter(OrderBookSnapshot.symbol == cfg.SYMBOL)
            .order_by(OrderBookSnapshot.updated_at.desc())
            .first()
        )
        pos_q = session.query(Position).filter(Position.symbol == cfg.SYMBOL, Position.is_open.is_(True))
        if user_id is not None:
            pos_q = pos_q.filter(Position.user_id == user_id)
        open_position = pos_q.order_by(Position.updated_at.desc()).first()

        hist_q = session.query(Position).filter(Position.symbol == cfg.SYMBOL)
        if user_id is not None:
            hist_q = hist_q.filter(Position.user_id == user_id)
        positions = hist_q.order_by(Position.updated_at.desc()).limit(40).all()

        last_signal = (
            session.query(Signal)
            .filter(Signal.symbol == cfg.SYMBOL)
            .order_by(Signal.created_at.desc())
            .first()
        )
        day_candles = (
            session.query(Candle)
            .filter(Candle.symbol == cfg.SYMBOL, Candle.interval == "5m")
            .order_by(Candle.candle_time.desc())
            .limit(288)
            .all()
        )

    price = float(snapshot.price or 0.0) if snapshot else 0.0
    mark = float(snapshot.mark_price or price) if snapshot else 0.0
    high_24h = max((c.high for c in day_candles), default=price)
    low_24h = min((c.low for c in day_candles), default=price)

    bids = _parse_book(book.bids if book else None)
    asks = _parse_book(book.asks if book else None)
    bid_sz = sum(l[1] for l in bids[:5])
    ask_sz = sum(l[1] for l in asks[:5])
    tot = bid_sz + ask_sz
    imbalance = ((bid_sz - ask_sz) / tot * 100.0) if tot > 0 else 0.0

    symbol = {
        "symbol": cfg.SYMBOL,
        "price": price,
        "bid": float(snapshot.bid or 0.0) if snapshot else None,
        "ask": float(snapshot.ask or 0.0) if snapshot else None,
        "mark_price": mark,
        "high_24h": high_24h,
        "low_24h": low_24h,
        "oi": float(snapshot.oi or 0.0) if snapshot else 0.0,
        "oi_change_6h": float(snapshot.oi_change_6h or 0.0) if snapshot else 0.0,
        "funding_rate": float(snapshot.funding_rate) if snapshot and snapshot.funding_rate is not None else None,
        "volume": float(snapshot.volume or 0.0) if snapshot else 0.0,
        "last_trade_side": snapshot.last_trade_side if snapshot else None,
        "updated_at": iso_ist(snapshot.updated_at) if snapshot else None,
        "funding": funding,
        "position": None
        if open_position is None
        else {
            "side": open_position.side,
            "size": open_position.size,
            "entry_price": open_position.entry_price,
            "mark_price": open_position.mark_price,
            "stop_price": open_position.stop_price,
            "target_price": open_position.target_price,
            "pnl": open_position.pnl,
            "leverage": open_position.leverage,
            "margin": open_position.margin,
            "trailing_stop": open_position.trailing_stop,
            "setup_name": open_position.setup_name,
            "entry_fee": open_position.entry_fee,
            "contract_value": open_position.contract_value or cfg.CONTRACT_VALUE_BTC,
            "is_open": open_position.is_open,
            "opened_at": iso_ist(open_position.opened_at),
            "updated_at": iso_ist(open_position.updated_at),
        },
        "last_signal": None
        if last_signal is None
        else {
            "side": last_signal.side,
            "score": (last_signal.confidence or 0) * 100,
            "reason": last_signal.reason,
            "rejection_reason": last_signal.rejection_reason,
            "setup_name": last_signal.setup_name,
            "regime_state": last_signal.regime_state,
            "regime_score": last_signal.regime_score,
            "created_at": iso_ist(last_signal.created_at),
        },
    }

    return {
        "type": "dashboard",
        "timezone": cfg.TIMEZONE,
        "ist_now": iso_ist(now_ist()),
        "ist_date": ist_date_str(),
        "server_ts": now_ist().timestamp(),
        "symbol": symbol,
        "account": account,
        "decision": decision,
        "orderbook": {
            "symbol": cfg.SYMBOL,
            "best_bid": book.best_bid if book else None,
            "best_ask": book.best_ask if book else None,
            "spread": book.spread if book else None,
            "bids": bids,
            "asks": asks,
            "imbalance_pct": round(imbalance, 2),
            "updated_at": iso_ist(book.updated_at) if book else None,
        },
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
                "opened_at": iso_ist(row.opened_at) if row.opened_at else None,
                "close_reason": row.close_reason,
                "updated_at": iso_ist(row.updated_at),
            }
            for row in positions
        ],
        "funding": funding,
        "config": {
            "symbol": cfg.SYMBOL,
            "leverage": cfg.LEVERAGE,
            "contract_value_btc": cfg.CONTRACT_VALUE_BTC,
            "tick_size": cfg.TICK_SIZE,
            "taker_fee_rate": cfg.TAKER_FEE_RATE,
            "risk_per_trade": cfg.RISK_PER_TRADE,
            "max_daily_loss": cfg.MAX_DAILY_LOSS,
            "initial_balance": cfg.INITIAL_BALANCE,
            "timezone": cfg.TIMEZONE,
        },
        "live": True,
    }


def build_position_risk_tick(user_id: Optional[int] = None) -> Dict[str, Any]:
    """Lightweight WS payload for real-time mark / P&L / equity (Position & Risk)."""
    manage_open_positions(cfg.SYMBOL, user_id=user_id)
    account = paper_account_summary(user_id=user_id)
    funding = funding_countdown()

    with SessionLocal() as session:
        snapshot = (
            session.query(MarketSnapshot)
            .filter(MarketSnapshot.symbol == cfg.SYMBOL)
            .order_by(MarketSnapshot.updated_at.desc())
            .first()
        )
        pos_q = session.query(Position).filter(Position.symbol == cfg.SYMBOL, Position.is_open.is_(True))
        if user_id is not None:
            pos_q = pos_q.filter(Position.user_id == user_id)
        open_position = pos_q.order_by(Position.updated_at.desc()).first()

    price = float(snapshot.price or 0.0) if snapshot else 0.0
    mark = float(snapshot.mark_price or price) if snapshot else 0.0

    position = None
    if open_position is not None:
        position = {
            "side": open_position.side,
            "size": open_position.size,
            "entry_price": open_position.entry_price,
            "mark_price": open_position.mark_price or mark,
            "stop_price": open_position.stop_price,
            "target_price": open_position.target_price,
            "pnl": open_position.pnl,
            "leverage": open_position.leverage,
            "margin": open_position.margin,
            "trailing_stop": open_position.trailing_stop,
            "setup_name": open_position.setup_name,
            "entry_fee": open_position.entry_fee,
            "contract_value": open_position.contract_value or cfg.CONTRACT_VALUE_BTC,
            "is_open": open_position.is_open,
            "opened_at": iso_ist(open_position.opened_at),
            "updated_at": iso_ist(open_position.updated_at),
        }

    return {
        "type": "position_risk",
        "ist_now": iso_ist(now_ist()),
        "server_ts": now_ist().timestamp(),
        "live": True,
        "symbol": {
            "symbol": cfg.SYMBOL,
            "price": price,
            "mark_price": mark,
            "bid": float(snapshot.bid or 0.0) if snapshot else None,
            "ask": float(snapshot.ask or 0.0) if snapshot else None,
            "updated_at": iso_ist(snapshot.updated_at) if snapshot else None,
            "funding": funding,
            "position": position,
        },
        "account": account,
        "funding": funding,
    }
