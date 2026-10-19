"""Reset paper trading state (IST calendar day)."""

from __future__ import annotations

from typing import Optional

from app import strategy_config as cfg
from app.db import SessionLocal
from app.models import MarketFeature, PaperAccount, PaperOrder, Position, RiskEvent, Signal
from app.timeutil import ist_date_str, store_naive_ist


def reset_paper_account_for_user(user_id: int) -> dict:
    with SessionLocal() as session:
        deleted = {
            "positions": session.query(Position).filter(Position.user_id == user_id).delete(),
            "paper_orders": session.query(PaperOrder).filter(PaperOrder.user_id == user_id).delete(),
            "risk_events": session.query(RiskEvent).filter(RiskEvent.user_id == user_id).delete(),
        }
        account = session.query(PaperAccount).filter(PaperAccount.user_id == user_id).first()
        now = store_naive_ist()
        if account is None:
            account = PaperAccount(user_id=user_id)
            session.add(account)
        account.initial_balance = cfg.INITIAL_BALANCE
        account.realized_pnl = 0.0
        account.daily_starting_equity = cfg.INITIAL_BALANCE
        account.daily_realized_pnl = 0.0
        account.consecutive_losses = 0
        account.last_loss_at = None
        account.daily_reset_date = ist_date_str()
        account.updated_at = now
        session.commit()
        return {
            "deleted": deleted,
            "user_id": user_id,
            "initial_balance": cfg.INITIAL_BALANCE,
            "ist_date": account.daily_reset_date,
            "timezone": cfg.TIMEZONE,
        }


def reset_paper_account(user_id: Optional[int] = None) -> dict:
    if user_id is not None:
        return reset_paper_account_for_user(user_id)
    with SessionLocal() as session:
        deleted = {
            "positions": session.query(Position).delete(),
            "paper_orders": session.query(PaperOrder).delete(),
            "signals": session.query(Signal).delete(),
            "risk_events": session.query(RiskEvent).delete(),
            "market_features": session.query(MarketFeature).delete(),
        }
        for account in session.query(PaperAccount).all():
            account.initial_balance = cfg.INITIAL_BALANCE
            account.realized_pnl = 0.0
            account.daily_starting_equity = cfg.INITIAL_BALANCE
            account.daily_realized_pnl = 0.0
            account.consecutive_losses = 0
            account.last_loss_at = None
            account.daily_reset_date = ist_date_str()
            account.updated_at = store_naive_ist()
        session.commit()
        return {
            "deleted": deleted,
            "initial_balance": cfg.INITIAL_BALANCE,
            "ist_date": ist_date_str(),
            "timezone": cfg.TIMEZONE,
        }
