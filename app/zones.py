"""Support / resistance / liquidity zone builder per Requ.md §3."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from app import strategy_config as cfg
from app.indicators import compute_atr, compute_vwap, swing_highs_lows


def zone_half_width(atr_5m: float) -> float:
    return max(0.15 * atr_5m, 2 * cfg.TICK_SIZE)


def _make_zone(
    zone_type: str,
    price: float,
    half_width: float,
    timeframe: str,
    strength: float,
    side: str,
) -> Dict[str, object]:
    return {
        "zone_type": zone_type,
        "lower": round(price - half_width, 4),
        "upper": round(price + half_width, 4),
        "mid": round(price, 4),
        "timeframe": timeframe,
        "strength": round(strength, 3),
        "side": side,  # resistance | support | both
    }


def build_support_resistance_zones(
    candles_15m: Sequence[dict],
    candles_5m: Sequence[dict],
    order_book_bids: Optional[Sequence[Sequence[float]]] = None,
    order_book_asks: Optional[Sequence[Sequence[float]]] = None,
) -> List[Dict[str, object]]:
    atr = compute_atr(candles_5m, 14) or compute_atr(candles_15m, 14) or cfg.TICK_SIZE * 10
    half = zone_half_width(atr)
    zones: List[Dict[str, object]] = []

    for timeframe, candles, strength_base in (
        ("15m", candles_15m, 0.9),
        ("5m", candles_5m, 0.7),
    ):
        highs, lows = swing_highs_lows(candles, lookback=2)
        for price in highs[-4:]:
            zones.append(_make_zone("structural", price, half, timeframe, strength_base, "resistance"))
        for price in lows[-4:]:
            zones.append(_make_zone("structural", price, half, timeframe, strength_base, "support"))

    if candles_5m:
        vwap = compute_vwap(candles_5m)
        zones.append(_make_zone("volume", vwap, half, "5m", 0.75, "both"))

        # Session extremes from recent 5m window
        recent = candles_5m[-48:] if len(candles_5m) >= 48 else candles_5m
        session_high = max(float(c["high"]) for c in recent)
        session_low = min(float(c["low"]) for c in recent)
        zones.append(_make_zone("structural", session_high, half, "5m", 0.8, "resistance"))
        zones.append(_make_zone("structural", session_low, half, "5m", 0.8, "support"))

    if order_book_bids:
        for price, size in list(order_book_bids)[:3]:
            strength = min(1.0, float(size) / 50.0) if size else 0.4
            zones.append(_make_zone("liquidity", float(price), half * 0.5, "book", strength, "support"))
    if order_book_asks:
        for price, size in list(order_book_asks)[:3]:
            strength = min(1.0, float(size) / 50.0) if size else 0.4
            zones.append(_make_zone("liquidity", float(price), half * 0.5, "book", strength, "resistance"))

    return _dedupe_zones(zones, half)


def _dedupe_zones(zones: List[Dict[str, object]], merge_distance: float) -> List[Dict[str, object]]:
    if not zones:
        return []
    sorted_zones = sorted(zones, key=lambda z: float(z["mid"]))
    merged: List[Dict[str, object]] = [sorted_zones[0]]
    for zone in sorted_zones[1:]:
        last = merged[-1]
        if abs(float(zone["mid"]) - float(last["mid"])) <= merge_distance:
            if float(zone["strength"]) > float(last["strength"]):
                merged[-1] = zone
            continue
        merged.append(zone)
    return merged


def nearest_zone(
    zones: Sequence[Dict[str, object]],
    price: float,
    side: Optional[str] = None,
) -> Optional[Dict[str, object]]:
    candidates = [
        z
        for z in zones
        if side is None or z.get("side") in {side, "both"}
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda z: abs(float(z["mid"]) - price))


def price_in_zone(price: float, zone: Dict[str, object]) -> bool:
    return float(zone["lower"]) <= price <= float(zone["upper"])


def interpret_level(candles: Sequence[dict], zone: Dict[str, object]) -> str:
    """Return continuation | sweep | choppy | none based on recent closes vs zone."""
    if len(candles) < 3:
        return "none"
    c0 = float(candles[-3]["close"])
    c1 = float(candles[-2]["close"])
    c2 = float(candles[-1]["close"])
    upper = float(zone["upper"])
    lower = float(zone["lower"])

    # Break and hold above resistance
    if c0 <= upper and c1 > upper and c2 > upper:
        return "bullish_continuation"
    # Sweep above then close back below
    if float(candles[-2]["high"]) > upper and c2 < lower:
        return "bearish_sweep"
    # Break and hold below support
    if c0 >= lower and c1 < lower and c2 < lower:
        return "bearish_continuation"
    # Sweep below then reclaim
    if float(candles[-2]["low"]) < lower and c2 > upper:
        return "bullish_sweep"
    # Choppy crossings
    crosses = 0
    mid = float(zone["mid"])
    for i in range(1, min(6, len(candles))):
        prev = float(candles[-i - 1]["close"])
        cur = float(candles[-i]["close"])
        if (prev - mid) * (cur - mid) < 0:
            crosses += 1
    if crosses >= 3:
        return "choppy"
    return "none"
