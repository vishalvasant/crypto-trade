import unittest
from datetime import datetime, timezone

from app.decision_engine import evaluate_market
from app.regime import classify_regime
from app.risk import RiskState, check_hard_gates, position_size
from app.setups import detect_setup_a_breakout_retest, detect_setup_b_sweep_reclaim, score_setup
from app.zones import build_support_resistance_zones, zone_half_width


def _candle(o, h, l, c, v=10.0, closed=True):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, "closed": closed}


def _trend_candles(start: float, step: float, n: int = 60, vol: float = 20.0):
    rows = []
    price = start
    for _ in range(n):
        nxt = price + step
        high = max(price, nxt) + abs(step) * 0.2
        low = min(price, nxt) - abs(step) * 0.2
        rows.append(_candle(price, high, low, nxt, vol))
        price = nxt
    return rows


class RegimeAndZonesTests(unittest.TestCase):
    def test_bullish_regime_on_uptrend(self):
        up = _trend_candles(100_000, 40, 80)
        regime = classify_regime(up, up, up, up, oi_change=1_000, last_trade_side="buy")
        self.assertIn(regime["state"], {"BULLISH", "RANGE"})
        self.assertGreater(regime["score"], 0)

    def test_zone_half_width_respects_tick(self):
        self.assertGreaterEqual(zone_half_width(1.0), 1.0)

    def test_zones_built_from_swings(self):
        candles = _trend_candles(100_000, 25, 50)
        zones = build_support_resistance_zones(candles, candles)
        self.assertGreater(len(zones), 0)


class SetupTests(unittest.TestCase):
    def test_setup_a_long_breakout_retest(self):
        # Build flat then breakout above resistance ~101000 then retest
        base = [_candle(100_000, 100_200, 99_800, 100_000, 15) for _ in range(20)]
        breakout = [
            _candle(100_000, 101_400, 100_000, 101_200, 40),
            _candle(101_200, 101_500, 101_050, 101_300, 30),
            _candle(101_300, 101_400, 101_050, 101_150, 25),  # retest hold
        ]
        c3 = base + breakout
        c1 = [
            _candle(101_100, 101_200, 101_050, 101_120, 20),
            _candle(101_120, 101_250, 101_100, 101_220, 22),
        ]
        zones = [
            {
                "zone_type": "structural",
                "lower": 100_900,
                "upper": 101_100,
                "mid": 101_000,
                "timeframe": "5m",
                "strength": 0.9,
                "side": "resistance",
            },
            {
                "zone_type": "structural",
                "lower": 102_400,
                "upper": 102_600,
                "mid": 102_500,
                "timeframe": "5m",
                "strength": 0.8,
                "side": "resistance",
            },
        ]
        setup = detect_setup_a_breakout_retest(c3, c1, zones, "LONG")
        self.assertIsNotNone(setup)
        self.assertEqual(setup["side"], "LONG")
        scored = score_setup(
            setup,
            {"state": "BULLISH", "score": 50},
            spread_bps=2.0,
            oi_change=500,
            funding_rate=0.0,
            candles_1m=c1,
        )
        self.assertGreaterEqual(scored["score"], 60)

    def test_setup_b_long_sweep_reclaim(self):
        zones = [
            {
                "zone_type": "structural",
                "lower": 99_900,
                "upper": 100_100,
                "mid": 100_000,
                "timeframe": "5m",
                "strength": 0.85,
                "side": "support",
            },
            {
                "zone_type": "structural",
                "lower": 100_900,
                "upper": 101_100,
                "mid": 101_000,
                "timeframe": "5m",
                "strength": 0.8,
                "side": "resistance",
            },
        ]
        c3 = [
            _candle(100_200, 100_300, 100_100, 100_150, 15),
            _candle(100_150, 100_200, 99_700, 99_850, 40),  # sweep
            _candle(99_850, 100_250, 99_800, 100_200, 35),  # reclaim
            _candle(100_200, 100_280, 100_150, 100_250, 30),
        ]
        c1 = [
            _candle(99_900, 100_050, 99_700, 99_800, 20),
            _candle(99_800, 100_220, 99_780, 100_180, 50),
        ]
        setup = detect_setup_b_sweep_reclaim(c3, c1, zones, "LONG")
        self.assertIsNotNone(setup)
        self.assertEqual(setup["setup"], "B_SWEEP_RECLAIM")


class RiskTests(unittest.TestCase):
    def test_hard_gates_stale_feed(self):
        state = RiskState(
            equity=10_000,
            daily_starting_equity=10_000,
            daily_realized_loss=0,
            consecutive_losses=0,
            position_open=False,
            last_loss_at=None,
            feed_is_healthy=False,
            candles_are_complete=True,
            spread_bps=2.0,
        )
        result = check_hard_gates(state)
        self.assertEqual(result["rejection_reason"], "STALE_OR_DISCONNECTED_FEED")

    def test_daily_loss_limit(self):
        state = RiskState(
            equity=240,
            daily_starting_equity=250,
            daily_realized_loss=8,  # 3.2% > 3% daily cap
            consecutive_losses=0,
            position_open=False,
            last_loss_at=None,
            feed_is_healthy=True,
            candles_are_complete=True,
            spread_bps=2.0,
        )
        result = check_hard_gates(state)
        self.assertEqual(result["rejection_reason"], "DAILY_LOSS_LIMIT")

    def test_position_sizing_rr(self):
        sizing = position_size(equity=250, entry=82_000, stop=81_800, target=82_400)
        self.assertIsNotNone(sizing)
        self.assertGreaterEqual(sizing["contracts"], 1)
        self.assertEqual(sizing["leverage"], 100.0)
        self.assertEqual(sizing["contract_value"], 0.001)
        self.assertGreaterEqual(sizing["reward_risk_gross"], 1.5)
        self.assertLessEqual(sizing["monetary_risk"], 250 * 0.01 * 1.01)
        # Margin at 100× must be far below notional
        self.assertAlmostEqual(sizing["margin"], sizing["notional_usd"] / 100.0, places=4)

    def test_position_sizing_rejects_poor_rr(self):
        sizing = position_size(equity=250, entry=82_000, stop=81_800, target=82_050)
        self.assertIsNone(sizing)

    def test_lot_size_example_250_at_100x(self):
        """$250 equity, 100×, stop $200 → risk-capped lots on 0.001 BTC contracts."""
        sizing = position_size(equity=250, entry=82_000, stop=81_800, target=82_400)
        self.assertIsNotNone(sizing)
        # Risk budget $2.50; ~$0.20 stop risk/lot + fees ⇒ a handful of lots, not hundreds
        self.assertGreaterEqual(sizing["contracts"], 1)
        self.assertLessEqual(sizing["contracts"], 20)
        self.assertLessEqual(sizing["margin"], 250 * 0.40)


class DecisionPipelineTests(unittest.TestCase):
    def test_evaluate_returns_wait_without_setup(self):
        flat = [_candle(100_000, 100_050, 99_950, 100_000, 10) for _ in range(40)]
        risk = RiskState(
            equity=10_000,
            daily_starting_equity=10_000,
            daily_realized_loss=0,
            consecutive_losses=0,
            position_open=False,
            last_loss_at=None,
            feed_is_healthy=True,
            candles_are_complete=True,
            spread_bps=2.0,
        )
        result = evaluate_market(
            {"1m": flat, "3m": flat, "5m": flat, "15m": flat},
            risk,
        )
        self.assertEqual(result["decision"], "WAIT")
        self.assertIsNotNone(result["regime"])


if __name__ == "__main__":
    unittest.main()
