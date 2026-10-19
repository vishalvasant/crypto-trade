"""Hard risk gates and Delta futures lot sizing (100× isolated margin)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app import strategy_config as cfg
from app.delta_product import describe_lot, fetch_product
from app.timeutil import ensure_ist, now_ist


@dataclass
class RiskState:
    equity: float
    daily_starting_equity: float
    daily_realized_loss: float
    consecutive_losses: int
    position_open: bool
    last_loss_at: Optional[datetime]
    feed_is_healthy: bool
    candles_are_complete: bool
    spread_bps: Optional[float]
    used_margin: float = 0.0


def wait(reason: str) -> dict:
    return {
        "decision": "WAIT",
        "side": "WAIT",
        "reason": reason,
        "rejection_reason": reason,
    }


def check_hard_gates(state: RiskState) -> Optional[dict]:
    if not state.feed_is_healthy:
        return wait("STALE_OR_DISCONNECTED_FEED")
    if not state.candles_are_complete:
        return wait("INCOMPLETE_CANDLE_DATA")
    if state.spread_bps is not None and state.spread_bps > cfg.MAX_SPREAD_BPS:
        return wait("SPREAD_TOO_WIDE")
    if state.daily_starting_equity > 0:
        loss_frac = state.daily_realized_loss / state.daily_starting_equity
        if loss_frac >= cfg.MAX_DAILY_LOSS:
            return wait("DAILY_LOSS_LIMIT")
    if state.consecutive_losses >= cfg.MAX_CONSECUTIVE_LOSSES:
        return wait("LOSS_STREAK_PAUSE")
    if state.last_loss_at is not None:
        last = ensure_ist(state.last_loss_at)
        if last is not None:
            elapsed = (now_ist() - last).total_seconds()
            if elapsed < cfg.LOSS_COOLDOWN_SECONDS:
                return wait("LOSS_COOLDOWN")
    if state.position_open:
        return wait("POSITION_ALREADY_OPEN")
    return None


def estimate_costs(entry: float, quantity_btc: float, fee_rate: Optional[float] = None) -> float:
    """Round-trip fee + slippage estimate in quote currency."""
    fee = fee_rate if fee_rate is not None else cfg.TAKER_FEE_RATE
    notional = entry * quantity_btc
    fees = notional * fee * 2
    slippage = notional * (cfg.SLIPPAGE_BPS / 10_000.0) * 2
    return fees + slippage


def position_size(
    equity: float,
    entry: float,
    stop: float,
    target: float,
    used_margin: float = 0.0,
) -> Optional[dict]:
    """
    Size BTCUSD perpetual lots using Delta contract metadata.

    Lot formula (vanilla linear):
      notional_usd = contracts * contract_value_btc * entry
      isolated_margin = notional_usd / leverage
      stop_risk_usd = |entry - stop| * contracts * contract_value_btc

    Contracts are the minimum of:
      1) risk budget / stop risk per contract (after fees)
      2) margin budget at configured leverage
      3) exchange position_size_limit
    """
    product = fetch_product()
    contract_value = float(product["contract_value"])
    fee_rate = float(product["taker_commission_rate"])
    leverage = float(cfg.LEVERAGE)
    max_contracts = int(product.get("position_size_limit") or 125000)

    risk_budget = equity * cfg.RISK_PER_TRADE
    stop_distance = abs(entry - stop)
    target_distance = abs(target - entry)
    if stop_distance <= 0 or entry <= 0 or equity <= 0 or target_distance <= 0:
        return None

    gross_rr = target_distance / stop_distance
    if gross_rr < cfg.MIN_REWARD_RISK:
        return None

    # Risk-based qty including round-trip cost drag
    cost_per_btc = entry * (2 * fee_rate + 2 * cfg.SLIPPAGE_BPS / 10_000.0)
    denom = stop_distance + cost_per_btc
    qty_btc_risk = risk_budget / denom if denom > 0 else 0.0
    contracts_by_risk = int(qty_btc_risk / contract_value)

    # Margin-based cap at configured leverage
    available = max(0.0, equity - used_margin)
    margin_budget = min(available, equity * cfg.MAX_MARGIN_FRACTION)
    notional_per_contract = contract_value * entry
    margin_per_contract = notional_per_contract / max(leverage, 1.0)
    # Leave room for entry taker fee on the fill
    fee_per_contract = notional_per_contract * fee_rate
    affordable_margin = margin_budget / max(margin_per_contract + fee_per_contract, 1e-9)
    contracts_by_margin = int(affordable_margin)

    contracts = min(contracts_by_risk, contracts_by_margin, max_contracts)
    if contracts < 1:
        return None

    # Shrink until risk + margin both fit
    while contracts >= 1:
        qty_btc = contracts * contract_value
        costs = estimate_costs(entry, qty_btc, fee_rate)
        monetary_risk = stop_distance * qty_btc + costs
        notional = entry * qty_btc
        margin = notional / max(leverage, 1.0)
        entry_fee = notional * fee_rate
        if monetary_risk <= risk_budget * 1.01 and margin + entry_fee <= margin_budget * 1.01:
            break
        contracts -= 1
    else:
        return None

    qty_btc = contracts * contract_value
    costs = estimate_costs(entry, qty_btc, fee_rate)
    monetary_risk = stop_distance * qty_btc + costs
    notional = entry * qty_btc
    margin = notional / max(leverage, 1.0)
    entry_fee = notional * fee_rate
    slip = notional * (cfg.SLIPPAGE_BPS / 10_000.0)
    reward_net = target_distance * qty_btc - costs
    if reward_net <= 0:
        return None
    rr_net = reward_net / monetary_risk if monetary_risk > 0 else 0.0
    lot = describe_lot(entry, contracts, leverage, product)

    return {
        "contracts": contracts,
        "lot_size": contracts,
        "quantity_btc": qty_btc,
        "risk_budget": risk_budget,
        "monetary_risk": monetary_risk,
        "reward_risk_gross": round(gross_rr, 3),
        "reward_risk_net": round(rr_net, 3),
        "notional_usd": round(notional, 4),
        "margin": margin,
        "leverage": leverage,
        "entry_fee": entry_fee,
        "entry_slippage": slip,
        "costs_estimate": costs,
        "contract_value": contract_value,
        "contracts_by_risk": contracts_by_risk,
        "contracts_by_margin": contracts_by_margin,
        "margin_per_contract": round(margin_per_contract, 6),
        "product": {
            "symbol": product.get("symbol"),
            "contract_type": product.get("contract_type"),
            "notional_type": product.get("notional_type"),
            "tick_size": product.get("tick_size"),
            "max_leverage": product.get("max_leverage"),
            "source": product.get("source"),
        },
        "lot_explanation": lot["explanation"],
    }


def apply_entry_slippage(side: str, mid_price: float) -> float:
    slip = mid_price * (cfg.SLIPPAGE_BPS / 10_000.0)
    half_spread = mid_price * (cfg.SLIPPAGE_BPS / 10_000.0)
    offset = slip + half_spread
    if side in {"LONG", "BUY"}:
        return mid_price + offset
    return mid_price - offset


def apply_exit_slippage(side: str, mid_price: float) -> float:
    """side here is the position side (LONG/BUY exits sell into bid)."""
    slip = mid_price * (cfg.SLIPPAGE_BPS / 10_000.0)
    if side in {"LONG", "BUY"}:
        return mid_price - slip
    return mid_price + slip
