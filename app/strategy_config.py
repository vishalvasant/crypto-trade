"""Configurable thresholds for BTCUSD paper futures scalping."""

from __future__ import annotations

# Account / risk
INITIAL_BALANCE = 250.0
# Delta India BTCUSD perpetual: 1 contract (lot) = 0.001 BTC, vanilla USD-margined
CONTRACT_VALUE_BTC = 0.001
TICK_SIZE = 0.5
TAKER_FEE_RATE = 0.0005
SLIPPAGE_BPS = 2.0  # 2 bps market slippage estimate
MAX_SPREAD_BPS = 8.0
RISK_PER_TRADE = 0.01  # 1% of equity — practical on a $250 book (Requ 0.25% is too tiny for 1 lot)
MAX_DAILY_LOSS = 0.03  # 3% of starting daily equity on small capital
MAX_CONSECUTIVE_LOSSES = 3
MAX_OPEN_POSITIONS = 1
MIN_REWARD_RISK = 1.5
LEVERAGE = 100.0  # isolated futures leverage requested for paper trading
MAX_MARGIN_FRACTION = 0.40  # never commit more than 40% equity as initial margin
LOSS_COOLDOWN_SECONDS = 5 * 60
TIME_STOP_MINUTES = 8
TRAIL_AFTER_R = 1.0
FEED_STALE_SECONDS = 60
MIN_SETUP_SCORE_ENTRY = 75
MIN_SETUP_SCORE_WATCH = 60

# Regime score weights (−100…+100)
REGIME_WEIGHT_15M = 0.30
REGIME_WEIGHT_5M = 0.30
REGIME_WEIGHT_3M = 0.20
REGIME_WEIGHT_1M = 0.10
REGIME_WEIGHT_FLOW = 0.10
REGIME_BULLISH_THRESHOLD = 35
REGIME_BEARISH_THRESHOLD = -35

# Setup score components (100 pts)
SCORE_REGIME = 25
SCORE_ZONE = 20
SCORE_3M = 20
SCORE_1M = 15
SCORE_OI_FUNDING = 10
SCORE_EXECUTION = 10

SYMBOL = "BTCUSD"
TIMEFRAMES = ("1m", "3m", "5m", "15m")
STRATEGY_VERSION = "requ-v1.1-lev100"
TIMEZONE = "Asia/Kolkata"  # IST — single app timezone for risk day, funding UI, journals
