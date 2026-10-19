from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app import strategy_config as cfg
from app.decision_engine import evaluate_market
from app.db import SessionLocal
from app.models import (
    Candle,
    MarketFeature,
    MarketSnapshot,
    OrderBookSnapshot,
    PaperAccount,
    PaperOrder,
    Position,
    RiskEvent,
    Signal,
)
from app.risk import RiskState, apply_entry_slippage, apply_exit_slippage, check_hard_gates, position_size
from app.timeutil import ensure_ist, iso_ist, ist_date_str, now_ist, store_naive_ist
from app.zones import nearest_zone, price_in_zone

_LIVE_EVAL_CACHE: Dict[str, Any] = {"ts": 0.0, "symbol": None, "decision": None}


def _ensure_account(session, user_id: Optional[int] = None) -> PaperAccount:
    today = ist_date_str()  # IST calendar day
    if user_id is not None:
        account = session.query(PaperAccount).filter(PaperAccount.user_id == user_id).first()
    else:
        account = session.query(PaperAccount).order_by(PaperAccount.id.asc()).first()

    if account is None:
        account = PaperAccount(
            user_id=user_id,
            initial_balance=cfg.INITIAL_BALANCE,
            realized_pnl=0.0,
            daily_starting_equity=cfg.INITIAL_BALANCE,
            daily_realized_pnl=0.0,
            daily_reset_date=today,
            consecutive_losses=0,
        )
        session.add(account)
        session.flush()
        return account

    if account.daily_reset_date != today:
        equity, _, _ = _account_equity(session, account)
        account.daily_starting_equity = equity
        account.daily_realized_pnl = 0.0
        account.daily_reset_date = today
        session.add(account)
        session.flush()
    return account


def _account_equity(session, account: PaperAccount) -> tuple[float, float, float]:
    q = session.query(Position).filter(Position.is_open.is_(True))
    if account.user_id is not None:
        q = q.filter(Position.user_id == account.user_id)
    positions = q.all()
    unrealized = sum(position.pnl + position.entry_fee for position in positions)
    used_margin = sum(position.margin for position in positions)
    equity = account.initial_balance + account.realized_pnl + unrealized
    return equity, unrealized, used_margin


def _candle_rows(session, symbol: str, interval: str, limit: int = 200) -> List[dict]:
    candles = (
        session.query(Candle)
        .filter(Candle.symbol == symbol, Candle.interval == interval)
        .order_by(Candle.candle_time.desc())
        .limit(limit)
        .all()
    )
    candles.reverse()
    return [
        {
            "open": float(c.open_price),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(c.volume),
            "closed": bool(getattr(c, "closed", True)),
            "time": c.candle_time,
        }
        for c in candles
    ]


def _spread_bps(snapshot: Optional[MarketSnapshot], book: Optional[OrderBookSnapshot]) -> Optional[float]:
    bid = None
    ask = None
    mid = None
    if book and book.best_bid and book.best_ask:
        bid, ask = float(book.best_bid), float(book.best_ask)
    elif snapshot and snapshot.bid and snapshot.ask:
        bid, ask = float(snapshot.bid), float(snapshot.ask)
    if bid and ask and ask > bid:
        mid = (bid + ask) / 2.0
        return ((ask - bid) / mid) * 10_000.0
    return None


def _feed_healthy(snapshot: Optional[MarketSnapshot]) -> bool:
    if snapshot is None or snapshot.updated_at is None:
        return False
    # App timestamps are stored as naive IST wall clock
    updated = snapshot.updated_at
    if updated.tzinfo is not None:
        updated = updated.astimezone(now_ist().tzinfo).replace(tzinfo=None)
    age = (store_naive_ist() - updated).total_seconds()
    return 0 <= age <= cfg.FEED_STALE_SECONDS


def _record_risk_event(session, symbol: str, event_type: str, detail: str = "") -> None:
    session.add(RiskEvent(symbol=symbol, event_type=event_type, detail=detail))


