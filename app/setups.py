"""Setup A (breakout/retest) and Setup B (sweep/reclaim) with 100-pt scoring."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from app import strategy_config as cfg
from app.indicators import compute_atr, relative_volume, structure_bias
from app.zones import interpret_level, nearest_zone, price_in_zone


def _enforce_min_target(side: str, entry: float, stop: float, target: float) -> float:
    """Ensure target is at least MIN_REWARD_RISK × stop distance away from entry."""
    risk = abs(entry - stop)
    min_reward = risk * cfg.MIN_REWARD_RISK
    if side == "LONG":
        return max(float(target), entry + min_reward)
    return min(float(target), entry - min_reward)


def _execution_quality(spread_bps: Optional[float], atr: float, price: float) -> int:
    if atr <= 0 or price <= 0:
        return 0
    points = cfg.SCORE_EXECUTION
    if spread_bps is None:
        return points // 2
    if spread_bps > cfg.MAX_SPREAD_BPS:
        return 0
    if spread_bps <= cfg.MAX_SPREAD_BPS * 0.4:
        return points
    if spread_bps <= cfg.MAX_SPREAD_BPS * 0.75:
        return int(points * 0.7)
    return int(points * 0.4)


def _oi_funding_points(
    side: str,
    oi_change: Optional[float],
    funding_rate: Optional[float],
    price_up: bool,
) -> int:
    points = 0
    max_pts = cfg.SCORE_OI_FUNDING
    if oi_change is not None:
        # Price up + OI up confirms longs; price down + OI up confirms shorts
        if side == "LONG" and price_up and oi_change > 0:
            points += 5
        elif side == "SHORT" and not price_up and oi_change > 0:
            points += 5
        elif side == "LONG" and price_up and oi_change < 0:
            points += 1  # short covering — weak
        elif side == "SHORT" and not price_up and oi_change < 0:
            points += 1
        else:
            points += 2
    else:
        points += 2

    if funding_rate is not None:
        # Extreme funding is context, not auto-fade
        if side == "LONG" and funding_rate < -0.0001:
            points += 3  # shorts crowded — squeeze context
        elif side == "SHORT" and funding_rate > 0.0001:
            points += 3
        elif abs(funding_rate) < 0.00005:
            points += 2
        else:
            points += 1
    else:
        points += 2
    return min(max_pts, points)


def _regime_alignment_points(side: str, regime_state: str, regime_score: float) -> int:
    max_pts = cfg.SCORE_REGIME
    if regime_state == "UNSTABLE":
        return 0
    if side == "LONG":
        if regime_state == "BULLISH" or regime_score >= cfg.REGIME_BULLISH_THRESHOLD:
            return max_pts
        if regime_state == "RANGE" and abs(regime_score) < cfg.REGIME_BULLISH_THRESHOLD:
            return int(max_pts * 0.5)
        if regime_score > 0:
            return int(max_pts * 0.35)
        return 0
    if regime_state == "BEARISH" or regime_score <= cfg.REGIME_BEARISH_THRESHOLD:
        return max_pts
    if regime_state == "RANGE" and abs(regime_score) < cfg.REGIME_BULLISH_THRESHOLD:
        return int(max_pts * 0.5)
    if regime_score < 0:
        return int(max_pts * 0.35)
    return 0


def detect_setup_a_breakout_retest(
    candles_3m: Sequence[dict],
    candles_1m: Sequence[dict],
    zones: Sequence[Dict[str, object]],
    side: str,
) -> Optional[Dict[str, object]]:
    if len(candles_3m) < 5 or len(candles_1m) < 2:
        return None
    price = float(candles_1m[-1]["close"])
    atr = compute_atr(candles_3m, 14) or 1.0

    if side == "LONG":
        zone = nearest_zone(zones, price, side="resistance")
        if not zone:
            return None
        # Breakout: earlier close above zone, retest holding above
        broke = any(float(c["close"]) > float(zone["upper"]) for c in candles_3m[-6:-1])
        retest = price_in_zone(float(candles_3m[-1]["low"]), zone) or (
            float(candles_3m[-1]["low"]) <= float(zone["upper"]) + atr * 0.15
            and float(candles_3m[-1]["close"]) >= float(zone["mid"])
        )
        holding = float(candles_3m[-1]["close"]) >= float(zone["lower"])
        trigger = float(candles_1m[-1]["close"]) > float(candles_1m[-2]["close"])
        if broke and retest and holding and trigger:
            stop = min(float(zone["lower"]), float(candles_3m[-1]["low"])) - atr * 0.15
            target_zone = nearest_zone(
                [z for z in zones if float(z["mid"]) > price],
                price,
                side="resistance",
            )
            raw_target = float(target_zone["mid"]) if target_zone else price + (price - stop) * cfg.MIN_REWARD_RISK
            target = _enforce_min_target("LONG", price, stop, raw_target)
            return {
                "setup": "A_BREAKOUT_RETEST",
                "side": "LONG",
                "zone": zone,
                "entry": price,
                "stop": stop,
                "target": target,
                "confirm_3m": True,
                "trigger_1m": True,
                "zone_quality": float(zone["strength"]),
            }
    else:
        zone = nearest_zone(zones, price, side="support")
        if not zone:
            return None
        broke = any(float(c["close"]) < float(zone["lower"]) for c in candles_3m[-6:-1])
        retest = price_in_zone(float(candles_3m[-1]["high"]), zone) or (
            float(candles_3m[-1]["high"]) >= float(zone["lower"]) - atr * 0.15
            and float(candles_3m[-1]["close"]) <= float(zone["mid"])
        )
        holding = float(candles_3m[-1]["close"]) <= float(zone["upper"])
        trigger = float(candles_1m[-1]["close"]) < float(candles_1m[-2]["close"])
        if broke and retest and holding and trigger:
            stop = max(float(zone["upper"]), float(candles_3m[-1]["high"])) + atr * 0.15
            target_zone = nearest_zone(
                [z for z in zones if float(z["mid"]) < price],
                price,
                side="support",
            )
            raw_target = float(target_zone["mid"]) if target_zone else price - (stop - price) * cfg.MIN_REWARD_RISK
            target = _enforce_min_target("SHORT", price, stop, raw_target)
            return {
                "setup": "A_BREAKOUT_RETEST",
                "side": "SHORT",
                "zone": zone,
                "entry": price,
                "stop": stop,
                "target": target,
                "confirm_3m": True,
                "trigger_1m": True,
                "zone_quality": float(zone["strength"]),
            }
    return None


def detect_setup_b_sweep_reclaim(
    candles_3m: Sequence[dict],
    candles_1m: Sequence[dict],
    zones: Sequence[Dict[str, object]],
    side: str,
) -> Optional[Dict[str, object]]:
    if len(candles_3m) < 4 or len(candles_1m) < 2:
        return None
    price = float(candles_1m[-1]["close"])
    atr = compute_atr(candles_3m, 14) or 1.0
    vol_ok = relative_volume(candles_1m) >= 1.05

    if side == "LONG":
        zone = nearest_zone(zones, price, side="support")
        if not zone:
            return None
        interp = interpret_level(candles_3m, zone)
        swept = float(candles_1m[-2]["low"]) < float(zone["lower"]) or float(candles_3m[-2]["low"]) < float(zone["lower"])
        reclaim = float(candles_1m[-1]["close"]) >= float(zone["lower"])
        structure_ok = structure_bias(candles_3m) >= 0 or float(candles_3m[-1]["low"]) > float(candles_3m[-2]["low"])
        if (interp == "bullish_sweep" or (swept and reclaim)) and structure_ok and vol_ok:
            stop = min(float(candles_1m[-2]["low"]), float(zone["lower"])) - atr * 0.1
            target_zone = nearest_zone(
                [z for z in zones if float(z["mid"]) > price],
                price,
                side="resistance",
            )
            raw_target = float(target_zone["mid"]) if target_zone else price + (price - stop) * cfg.MIN_REWARD_RISK
            target = _enforce_min_target("LONG", price, stop, raw_target)
            return {
                "setup": "B_SWEEP_RECLAIM",
                "side": "LONG",
                "zone": zone,
                "entry": price,
                "stop": stop,
                "target": target,
                "confirm_3m": True,
                "trigger_1m": True,
                "zone_quality": float(zone["strength"]),
            }
    else:
        zone = nearest_zone(zones, price, side="resistance")
        if not zone:
            return None
        interp = interpret_level(candles_3m, zone)
        swept = float(candles_1m[-2]["high"]) > float(zone["upper"]) or float(candles_3m[-2]["high"]) > float(zone["upper"])
        reclaim = float(candles_1m[-1]["close"]) <= float(zone["upper"])
        structure_ok = structure_bias(candles_3m) <= 0 or float(candles_3m[-1]["high"]) < float(candles_3m[-2]["high"])
        if (interp == "bearish_sweep" or (swept and reclaim)) and structure_ok and vol_ok:
            stop = max(float(candles_1m[-2]["high"]), float(zone["upper"])) + atr * 0.1
            target_zone = nearest_zone(
                [z for z in zones if float(z["mid"]) < price],
                price,
                side="support",
            )
            raw_target = float(target_zone["mid"]) if target_zone else price - (stop - price) * cfg.MIN_REWARD_RISK
            target = _enforce_min_target("SHORT", price, stop, raw_target)
            return {
                "setup": "B_SWEEP_RECLAIM",
                "side": "SHORT",
                "zone": zone,
                "entry": price,
                "stop": stop,
                "target": target,
                "confirm_3m": True,
                "trigger_1m": True,
                "zone_quality": float(zone["strength"]),
            }
    return None


def score_setup(
    setup: Dict[str, object],
    regime: Dict[str, object],
    spread_bps: Optional[float],
    oi_change: Optional[float],
    funding_rate: Optional[float],
    candles_1m: Sequence[dict],
) -> Dict[str, object]:
    side = str(setup["side"])
    price = float(setup["entry"])
    atr = abs(float(setup["entry"]) - float(setup["stop"])) or 1.0
    price_up = len(candles_1m) >= 2 and float(candles_1m[-1]["close"]) >= float(candles_1m[-5]["close"] if len(candles_1m) >= 5 else candles_1m[0]["close"])

    regime_pts = _regime_alignment_points(side, str(regime["state"]), float(regime["score"]))
    zone_pts = int(cfg.SCORE_ZONE * float(setup.get("zone_quality") or 0.5))
    confirm_3m = cfg.SCORE_3M if setup.get("confirm_3m") else 0
    trigger_1m = cfg.SCORE_1M if setup.get("trigger_1m") else 0
    oi_pts = _oi_funding_points(side, oi_change, funding_rate, price_up)
    exec_pts = _execution_quality(spread_bps, atr, price)

    total = regime_pts + zone_pts + confirm_3m + trigger_1m + oi_pts + exec_pts
    reward = abs(float(setup["target"]) - float(setup["entry"]))
    risk = abs(float(setup["entry"]) - float(setup["stop"]))
    rr = (reward / risk) if risk > 0 else 0.0

    reasons = [
        f"regime={regime['state']}({regime['score']})+{regime_pts}",
        f"zone+{zone_pts}",
        f"3m+{confirm_3m}",
        f"1m+{trigger_1m}",
        f"oi_funding+{oi_pts}",
        f"exec+{exec_pts}",
        f"rr={rr:.2f}",
    ]
    return {
        **setup,
        "score": total,
        "reward_risk": round(rr, 3),
        "score_breakdown": {
            "regime": regime_pts,
            "zone": zone_pts,
            "confirm_3m": confirm_3m,
            "trigger_1m": trigger_1m,
            "oi_funding": oi_pts,
            "execution": exec_pts,
        },
        "reasons": reasons,
    }


def find_best_setup(
    candles_3m: Sequence[dict],
    candles_1m: Sequence[dict],
    zones: Sequence[Dict[str, object]],
    regime: Dict[str, object],
    spread_bps: Optional[float] = None,
    oi_change: Optional[float] = None,
    funding_rate: Optional[float] = None,
) -> Optional[Dict[str, object]]:
    candidates: List[Dict[str, object]] = []
    preferred_sides = []
    state = str(regime.get("state"))
    if state == "BULLISH":
        preferred_sides = ["LONG", "SHORT"]
    elif state == "BEARISH":
        preferred_sides = ["SHORT", "LONG"]
    else:
        preferred_sides = ["LONG", "SHORT"]

    for side in preferred_sides:
        for detector in (detect_setup_a_breakout_retest, detect_setup_b_sweep_reclaim):
            raw = detector(candles_3m, candles_1m, zones, side)
            if raw:
                scored = score_setup(raw, regime, spread_bps, oi_change, funding_rate, candles_1m)
                candidates.append(scored)

    if not candidates:
        return None
    return max(candidates, key=lambda s: int(s["score"]))
