"""Multi-timeframe regime classification (−100…+100) per Requ.md §2."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from app import strategy_config as cfg
from app.indicators import (
    compute_atr,
    compute_ema,
    compute_vwap,
    relative_volume,
    structure_bias,
)


def _tf_momentum(candles: Sequence[dict], fast: int, slow: int) -> float:
    if len(candles) < slow + 2:
        return 0.0
    closes = [float(c["close"]) for c in candles]
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    last = closes[-1]
    structure = structure_bias(candles)
    ema_part = 1.0 if ema_fast > ema_slow and last > ema_fast else -1.0 if ema_fast < ema_slow and last < ema_fast else 0.0
    return max(-1.0, min(1.0, 0.6 * ema_part + 0.4 * structure))


def _vwap_bias(candles: Sequence[dict]) -> float:
    if len(candles) < 5:
        return 0.0
    vwap = compute_vwap(candles)
    last = float(candles[-1]["close"])
    atr = compute_atr(candles, 14) or 1.0
    distance = (last - vwap) / atr
    return max(-1.0, min(1.0, distance / 2.0))


def _flow_bias(oi_change: Optional[float], last_trade_side: Optional[str], volume_ratio: float) -> float:
    score = 0.0
    if oi_change is not None:
        if oi_change > 0:
            score += 0.3
        elif oi_change < 0:
            score -= 0.2
    side = (last_trade_side or "").lower()
    if side in {"buy", "b"}:
        score += 0.3
    elif side in {"sell", "s"}:
        score -= 0.3
    if volume_ratio >= 1.3:
        score += 0.2 if score >= 0 else -0.2
    return max(-1.0, min(1.0, score))


def classify_regime(
    candles_15m: Sequence[dict],
    candles_5m: Sequence[dict],
    candles_3m: Sequence[dict],
    candles_1m: Sequence[dict],
    oi_change: Optional[float] = None,
    last_trade_side: Optional[str] = None,
    atr_spike_ratio: Optional[float] = None,
) -> Dict[str, object]:
    score_15m = _tf_momentum(candles_15m, 20, 50) if len(candles_15m) >= 20 else _tf_momentum(candles_15m, 9, 21)
    score_5m = 0.7 * _tf_momentum(candles_5m, 9, 21) + 0.3 * _vwap_bias(candles_5m)
    score_3m = _tf_momentum(candles_3m, 9, 21)
    score_1m = _tf_momentum(candles_1m, 5, 13)
    vol_ratio = relative_volume(candles_1m or candles_5m)
    flow = _flow_bias(oi_change, last_trade_side, vol_ratio)

    raw = (
        cfg.REGIME_WEIGHT_15M * score_15m
        + cfg.REGIME_WEIGHT_5M * score_5m
        + cfg.REGIME_WEIGHT_3M * score_3m
        + cfg.REGIME_WEIGHT_1M * score_1m
        + cfg.REGIME_WEIGHT_FLOW * flow
    )
    score = round(raw * 100, 2)

    atr_5m = compute_atr(candles_5m, 14)
    atr_avg = compute_atr(candles_5m[:-5], 14) if len(candles_5m) > 20 else atr_5m
    spike = atr_spike_ratio
    if spike is None and atr_avg and atr_avg > 0:
        spike = atr_5m / atr_avg

    state = "RANGE"
    if spike and spike >= 2.2:
        state = "UNSTABLE"
    elif score >= cfg.REGIME_BULLISH_THRESHOLD:
        state = "BULLISH"
    elif score <= cfg.REGIME_BEARISH_THRESHOLD:
        state = "BEARISH"

    vwap = compute_vwap(candles_5m) if candles_5m else 0.0
    return {
        "state": state,
        "score": score,
        "components": {
            "tf_15m": round(score_15m * 100, 2),
            "tf_5m": round(score_5m * 100, 2),
            "tf_3m": round(score_3m * 100, 2),
            "tf_1m": round(score_1m * 100, 2),
            "flow": round(flow * 100, 2),
        },
        "vwap": round(vwap, 4),
        "atr_5m": round(atr_5m, 4),
        "atr_spike_ratio": round(spike or 1.0, 3),
        "volume_ratio": round(vol_ratio, 3),
    }


def candles_closed_enough(candles_by_tf: Dict[str, List[dict]], min_counts: Optional[Dict[str, int]] = None) -> bool:
    defaults = {"1m": 30, "3m": 20, "5m": 20, "15m": 15}
    needed = min_counts or defaults
    for tf, count in needed.items():
        rows = candles_by_tf.get(tf) or []
        closed = [c for c in rows if c.get("closed", True)]
        if len(closed) < count:
            return False
    return True
