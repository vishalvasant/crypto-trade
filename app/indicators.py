"""Shared technical indicators used by regime, zones, and setups."""

from __future__ import annotations

from typing import Iterable, List, Sequence


def safe_average(values: Iterable[float]) -> float:
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def compute_ema_series(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    ema = float(values[0])
    series = [ema]
    for value in values[1:]:
        ema = (float(value) - ema) * multiplier + ema
        series.append(ema)
    return series


def compute_ema(values: Sequence[float], period: int) -> float:
    series = compute_ema_series(values, period)
    return series[-1] if series else 0.0


def compute_rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [abs(min(delta, 0.0)) for delta in deltas]
    avg_gain = safe_average(gains[-period:])
    avg_loss = safe_average(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_atr(candles: Sequence[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    true_ranges: List[float] = []
    previous_close = float(candles[0]["close"])
    for candle in candles[1:]:
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = close
    return safe_average(true_ranges[-period:])


def compute_vwap(candles: Sequence[dict]) -> float:
    numerator = 0.0
    denominator = 0.0
    for candle in candles:
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        volume = float(candle.get("volume") or 0.0)
        typical = (high + low + close) / 3.0
        numerator += typical * volume
        denominator += volume
    if denominator <= 0:
        return float(candles[-1]["close"]) if candles else 0.0
    return numerator / denominator


def swing_highs_lows(candles: Sequence[dict], lookback: int = 3) -> tuple[List[float], List[float]]:
    highs: List[float] = []
    lows: List[float] = []
    if len(candles) < lookback * 2 + 1:
        return highs, lows
    for idx in range(lookback, len(candles) - lookback):
        high = float(candles[idx]["high"])
        low = float(candles[idx]["low"])
        left_highs = [float(candles[i]["high"]) for i in range(idx - lookback, idx)]
        right_highs = [float(candles[i]["high"]) for i in range(idx + 1, idx + lookback + 1)]
        left_lows = [float(candles[i]["low"]) for i in range(idx - lookback, idx)]
        right_lows = [float(candles[i]["low"]) for i in range(idx + 1, idx + lookback + 1)]
        if high >= max(left_highs + right_highs):
            highs.append(high)
        if low <= min(left_lows + right_lows):
            lows.append(low)
    return highs, lows


def structure_bias(candles: Sequence[dict]) -> float:
    """Return roughly −1…+1 from higher-highs/higher-lows vs lower-highs/lower-lows."""
    highs, lows = swing_highs_lows(candles, lookback=2)
    score = 0.0
    if len(highs) >= 2:
        score += 0.5 if highs[-1] > highs[-2] else -0.5
    if len(lows) >= 2:
        score += 0.5 if lows[-1] > lows[-2] else -0.5
    return score


def relative_volume(candles: Sequence[dict], window: int = 20) -> float:
    if not candles:
        return 0.0
    volumes = [float(c.get("volume") or 0.0) for c in candles]
    avg = safe_average(volumes[-window:])
    if avg <= 0:
        return 0.0
    return volumes[-1] / avg
