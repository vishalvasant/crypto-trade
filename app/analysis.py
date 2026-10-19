"""Backward-compatible indicator helpers; strategy logic lives in decision_engine."""

from __future__ import annotations

from typing import List, Optional

from app.indicators import compute_atr, compute_ema, compute_rsi, relative_volume


def evaluate_market(symbol: str, candles: List[dict]) -> Optional[dict]:
    """Legacy single-TF helper kept for older tests; prefer decision_engine.evaluate_market."""
    if len(candles) < 20:
        return None

    closes = [float(c["close"]) for c in candles if "close" in c]
    ema_short = compute_ema(closes, 9)
    ema_long = compute_ema(closes, 21)
    rsi = compute_rsi(closes, 14)
    atr = compute_atr(candles, 14)
    if atr <= 0:
        return None
    volume_ratio = relative_volume(candles)
    last_close = closes[-1]
    prev_close = closes[-2]
    trend = (
        "bullish"
        if ema_short > ema_long and last_close > prev_close
        else "bearish"
        if ema_short < ema_long and last_close < prev_close
        else "neutral"
    )

    if trend == "bullish" and rsi > 55 and volume_ratio >= 1.2:
        side = "BUY"
        reason = "Bullish trend, RSI above 55, volume expansion"
        rsi_strength = min(max((rsi - 55) / 20, 0.0), 1.0)
        volume_strength = min(max((volume_ratio - 1.2) / 2.0, 0.0), 1.0)
        confidence = min(0.95, 0.6 + rsi_strength * 0.2 + volume_strength * 0.15)
    elif trend == "bearish" and rsi < 45 and volume_ratio >= 1.2:
        side = "SELL"
        reason = "Bearish trend, RSI below 45, volume expansion"
        rsi_strength = min(max((45 - rsi) / 20, 0.0), 1.0)
        volume_strength = min(max((volume_ratio - 1.2) / 2.0, 0.0), 1.0)
        confidence = min(0.95, 0.6 + rsi_strength * 0.2 + volume_strength * 0.15)
    else:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "confidence": round(confidence, 3),
        "reason": reason,
        "rsi": round(rsi, 2),
        "ema_short": round(ema_short, 4),
        "ema_long": round(ema_long, 4),
        "volume_ratio": round(volume_ratio, 3),
        "atr": round(atr, 4),
        "price": last_close,
    }