def _parse_book_levels(raw: Optional[str]) -> List[List[float]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    levels = []
    for level in data[:10]:
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            levels.append([float(level[0]), float(level[1])])
    return levels


def generate_signal_for_symbol(
    symbol: str,
    user_id: Optional[int] = None,
    *,
    market_only: bool = False,
) -> Optional[dict]:
    """
    Evaluate BTCUSD setups.

    market_only=True (auto-trade loop): ignore per-account gates (open position,
    loss cooldown, daily loss). Those are applied per user at fill time.
    Otherwise gates use the given user_id account (or the first account).
    """
    with SessionLocal() as session:
        account = _ensure_account(session, user_id=user_id)
        equity, _, used_margin = _account_equity(session, account)
        snapshot = (
            session.query(MarketSnapshot)
            .filter(MarketSnapshot.symbol == symbol)
            .order_by(MarketSnapshot.updated_at.desc())
            .first()
        )
        book = (
            session.query(OrderBookSnapshot)
            .filter(OrderBookSnapshot.symbol == symbol)
            .order_by(OrderBookSnapshot.updated_at.desc())
            .first()
        )
        open_q = session.query(Position).filter(Position.symbol == symbol, Position.is_open.is_(True))
        if user_id is not None:
            open_q = open_q.filter(Position.user_id == user_id)
        open_pos = None if market_only else open_q.first()
        candles_by_tf = {
            tf: _candle_rows(session, symbol, tf)
            for tf in cfg.TIMEFRAMES
        }
        spread = _spread_bps(snapshot, book)
        if market_only:
            # Shared market decision — account risk applied in paper_trade_from_signal
            risk_state = RiskState(
                equity=float(cfg.INITIAL_BALANCE),
                daily_starting_equity=float(cfg.INITIAL_BALANCE),
                daily_realized_loss=0.0,
                consecutive_losses=0,
                position_open=False,
                last_loss_at=None,
                feed_is_healthy=_feed_healthy(snapshot),
                candles_are_complete=True,
                spread_bps=spread,
                used_margin=0.0,
            )
        else:
            daily_loss = max(0.0, -float(account.daily_realized_pnl or 0.0))
            risk_state = RiskState(
                equity=equity,
                daily_starting_equity=float(account.daily_starting_equity or account.initial_balance),
                daily_realized_loss=daily_loss,
                consecutive_losses=int(account.consecutive_losses or 0),
                position_open=open_pos is not None,
                last_loss_at=account.last_loss_at,
                feed_is_healthy=_feed_healthy(snapshot),
                candles_are_complete=True,
                spread_bps=spread,
                used_margin=used_margin,
            )
        decision = evaluate_market(
            candles_by_tf=candles_by_tf,
            risk_state=risk_state,
            oi_change=float(snapshot.oi_change_6h) if snapshot and snapshot.oi_change_6h is not None else None,
            funding_rate=float(snapshot.funding_rate) if snapshot and snapshot.funding_rate is not None else None,
            last_trade_side=snapshot.last_trade_side if snapshot else None,
            order_book_bids=_parse_book_levels(book.bids if book else None),
            order_book_asks=_parse_book_levels(book.asks if book else None),
        )

        regime = decision.get("regime") or {}
        setup = decision.get("setup") or {}
        score = float(decision.get("score") or setup.get("score") or 0.0)
        side = str(decision.get("decision") or "WAIT")
        price = float(
            decision.get("entry")
            or (snapshot.mark_price if snapshot else 0.0)
            or (snapshot.price if snapshot else 0.0)
            or 0.0
        )
        new_signal = Signal(
            symbol=symbol,
            side=side,
            confidence=round(score / 100.0, 4) if score else 0.0,
            reason=str(decision.get("reason") or ""),
            rejection_reason=decision.get("rejection_reason"),
            setup_name=decision.get("setup_name") or setup.get("setup"),
            regime_state=regime.get("state"),
            regime_score=regime.get("score"),
            price=price,
            stop=decision.get("stop") or setup.get("stop"),
            target=decision.get("target") or setup.get("target"),
            atr=regime.get("atr_5m"),
            volume_ratio=regime.get("volume_ratio"),
            strategy_version=cfg.STRATEGY_VERSION,
            created_at=store_naive_ist(),
        )
        session.add(new_signal)
        session.add(
            MarketFeature(
                symbol=symbol,
                timestamp=store_naive_ist(),
                regime_state=regime.get("state"),
                regime_score=regime.get("score"),
                atr=regime.get("atr_5m"),
                vwap=regime.get("vwap"),
                oi_change=float(snapshot.oi_change_6h) if snapshot and snapshot.oi_change_6h is not None else None,
                funding_rate=float(snapshot.funding_rate) if snapshot and snapshot.funding_rate is not None else None,
                decision=side,
                setup_score=score or None,
            )
        )
        if side == "WAIT" and decision.get("rejection_reason"):
            _record_risk_event(session, symbol, str(decision["rejection_reason"]), str(decision.get("reason") or ""))
        session.commit()
        session.refresh(new_signal)
        _LIVE_EVAL_CACHE.update({"ts": time.monotonic(), "symbol": symbol, "decision": decision})

        payload = {
            "id": new_signal.id,
            "symbol": symbol,
            "side": side,
            "decision": side,
            "confidence": new_signal.confidence,
            "score": score,
            "live_score_kind": decision.get("live_score_kind"),
            "score_breakdown": decision.get("score_breakdown"),
            "reason": new_signal.reason,
            "rejection_reason": new_signal.rejection_reason,
            "setup_name": new_signal.setup_name,
            "regime": regime,
            "setup": setup,
            "sizing": decision.get("sizing"),
            "price": price,
            "entry": decision.get("entry"),
            "stop": decision.get("stop"),
            "target": decision.get("target"),
            "zones": decision.get("zones") or [],
            "strategy_version": cfg.STRATEGY_VERSION,
            "atr": regime.get("atr_5m"),
        }
        return payload


def _run_evaluate(symbol: str, user_id: Optional[int] = None) -> Dict[str, object]:
    """Fresh evaluate_market against current DB market state (no Signal write)."""
    with SessionLocal() as session:
        account = _ensure_account(session, user_id=user_id)
        equity, _, used_margin = _account_equity(session, account)
        snapshot = (
            session.query(MarketSnapshot)
            .filter(MarketSnapshot.symbol == symbol)
            .order_by(MarketSnapshot.updated_at.desc())
            .first()
        )
        book = (
            session.query(OrderBookSnapshot)
            .filter(OrderBookSnapshot.symbol == symbol)
            .order_by(OrderBookSnapshot.updated_at.desc())
            .first()
        )
        open_q = session.query(Position).filter(Position.symbol == symbol, Position.is_open.is_(True))
        if user_id is not None:
            open_q = open_q.filter(Position.user_id == user_id)
        open_pos = open_q.first()
        candles_by_tf = {tf: _candle_rows(session, symbol, tf) for tf in cfg.TIMEFRAMES}
        spread = _spread_bps(snapshot, book)
        daily_loss = max(0.0, -float(account.daily_realized_pnl or 0.0))
        risk_state = RiskState(
            equity=equity,
            daily_starting_equity=float(account.daily_starting_equity or account.initial_balance),
            daily_realized_loss=daily_loss,
            consecutive_losses=int(account.consecutive_losses or 0),
            position_open=open_pos is not None,
            last_loss_at=account.last_loss_at,
            feed_is_healthy=_feed_healthy(snapshot),
            candles_are_complete=True,
            spread_bps=spread,
            used_margin=used_margin,
        )
        decision = evaluate_market(
            candles_by_tf=candles_by_tf,
            risk_state=risk_state,
            oi_change=float(snapshot.oi_change_6h) if snapshot and snapshot.oi_change_6h is not None else None,
            funding_rate=float(snapshot.funding_rate) if snapshot and snapshot.funding_rate is not None else None,
            last_trade_side=snapshot.last_trade_side if snapshot else None,
            order_book_bids=_parse_book_levels(book.bids if book else None),
            order_book_asks=_parse_book_levels(book.asks if book else None),
        )
        decision["_spread_bps"] = spread
        decision["_funding_rate"] = (
            float(snapshot.funding_rate) if snapshot and snapshot.funding_rate is not None else None
        )
        decision["_mark"] = float(
            (snapshot.mark_price if snapshot else 0.0) or (snapshot.price if snapshot else 0.0) or 0.0
        )
        return decision


def evaluate_live_decision(
    symbol: str = cfg.SYMBOL,
    max_age_sec: float = 2.0,
    user_id: Optional[int] = None,
) -> Dict[str, object]:
    """Cached live evaluation for dashboard WS (~1s pushes)."""
    now = time.monotonic()
    cache_key = f"{symbol}:{user_id}"
    if (
        _LIVE_EVAL_CACHE.get("decision")
        and _LIVE_EVAL_CACHE.get("key") == cache_key
        and now - float(_LIVE_EVAL_CACHE.get("ts") or 0) < max_age_sec
    ):
        return dict(_LIVE_EVAL_CACHE["decision"])
    decision = _run_evaluate(symbol, user_id=user_id)
    _LIVE_EVAL_CACHE.update({"ts": now, "key": cache_key, "symbol": symbol, "decision": decision})
    return decision


def _signed_pnl(position: Position, exit_price: float) -> float:
    direction = 1.0 if position.side in {"BUY", "LONG"} else -1.0
    return direction * (exit_price - position.entry_price) * position.size * position.contract_value


def _close_position(
    session,
    account: PaperAccount,
    position: Position,
    exit_price: float,
    reason: str,
) -> PaperOrder:
    fill = apply_exit_slippage(position.side, exit_price)
    gross_pnl = _signed_pnl(position, fill)
    exit_notional = position.size * position.contract_value * fill
    exit_fee = exit_notional * cfg.TAKER_FEE_RATE
    net_trade_pnl = gross_pnl - position.entry_fee - exit_fee
    account.realized_pnl += gross_pnl - exit_fee
    account.daily_realized_pnl = float(account.daily_realized_pnl or 0.0) + (gross_pnl - exit_fee)
    account.updated_at = store_naive_ist()

    if net_trade_pnl < 0:
        account.consecutive_losses = int(account.consecutive_losses or 0) + 1
        account.last_loss_at = store_naive_ist()
    else:
        account.consecutive_losses = 0

    position.mark_price = fill
    position.pnl = net_trade_pnl
    position.realized_pnl = net_trade_pnl
    position.margin = 0.0
    position.is_open = False
    position.closed_at = store_naive_ist()
    position.close_reason = reason
    position.updated_at = position.closed_at

    exit_order = PaperOrder(
        symbol=position.symbol,
        side="SELL" if position.side in {"BUY", "LONG"} else "BUY",
        size=position.size,
        price=fill,
        intended_price=exit_price,
        fee=exit_fee,
        slippage=abs(fill - exit_price),
        status="filled",
        order_type=reason,
        created_at=position.closed_at,
    )
    session.add(exit_order)
    session.add(account)
    session.add(position)
    return exit_order


def paper_trade_from_signal(
    symbol: str, signal_payload: dict, user_id: Optional[int] = None
) -> Optional[dict]:
    decision = str(signal_payload.get("decision") or signal_payload.get("side") or "WAIT")
    if decision not in {"LONG", "SHORT", "BUY", "SELL"}:
        return None
    if decision == "BUY":
        decision = "LONG"
    if decision == "SELL":
        decision = "SHORT"

    entry_mid = float(signal_payload.get("entry") or signal_payload.get("price") or 0.0)
    stop = float(signal_payload.get("stop") or 0.0)
    target = float(signal_payload.get("target") or 0.0)
    if entry_mid <= 0 or stop <= 0 or target <= 0:
        return None

    with SessionLocal() as session:
        account = _ensure_account(session, user_id=user_id)
        uid = account.user_id
        equity, _, used_margin = _account_equity(session, account)
        snapshot = (
            session.query(MarketSnapshot)
            .filter(MarketSnapshot.symbol == symbol)
            .order_by(MarketSnapshot.updated_at.desc())
            .first()
        )
        book = (
            session.query(OrderBookSnapshot)
            .filter(OrderBookSnapshot.symbol == symbol)
            .order_by(OrderBookSnapshot.updated_at.desc())
            .first()
        )
        if snapshot is None or not _feed_healthy(snapshot):
            _record_risk_event(session, symbol, "STALE_OR_DISCONNECTED_FEED", "blocked entry")
            session.commit()
            return None

        open_q = session.query(Position).filter(Position.symbol == symbol, Position.is_open.is_(True))
        if uid is not None:
            open_q = open_q.filter(Position.user_id == uid)
        open_pos = open_q.first()
        spread = _spread_bps(snapshot, book)
        daily_loss = max(0.0, -float(account.daily_realized_pnl or 0.0))
        gate = check_hard_gates(
            RiskState(
                equity=equity,
                daily_starting_equity=float(account.daily_starting_equity or account.initial_balance),
                daily_realized_loss=daily_loss,
                consecutive_losses=int(account.consecutive_losses or 0),
                position_open=open_pos is not None,
                last_loss_at=account.last_loss_at,
                feed_is_healthy=True,
                candles_are_complete=True,
                spread_bps=spread,
                used_margin=used_margin,
            )
        )
        if gate:
            _record_risk_event(
                session,
                symbol,
                str(gate.get("rejection_reason") or gate.get("reason") or "BLOCKED"),
                f"user={uid}",
            )
            session.commit()
            return None

        # Re-size with this user's equity (market signal may use placeholder sizing)
        sizing = position_size(
            equity=equity,
            entry=entry_mid,
            stop=stop,
            target=target,
            used_margin=used_margin,
        ) or signal_payload.get("sizing")
        if not sizing:
            _record_risk_event(session, symbol, "RISK_OR_RR_CHECK_FAILED", f"user={uid}")
            session.commit()
            return None

        mid = float(snapshot.mark_price or snapshot.price or entry_mid)
        fill = apply_entry_slippage(decision, mid)
        now = store_naive_ist()
        contracts = int(sizing["contracts"])
        contract_value = float(sizing.get("contract_value") or cfg.CONTRACT_VALUE_BTC)
        # Configured futures leverage is authoritative for paper fills
        leverage = float(cfg.LEVERAGE)
        notional = contracts * contract_value * fill
        entry_fee = notional * cfg.TAKER_FEE_RATE
        margin = notional / max(leverage, 1.0)
        equity, _, used_margin = _account_equity(session, account)
        if margin + entry_fee > max(0.0, equity - used_margin):
            _record_risk_event(session, symbol, "INSUFFICIENT_MARGIN", f"need {margin + entry_fee:.2f}")
            session.commit()
            return None

        account.realized_pnl -= entry_fee
        account.daily_realized_pnl = float(account.daily_realized_pnl or 0.0) - entry_fee
        account.updated_at = now

        initial_risk = abs(fill - stop) * contracts * contract_value
        position = Position(
            user_id=uid,
            symbol=symbol,
            side=decision,
            size=float(contracts),
            entry_price=fill,
            mark_price=fill,
            stop_price=stop,
            target_price=target,
            pnl=-entry_fee - entry_fee,
            realized_pnl=0.0,
            leverage=leverage,
            margin=margin,
            confidence=float(signal_payload.get("confidence") or 0.0),
            contract_value=contract_value,
            trailing_stop=stop,
            trail_distance=abs(fill - stop),
            max_favorable_price=fill,
            max_adverse_price=fill,
            initial_risk=initial_risk,
            entry_fee=entry_fee,
            setup_name=signal_payload.get("setup_name"),
            strategy_version=cfg.STRATEGY_VERSION,
            opened_at=now,
            is_open=True,
            updated_at=now,
        )
        order = PaperOrder(
            user_id=uid,
            symbol=symbol,
            side="BUY" if decision == "LONG" else "SELL",
            signal_id=int(signal_payload.get("id") or 0),
            size=float(contracts),
            price=fill,
            intended_price=entry_mid,
            fee=entry_fee,
            slippage=abs(fill - mid),
            status="filled",
            order_type="entry",
            created_at=now,
        )
        session.add_all([position, order, account])
        session.commit()
        session.refresh(order)
        session.refresh(position)

    return {
        "order_id": order.id,
        "symbol": symbol,
        "side": decision,
        "contracts": contracts,
        "lot_size": contracts,
        "contract_value_btc": contract_value,
        "quantity_btc": contracts * contract_value,
        "notional": notional,
        "margin": margin,
        "leverage": leverage,
        "entry_fee": entry_fee,
        "price": fill,
        "stop": stop,
        "target": target,
        "lot_explanation": sizing.get("lot_explanation"),
        "status": "filled",
        "strategy_version": cfg.STRATEGY_VERSION,
    }


def manage_open_positions(symbol: Optional[str] = None, user_id: Optional[int] = None) -> None:
    with SessionLocal() as session:
        query = session.query(Position).filter(Position.is_open.is_(True))
        if symbol:
            query = query.filter(Position.symbol == symbol)
        if user_id is not None:
            query = query.filter(Position.user_id == user_id)
        positions = query.all()
        if not positions:
            return

        now = store_naive_ist()
        now_aware = now_ist()
        # Group by user so each account is updated correctly
        by_user: Dict[Optional[int], list] = {}
        for position in positions:
            by_user.setdefault(position.user_id, []).append(position)

        for uid, user_positions in by_user.items():
            account = _ensure_account(session, user_id=uid)
            for position in user_positions:
                snapshot = (
                    session.query(MarketSnapshot)
                    .filter(MarketSnapshot.symbol == position.symbol)
                    .order_by(MarketSnapshot.updated_at.desc())
                    .first()
                )
                if snapshot is None:
                    continue
                if not _feed_healthy(snapshot):
                    _record_risk_event(session, position.symbol, "STALE_FEED_WHILE_OPEN", "monitoring")
                    continue

                mark_price = float(snapshot.mark_price or snapshot.price or 0.0)
                if mark_price <= 0:
                    continue

                position.mark_price = mark_price
                gross_unrealized = _signed_pnl(position, mark_price)
                estimated_exit_fee = position.size * position.contract_value * mark_price * cfg.TAKER_FEE_RATE
                position.pnl = gross_unrealized - position.entry_fee - estimated_exit_fee

                if position.side in {"BUY", "LONG"}:
                    position.max_favorable_price = max(position.max_favorable_price or position.entry_price, mark_price)
                    position.max_adverse_price = min(position.max_adverse_price or position.entry_price, mark_price)
                else:
                    position.max_favorable_price = min(position.max_favorable_price or position.entry_price, mark_price)
                    position.max_adverse_price = max(position.max_adverse_price or position.entry_price, mark_price)

                r_distance = abs(
                    position.entry_price - float(position.stop_price or position.trailing_stop or position.entry_price)
                )
                if r_distance > 0:
                    favorable = abs((position.max_favorable_price or position.entry_price) - position.entry_price)
                    if favorable >= cfg.TRAIL_AFTER_R * r_distance:
                        if position.side in {"BUY", "LONG"}:
                            be = position.entry_price
                            trail = (position.max_favorable_price or mark_price) - r_distance * 0.5
                            position.trailing_stop = max(position.trailing_stop or be, be, trail)
                        else:
                            be = position.entry_price
                            trail = (position.max_favorable_price or mark_price) + r_distance * 0.5
                            position.trailing_stop = min(position.trailing_stop or be, be, trail)

                stop = position.trailing_stop if position.trailing_stop is not None else position.stop_price
                target = position.target_price
                stop_hit = stop is not None and (
                    mark_price <= float(stop) if position.side in {"BUY", "LONG"} else mark_price >= float(stop)
                )
                target_hit = target is not None and (
                    mark_price >= float(target) if position.side in {"BUY", "LONG"} else mark_price <= float(target)
                )

                opened = ensure_ist(position.opened_at or position.updated_at)
                time_stop = opened is not None and (now_aware - opened).total_seconds() >= cfg.TIME_STOP_MINUTES * 60
                progress_ok = r_distance > 0 and abs(mark_price - position.entry_price) >= 0.5 * r_distance
                if time_stop and not progress_ok:
                    _close_position(session, account, position, mark_price, "time_stop_exit")
                elif stop_hit and target_hit:
                    _close_position(session, account, position, float(stop), "ambiguous_stop_priority_exit")
                elif stop_hit:
                    _close_position(session, account, position, mark_price, "stop_exit")
                elif target_hit:
                    _close_position(session, account, position, mark_price, "target_exit")
                else:
                    position.updated_at = now
                    session.add(position)

            account.updated_at = now
            session.add(account)
        session.commit()


def paper_account_summary(user_id: Optional[int] = None) -> dict:
    with SessionLocal() as session:
        account = _ensure_account(session, user_id=user_id)
        equity, unrealized, used_margin = _account_equity(session, account)
        session.commit()
        return {
            "user_id": account.user_id,
            "initial_balance": account.initial_balance,
            "realized_pnl": account.realized_pnl,
            "unrealized_pnl": unrealized,
            "equity": equity,
            "used_margin": used_margin,
            "available_balance": max(0.0, equity - used_margin),
            "daily_starting_equity": account.daily_starting_equity,
            "daily_realized_pnl": account.daily_realized_pnl,
            "consecutive_losses": account.consecutive_losses,
            "max_leverage": cfg.LEVERAGE,
            "risk_per_trade": cfg.RISK_PER_TRADE,
            "strategy_version": cfg.STRATEGY_VERSION,
            "timezone": cfg.TIMEZONE,
            "ist_date": ist_date_str(),
            "mode": "paper",
            "automatic_trading": True,
            "contract_value_btc": cfg.CONTRACT_VALUE_BTC,
            "taker_fee_rate": cfg.TAKER_FEE_RATE,
        }


def _live_checklist(decision: Dict[str, object]) -> list:
    """Checklist from live evaluate_market output (Requ gate conditions)."""
    regime = decision.get("regime") or {}
    setup = decision.get("setup") or {}
    reject = str(decision.get("rejection_reason") or "").upper()
    side = str(decision.get("decision") or decision.get("side") or "WAIT").upper()
    score = float(decision.get("score") or setup.get("score") or 0.0)
    spread = decision.get("_spread_bps")
    funding = decision.get("_funding_rate")
    mark = float(decision.get("_mark") or decision.get("entry") or setup.get("entry") or 0.0)
    atr = float(regime.get("atr_5m") or 0.0) or 1.0
    zones = decision.get("zones") or []
    vol_ratio = float(regime.get("volume_ratio") or 0.0)

    regime_state = str(regime.get("state") or "")
    regime_ok = bool(regime_state) and regime_state != "UNSTABLE" and "VOLATILITY" not in reject

    near = decision.get("nearest_zone") or (nearest_zone(zones, mark) if zones and mark else None)
    dist_atr = decision.get("dist_atr")
    if dist_atr is None and near is not None and mark:
        dist_atr = abs(float(near["mid"]) - mark) / atr
    in_zone = bool(near) and mark and price_in_zone(mark, near)  # type: ignore[arg-type]
    level_ok = bool(setup) or in_zone or (dist_atr is not None and float(dist_atr) <= 1.0)

    volume_ok = vol_ratio >= 1.05 or bool(setup.get("confirm_3m"))

    rr = float(setup.get("reward_risk") or 0.0)
    if not rr and setup.get("entry") and setup.get("stop") and setup.get("target"):
        risk = abs(float(setup["entry"]) - float(setup["stop"]))
        reward = abs(float(setup["target"]) - float(setup["entry"]))
        rr = (reward / risk) if risk > 0 else 0.0
    rr_ok = (side in {"LONG", "SHORT"} and "RISK_OR_RR" not in reject) or rr >= cfg.MIN_REWARD_RISK

    funding_ok = funding is None or abs(float(funding)) <= 0.05
    liquidity_ok = (
        "SPREAD_TOO_WIDE" not in reject
        and "INSUFFICIENT_MARGIN" not in reject
        and (spread is None or float(spread) <= cfg.MAX_SPREAD_BPS)
    )

    return [
        {"label": "Regime aligned", "ok": regime_ok},
        {"label": "Near key level", "ok": level_ok},
        {"label": "Volume confirm", "ok": volume_ok},
        {"label": "Risk:Reward OK", "ok": rr_ok},
        {"label": "Funding neutral", "ok": funding_ok},
        {"label": "Liquidity OK", "ok": liquidity_ok},
    ]


def build_key_levels(symbol: str = cfg.SYMBOL) -> list:
    from app.zones import build_support_resistance_zones

    with SessionLocal() as session:
        c5 = _candle_rows(session, symbol, "5m", 80)
        c15 = _candle_rows(session, symbol, "15m", 60)
        book = (
            session.query(OrderBookSnapshot)
            .filter(OrderBookSnapshot.symbol == symbol)
            .order_by(OrderBookSnapshot.updated_at.desc())
            .first()
        )
    bids = _parse_book_levels(book.bids if book else None)
    asks = _parse_book_levels(book.asks if book else None)
    zones = build_support_resistance_zones(c15, c5, bids, asks)
    price = float(c5[-1]["close"]) if c5 else 0.0
    labeled = []
    resistances = sorted(
        [z for z in zones if z.get("side") in {"resistance", "both"} and float(z["mid"]) >= price],
        key=lambda z: float(z["mid"]),
    )[:3]
    supports = sorted(
        [z for z in zones if z.get("side") in {"support", "both"} and float(z["mid"]) <= price],
        key=lambda z: float(z["mid"]),
        reverse=True,
    )[:3]
    for idx, zone in enumerate(reversed(resistances), start=1):
        labeled.append(
            {
                "label": f"R{len(resistances) - idx + 1}",
                "side": "resistance",
                "price": float(zone["mid"]),
                "strength": float(zone["strength"]),
                "zone_type": zone.get("zone_type"),
            }
        )
    for idx, zone in enumerate(supports, start=1):
        labeled.append(
            {
                "label": f"S{idx}",
                "side": "support",
                "price": float(zone["mid"]),
                "strength": float(zone["strength"]),
                "zone_type": zone.get("zone_type"),
            }
        )
    return labeled


def latest_decision_summary(symbol: str = cfg.SYMBOL, user_id: Optional[int] = None) -> dict:
    live = evaluate_live_decision(symbol, user_id=user_id)
    regime = live.get("regime") or {}
    setup = live.get("setup") or {}
    side = str(live.get("decision") or live.get("side") or "WAIT")
    score = float(live.get("score") or setup.get("score") or 0.0)

    with SessionLocal() as session:
        feature = (
            session.query(MarketFeature)
            .filter(MarketFeature.symbol == symbol)
            .order_by(MarketFeature.timestamp.desc())
            .first()
        )
        events = (
            session.query(RiskEvent)
            .filter(RiskEvent.symbol == symbol)
            .order_by(RiskEvent.created_at.desc())
            .limit(10)
            .all()
        )
        closed_q = session.query(Position).filter(Position.symbol == symbol, Position.is_open.is_(False))
        if user_id is not None:
            closed_q = closed_q.filter(Position.user_id == user_id)
        closed = closed_q.order_by(Position.closed_at.desc()).limit(20).all()
        wins = sum(1 for p in closed if (p.realized_pnl or 0) > 0)
        win_rate = (wins / len(closed) * 100.0) if closed else 0.0

    checklist = _live_checklist(live)
    return {
        "signal": {
            "side": side,
            "score": score,
            "live_score_kind": live.get("live_score_kind"),
            "score_breakdown": live.get("score_breakdown"),
            "reason": live.get("reason"),
            "rejection_reason": live.get("rejection_reason"),
            "setup_name": live.get("setup_name") or setup.get("setup"),
            "regime_state": regime.get("state"),
            "regime_score": regime.get("score"),
            "price": live.get("_mark") or live.get("entry") or setup.get("entry"),
            "stop": live.get("stop") or setup.get("stop"),
            "target": live.get("target") or setup.get("target"),
            "entry": live.get("entry") or setup.get("entry"),
            "reward_risk": setup.get("reward_risk"),
            "dist_atr": live.get("dist_atr"),
            "updated_at": iso_ist(now_ist()),
            "live": True,
        },
        "feature": None
        if feature is None
        else {
            "regime_state": feature.regime_state,
            "regime_score": feature.regime_score,
            "atr": feature.atr,
            "vwap": feature.vwap,
            "oi_change": feature.oi_change,
            "funding_rate": feature.funding_rate,
            "decision": feature.decision,
            "setup_score": feature.setup_score,
        },
        "checklist": checklist,
        "levels": build_key_levels(symbol),
        "stats": {
            "total_closed_trades": len(closed),
            "win_rate": round(win_rate, 1),
        },
        "timezone": cfg.TIMEZONE,
        "risk_events": [
            {"type": e.event_type, "detail": e.detail, "created_at": iso_ist(e.created_at)}
            for e in events
        ],
        "live": True,
    }
