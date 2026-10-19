"""Live Delta Exchange product specs for BTCUSD perpetual futures."""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Dict, Optional

from app import strategy_config as cfg

REST_BASE = "https://api.india.delta.exchange"
_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "product": None}
_CACHE_TTL_SECONDS = 300.0


def _rest_get(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover
        print(f"Delta product fetch failed: {exc}")
        return None


def fetch_product(symbol: str = cfg.SYMBOL, force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if (
        not force
        and _CACHE["product"] is not None
        and now - float(_CACHE["fetched_at"]) < _CACHE_TTL_SECONDS
    ):
        return dict(_CACHE["product"])

    payload = _rest_get(f"{REST_BASE}/v2/products/{symbol}")
    result = (payload or {}).get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        # Fall back to configured defaults from last known Delta India BTCUSD specs
        product = {
            "symbol": symbol,
            "contract_type": "perpetual_futures",
            "contract_value": cfg.CONTRACT_VALUE_BTC,
            "tick_size": cfg.TICK_SIZE,
            "taker_commission_rate": cfg.TAKER_FEE_RATE,
            "maker_commission_rate": 0.0002,
            "default_leverage": 200.0,
            "max_leverage": 200.0,
            "initial_margin_pct": 0.5,
            "position_size_limit": 125000,
            "notional_type": "vanilla",
            "settling_asset": "USD",
            "source": "fallback_config",
        }
        _CACHE["product"] = product
        _CACHE["fetched_at"] = now
        return dict(product)

    ui = result.get("ui_config") or {}
    leverage_values = ui.get("leverage_slider_values") or []
    max_leverage = float(max(leverage_values)) if leverage_values else float(result.get("default_leverage") or 200)

    product = {
        "symbol": result.get("symbol") or symbol,
        "product_id": result.get("id"),
        "contract_type": result.get("contract_type") or "perpetual_futures",
        "contract_value": float(result.get("contract_value") or cfg.CONTRACT_VALUE_BTC),
        "tick_size": float(result.get("tick_size") or cfg.TICK_SIZE),
        "taker_commission_rate": float(result.get("taker_commission_rate") or cfg.TAKER_FEE_RATE),
        "maker_commission_rate": float(result.get("maker_commission_rate") or 0.0002),
        "default_leverage": float(result.get("default_leverage") or 200),
        "max_leverage": max_leverage,
        "leverage_slider_values": leverage_values,
        "initial_margin_pct": float(result.get("initial_margin") or 0.5),
        "maintenance_margin_pct": float(result.get("maintenance_margin") or 0.25),
        "position_size_limit": int(result.get("position_size_limit") or 125000),
        "notional_type": result.get("notional_type") or "vanilla",
        "settling_asset": (result.get("settling_asset") or {}).get("symbol") or "USD",
        "trading_status": result.get("trading_status"),
        "source": "delta_rest",
    }
    _CACHE["product"] = product
    _CACHE["fetched_at"] = now

    # Keep runtime config in sync with exchange metadata
    cfg.CONTRACT_VALUE_BTC = float(product["contract_value"])
    cfg.TICK_SIZE = float(product["tick_size"])
    cfg.TAKER_FEE_RATE = float(product["taker_commission_rate"])
    return dict(product)


def describe_lot(entry_price: float, contracts: int, leverage: float, product: Optional[dict] = None) -> dict:
    product = product or fetch_product()
    cv = float(product["contract_value"])
    qty_btc = contracts * cv
    notional = qty_btc * entry_price
    margin = notional / max(leverage, 1.0)
    return {
        "lot_size_contracts": contracts,
        "contract_value_btc": cv,
        "quantity_btc": qty_btc,
        "notional_usd": round(notional, 4),
        "leverage": leverage,
        "isolated_margin_usd": round(margin, 4),
        "margin_per_contract_usd": round((cv * entry_price) / max(leverage, 1.0), 6),
        "pnl_per_1_usd_move": round(qty_btc, 6),
        "explanation": (
            f"{contracts} lot(s) × {cv} BTC/contract = {qty_btc:.4f} BTC; "
            f"notional ${notional:.2f}; margin at {leverage:.0f}× ≈ ${margin:.2f}"
        ),
    }
