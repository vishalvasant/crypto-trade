import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import strategy_config as cfg
from app.market_ws import DeltaMarketClient
from app.models import Base, Candle, MarketSnapshot, PaperOrder, Position
from app.timeutil import store_naive_ist
import app.market_ws as market_ws
import app.signal_engine as signal_engine


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session_local_patch = patch.object(signal_engine, "SessionLocal", self.session_factory)
        self.market_session_patch = patch.object(market_ws, "SessionLocal", self.session_factory)
        self.session_local_patch.start()
        self.market_session_patch.start()
        with self.session_factory() as session:
            session.add(
                MarketSnapshot(
                    symbol="BTCUSD",
                    price=100_000.0,
                    mark_price=100_000.0,
                    bid=99_998.0,
                    ask=100_002.0,
                    updated_at=store_naive_ist(),
                )
            )
            session.commit()

    def tearDown(self):
        self.session_local_patch.stop()
        self.market_session_patch.stop()
        self.engine.dispose()

    def test_account_uses_requ_initial_balance(self):
        summary = signal_engine.paper_account_summary()
        self.assertEqual(summary["initial_balance"], cfg.INITIAL_BALANCE)
        self.assertEqual(summary["max_leverage"], cfg.LEVERAGE)

    def test_paper_entry_stop_target_and_stop_exit(self):
        signal = {
            "id": 1,
            "symbol": "BTCUSD",
            "decision": "LONG",
            "side": "LONG",
            "confidence": 0.8,
            "entry": 100_000.0,
            "stop": 99_800.0,
            "target": 100_400.0,
            "setup_name": "A_BREAKOUT_RETEST",
            "sizing": {
                "contracts": 10,
                "quantity_btc": 0.01,
                "margin": 10.0,
                "leverage": 100.0,
                "contract_value": 0.001,
                "entry_fee": 0.5,
            },
        }
        entry = signal_engine.paper_trade_from_signal("BTCUSD", signal)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["side"], "LONG")
        self.assertEqual(entry["leverage"], 100.0)
        self.assertEqual(entry["stop"], 99_800.0)

        # Second entry blocked while position open
        self.assertIsNone(signal_engine.paper_trade_from_signal("BTCUSD", signal))

        with self.session_factory() as session:
            session.query(MarketSnapshot).filter_by(symbol="BTCUSD").update(
                {"price": 99_700.0, "mark_price": 99_700.0, "updated_at": store_naive_ist()}
            )
            session.commit()
        signal_engine.manage_open_positions("BTCUSD")

        with self.session_factory() as session:
            position = session.query(Position).one()
            self.assertFalse(position.is_open)
            self.assertIn(position.close_reason, {"stop_exit", "ambiguous_stop_priority_exit"})
            self.assertEqual(session.query(PaperOrder).filter(PaperOrder.order_type.like("%exit%")).count(), 1)

    def test_wait_decision_does_not_open(self):
        result = signal_engine.paper_trade_from_signal(
            "BTCUSD",
            {"decision": "WAIT", "side": "WAIT", "sizing": {"contracts": 1}},
        )
        self.assertIsNone(result)

    def test_target_exit(self):
        signal_engine.paper_trade_from_signal(
            "BTCUSD",
            {
                "id": 2,
                "decision": "LONG",
                "entry": 100_000.0,
                "stop": 99_800.0,
                "target": 100_400.0,
                "sizing": {"contracts": 5},
            },
        )
        with self.session_factory() as session:
            session.query(MarketSnapshot).filter_by(symbol="BTCUSD").update(
                {"price": 100_450.0, "mark_price": 100_450.0, "updated_at": store_naive_ist()}
            )
            session.commit()
        signal_engine.manage_open_positions("BTCUSD")
        with self.session_factory() as session:
            position = session.query(Position).one()
            self.assertFalse(position.is_open)
            self.assertEqual(position.close_reason, "target_exit")

    def test_exchange_candle_updates_upsert_by_utc_minute(self):
        market_client = DeltaMarketClient()
        market_client._save_candle(
            {
                "symbol": "BTCUSD",
                "interval": "1m",
                "time": 1_797_123_456,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 10.0,
                "closed": True,
            }
        )
        market_client._save_candle(
            {
                "symbol": "BTCUSD",
                "interval": "1m",
                "time": 1_797_123_470,
                "open": 100.0,
                "high": 103.0,
                "low": 99.0,
                "close": 102.0,
                "volume": 12.0,
                "closed": True,
            }
        )
        market_client._save_candle(
            {
                "symbol": "BTCUSD",
                "interval": "5m",
                "time": 1_797_123_456,
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 50.0,
                "closed": True,
            }
        )
        with self.session_factory() as session:
            one_m = session.query(Candle).filter_by(interval="1m").all()
            five_m = session.query(Candle).filter_by(interval="5m").all()
            self.assertEqual(len(one_m), 1)
            self.assertEqual(one_m[0].close, 102.0)
            self.assertEqual(len(five_m), 1)
            self.assertTrue(five_m[0].closed)


if __name__ == "__main__":
    unittest.main()
