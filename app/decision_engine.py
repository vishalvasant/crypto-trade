"""Core decision pipeline: gates → regime → zones → setups → LONG/SHORT/WAIT."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from app import strategy_config as cfg
from app.regime import candles_closed_enough, classify_regime
from app.risk import RiskState, check_hard_gates, position_size, wait
from app.setups import find_best_setup
from app.zones import build_support_resistance_zones, nearest_zone


def _soft_readiness_score(
    regime: Dict[str, object],
    zones: Sequence[Dict[str, object]],
    price: float,
    spread_bps: Optional[float],
    funding_rate: Optional[float],
) -> Dict[str, object]:
    """0–100 readiness when no full setup yet — drives live dashboard score."""
    atr = float(regime.get("atr_5m") or 0.0) or max(price * 0.001, cfg.TICK_SIZE * 10)
    regime_abs = abs(float(regime.get("score") or 0.0))
    regime_pts = min(25, int(regime_abs * 0.25))

    near = nearest_zone(zones, price)
    zone_pts = 0
    dist_atr = None
    if near is not None:
        dist_atr = abs(float(near["mid"]) - price) / atr
        if dist_atr <= 0.35:
            zone_pts = 20
        elif dist_atr <= 0.8:
            zone_pts = 14
        elif dist_atr <= 1.5:
            zone_pts = 8
        elif dist_atr <= 2.5:
            zone_pts = 4

    vol_ratio = float(regime.get("volume_ratio") or 0.0)
    vol_pts = 0
    if vol_ratio >= 1.4:
        vol_pts = 15
    elif vol_ratio >= 1.1:
        vol_pts = 10
    elif vol_ratio >= 0.9:
        vol_pts = 5

    flow_pts = 0
    comps = regime.get("components") or {}
    flow = abs(float(comps.get("flow") or 0.0))
    if flow >= 40:
        flow_pts = 10
    elif flow >= 20:
        flow_pts = 6
    elif flow >= 10:
        flow_pts = 3

    exec_pts = 0
    if spread_bps is not None:
        if spread_bps <= 2:
            exec_pts = 10
        elif spread_bps <= cfg.MAX_SPREAD_BPS:
            exec_pts = 6
        elif spread_bps <= cfg.MAX_SPREAD_BPS * 1.5:
            exec_pts = 2

    funding_pts = 5
    if funding_rate is not None and abs(float(funding_rate)) > 0.05:
        funding_pts = 0
    elif funding_rate is not None and abs(float(funding_rate)) > 0.02:
        funding_pts = 2

    # Momentum alignment (3m/1m) up to 15
    tf3 = abs(float(comps.get("tf_3m") or 0.0))
    tf1 = abs(float(comps.get("tf_1m") or 0.0))
    mom_pts = min(15, int((tf3 + tf1) / 12))

    total = min(100, regime_pts + zone_pts + vol_pts + flow_pts + exec_pts + funding_pts + mom_pts)
    return {
        "score": total,
        "score_breakdown": {
            "regime": regime_pts,
            "zone": zone_pts,
            "volume": vol_pts,
            "flow": flow_pts,
            "execution": exec_pts,
            "funding": funding_pts,
            "momentum": mom_pts,
        },
        "nearest_zone": near,
        "dist_atr": round(dist_atr, 3) if dist_atr is not None else None,
    }


def _attach_readiness(
    payload: Dict[str, object],
    regime: Optional[Dict[str, object]],
    zones: Sequence[Dict[str, object]],
    candles_1m: Sequence[dict],
    spread_bps: Optional[float],
    funding_rate: Optional[float],
) -> Dict[str, object]:
    setup = payload.get("setup")
    if isinstance(setup, dict) and setup.get("score") is not None:
        payload["score"] = int(setup["score"])
        payload["score_breakdown"] = setup.get("score_breakdown")
        payload["setup_name"] = setup.get("setup") or payload.get("setup_name")
        payload["live_score_kind"] = "setup"
        return payload
    if not regime:
        payload["score"] = int(payload.get("score") or 0)
        payload["live_score_kind"] = "none"
        return payload
    price = float(candles_1m[-1]["close"]) if candles_1m else 0.0
    soft = _soft_readiness_score(regime, zones, price, spread_bps, funding_rate)
    payload["score"] = int(soft["score"])
    payload["score_breakdown"] = soft["score_breakdown"]
    payload["nearest_zone"] = soft.get("nearest_zone")
    payload["dist_atr"] = soft.get("dist_atr")
    payload["live_score_kind"] = "readiness"
    return payload


def evaluate_market(
    candles_by_tf: Dict[str, List[dict]],
    risk_state: RiskState,
    oi_change: Optional[float] = None,
    funding_rate: Optional[float] = None,
    last_trade_side: Optional[str] = None,
    order_book_bids: Optional[Sequence[Sequence[float]]] = None,
    order_book_asks: Optional[Sequence[Sequence[float]]] = None,
) -> Dict[str, object]:
    # 1. Hard safety gates
    gate = check_hard_gates(risk_state)
    if gate:
        return {
            **gate,
            "regime": None,
            "setup": None,
            "zones": [],
            "score": 0,
            "live_score_kind": "none",
            "strategy_version": cfg.STRATEGY_VERSION,
        }

    candles_1m = [c for c in (candles_by_tf.get("1m") or []) if c.get("closed", True)]
    candles_3m = [c for c in (candles_by_tf.get("3m") or []) if c.get("closed", True)]
    candles_5m = [c for c in (candles_by_tf.get("5m") or []) if c.get("closed", True)]
    candles_15m = [c for c in (candles_by_tf.get("15m") or []) if c.get("closed", True)]

    if not candles_closed_enough(
        {"1m": candles_1m, "3m": candles_3m, "5m": candles_5m, "15m": candles_15m}
    ):
        return {
            **wait("INCOMPLETE_CANDLE_DATA"),
            "regime": None,
            "setup": None,
            "zones": [],
            "score": 0,
            "live_score_kind": "none",
            "strategy_version": cfg.STRATEGY_VERSION,
        }

    # 2. Regime
    regime = classify_regime(
        candles_15m=candles_15m,
        candles_5m=candles_5m,
        candles_3m=candles_3m,
        candles_1m=candles_1m,
        oi_change=oi_change,
        last_trade_side=last_trade_side,
    )
    if regime["state"] == "UNSTABLE":
        return _attach_readiness(
            {
                **wait("VOLATILITY_OR_STRUCTURE_UNSTABLE"),
                "regime": regime,
                "setup": None,
                "zones": [],
                "strategy_version": cfg.STRATEGY_VERSION,
            },
            regime,
            [],
            candles_1m,
            risk_state.spread_bps,
            funding_rate,
        )

    # 3. Zones
    zones = build_support_resistance_zones(
        candles_15m=candles_15m,
        candles_5m=candles_5m,
        order_book_bids=order_book_bids,
        order_book_asks=order_book_asks,
    )

    # 4. Setups
    setup = find_best_setup(
        candles_3m=candles_3m,
        candles_1m=candles_1m,
        zones=zones,
        regime=regime,
        spread_bps=risk_state.spread_bps,
        oi_change=oi_change,
        funding_rate=funding_rate,
    )
    if not setup:
        return _attach_readiness(
            {
                **wait("NO_VALID_SETUP"),
                "regime": regime,
                "setup": None,
                "zones": zones[:12],
                "strategy_version": cfg.STRATEGY_VERSION,
            },
            regime,
            zones,
            candles_1m,
            risk_state.spread_bps,
            funding_rate,
        )

    score = int(setup["score"])
    if score < cfg.MIN_SETUP_SCORE_WATCH:
        return _attach_readiness(
            {
                **wait("SETUP_SCORE_BELOW_60"),
                "regime": regime,
                "setup": setup,
                "zones": zones[:12],
                "strategy_version": cfg.STRATEGY_VERSION,
            },
            regime,
            zones,
            candles_1m,
            risk_state.spread_bps,
            funding_rate,
        )
    if score < cfg.MIN_SETUP_SCORE_ENTRY:
        return _attach_readiness(
            {
                "decision": "WAIT",
                "side": "WAIT",
                "reason": "WATCHLIST_SCORE_60_74",
                "rejection_reason": "WATCHLIST_SCORE_60_74",
                "regime": regime,
                "setup": setup,
                "zones": zones[:12],
                "strategy_version": cfg.STRATEGY_VERSION,
                "entry": setup.get("entry"),
                "stop": setup.get("stop"),
                "target": setup.get("target"),
                "setup_name": setup.get("setup"),
            },
            regime,
            zones,
            candles_1m,
            risk_state.spread_bps,
            funding_rate,
        )

    sizing = position_size(
        equity=risk_state.equity,
        entry=float(setup["entry"]),
        stop=float(setup["stop"]),
        target=float(setup["target"]),
        used_margin=float(getattr(risk_state, "used_margin", 0.0) or 0.0),
    )
    if sizing is None:
        return _attach_readiness(
            {
                **wait("RISK_OR_RR_CHECK_FAILED"),
                "regime": regime,
                "setup": setup,
                "zones": zones[:12],
                "strategy_version": cfg.STRATEGY_VERSION,
                "entry": setup.get("entry"),
                "stop": setup.get("stop"),
                "target": setup.get("target"),
                "setup_name": setup.get("setup"),
            },
            regime,
            zones,
            candles_1m,
            risk_state.spread_bps,
            funding_rate,
        )

    decision = "LONG" if setup["side"] == "LONG" else "SHORT"
    return _attach_readiness(
        {
            "decision": decision,
            "side": decision,
            "reason": "; ".join(setup.get("reasons") or []),
            "rejection_reason": None,
            "regime": regime,
            "setup": setup,
            "sizing": sizing,
            "zones": zones[:12],
            "strategy_version": cfg.STRATEGY_VERSION,
            "score": score,
            "entry": setup["entry"],
            "stop": setup["stop"],
            "target": setup["target"],
            "setup_name": setup["setup"],
        },
        regime,
        zones,
        candles_1m,
        risk_state.spread_bps,
        funding_rate,
    )
