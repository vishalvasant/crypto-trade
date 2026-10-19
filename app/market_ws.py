import asyncio
import json
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import websockets

from app import strategy_config as cfg
from app.config import get_delta_ws_url
from app.db import SessionLocal
from app.models import Candle, MarketSnapshot, OrderBookSnapshot, TradeEvent
from app.timeutil import IST, UTC, now_ist, store_naive_ist

REST_BASE = "https://api.india.delta.exchange"
RESOLUTIONS = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
}
INTERVAL_MINUTES = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "60m": 60,
}


def floor_candle_time(dt: datetime, interval: str) -> datetime:
    """Align a timestamp to the candle open for the given resolution (IST wall clock)."""
    minutes = INTERVAL_MINUTES.get(interval, 1)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)
    total = dt.hour * 60 + dt.minute
    floored = (total // minutes) * minutes
    return dt.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def interval_seconds(interval: str) -> int:
    return INTERVAL_MINUTES.get(interval, 1) * 60


class DeltaMarketClient:
    def __init__(self) -> None:
        self.ws_url = get_delta_ws_url()
        self.loop = asyncio.get_event_loop()
        self.task: Optional[asyncio.Task] = None
        self._backoff = 5
        self._rest_cycle = 0

    async def run(self) -> None:
        """Keep market data fresh via WS + always-on REST heartbeat."""
        await asyncio.to_thread(self._fallback_rest_poll)
        rest_task = asyncio.create_task(self._rest_heartbeat_loop())
        try:
            await self._ws_loop()
        finally:
            rest_task.cancel()
            try:
                await rest_task
            except asyncio.CancelledError:
                pass

    async def _rest_heartbeat_loop(self) -> None:
        """Never leave the feed stale for hours if Delta WS dies silently."""
        while True:
            try:
                await asyncio.to_thread(self._light_rest_poll)
                self._rest_cycle += 1
                if self._rest_cycle % 8 == 0:
                    await asyncio.to_thread(self._fallback_rest_poll)
            except Exception as exc:  # pragma: no cover
                print(f"REST heartbeat error: {exc}")
            await asyncio.sleep(15)

    def _light_rest_poll(self) -> None:
        """Ticker + recent 1m/5m candles — cheap keep-alive for mark/P&L/signals."""
        symbol = cfg.SYMBOL
        ticker_url = f"{REST_BASE}/v2/tickers/{symbol}"
        data = self._rest_get_json(ticker_url)
        if data and isinstance(data, dict):
            result = data.get("result")
            if isinstance(result, dict):
                price = float(result.get("close") or result.get("last_price") or result.get("mark_price") or 0.0)
                self._save_snapshot(
                    symbol=symbol,
                    price=price,
                    bid=float(result.get("bid") or 0.0),
                    ask=float(result.get("ask") or 0.0),
                    mark_price=float(result.get("mark_price") or price),
                    volume=float(result.get("volume") or 0.0),
                    oi=float(result.get("oi_value") or result.get("open_interest") or 0.0),
                    oi_change_6h=float(result.get("oi_change_usd_6h") or 0.0),
                    funding_rate=float(result.get("funding_rate") or 0.0)
                    if result.get("funding_rate") is not None
                    else None,
                    last_trade_side=str(result.get("side") or ""),
                )

        end = int(time.time())
        start = end - 3 * 3600
        for resolution in ("1m", "5m"):
            candles_url = (
                f"{REST_BASE}/v2/history/candles?symbol={symbol}"
                f"&resolution={resolution}&start={start}&end={end}&limit=120"
            )
            candle_payload = self._rest_get_json(candles_url)
            if not candle_payload or not isinstance(candle_payload, dict):
                continue
            rows = candle_payload.get("result") or []
            if not isinstance(rows, list):
                continue
            for row in rows[-40:]:
                if not isinstance(row, dict):
                    continue
                self._save_candle(
                    {
                        "symbol": symbol,
                        "interval": resolution,
                        "open": float(row.get("open") or 0.0),
                        "high": float(row.get("high") or 0.0),
                        "low": float(row.get("low") or 0.0),
                        "close": float(row.get("close") or 0.0),
                        "volume": float(row.get("volume") or 0.0),
                        "time": row.get("time"),
                        "closed": True,
                    }
                )

        orderbook_url = f"{REST_BASE}/v2/l2orderbook/{symbol}?depth=10"
        orderbook_payload = self._rest_get_json(orderbook_url)
        if isinstance(orderbook_payload, dict) and isinstance(orderbook_payload.get("result"), dict):
            result = orderbook_payload["result"]
            bids = result.get("buy") or []
            asks = result.get("sell") or []
            if bids and asks:
                bid_payload = [
                    [float(level.get("price", 0.0)), float(level.get("size", 0.0))] for level in bids[:10]
                ]
                ask_payload = [
                    [float(level.get("price", 0.0)), float(level.get("size", 0.0))] for level in asks[:10]
                ]
                best_bid = float(bids[0].get("price", 0.0))
                best_ask = float(asks[0].get("price", 0.0))
                self._save_orderbook(
                    symbol=symbol,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    spread=best_ask - best_bid,
                    bids=json.dumps(bid_payload),
                    asks=json.dumps(ask_payload),
                )

    async def _ws_loop(self) -> None:
        urls = [self.ws_url]
        for alt in ("wss://public-socket.india.delta.exchange", "wss://socket.india.delta.exchange"):
            if alt not in urls:
                urls.append(alt)
        url_idx = 0
        while True:
            url = urls[url_idx % len(urls)]
            try:
                async with websockets.connect(
                    url,
                    ping_interval=15,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=1024,
                    user_agent_header="autotrade-paper/1.0",
                ) as websocket:
                    self._backoff = 5
                    print(f"Delta websocket connected: {url}")
                    candle_channels = [
                        {"name": f"candlestick_{res}", "symbols": [cfg.SYMBOL]}
                        for res in RESOLUTIONS
                    ]
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "payload": {
                                    "channels": [
                                        {"name": "v2/ticker", "symbols": [cfg.SYMBOL]},
                                        {"name": "ticker", "symbols": [cfg.SYMBOL]},
                                        {"name": "trades", "symbols": [cfg.SYMBOL]},
                                        {"name": "l2_orderbook", "symbols": [cfg.SYMBOL]},
                                        {"name": "ob_l2", "symbols": [cfg.SYMBOL]},
                                        *candle_channels,
                                        {"name": "mark_price", "symbols": [f"MARK:{cfg.SYMBOL}"]},
                                        {"name": "funding_rate", "symbols": [cfg.SYMBOL]},
                                    ]
                                },
                            }
                        )
                    )
                    while True:
                        try:
                            raw_message = await asyncio.wait_for(websocket.recv(), timeout=45)
                        except asyncio.TimeoutError:
                            print("Delta websocket idle >45s — forcing reconnect")
                            break
                        await self._handle_message(raw_message)
            except Exception as exc:  # pragma: no cover
                print(f"Delta websocket reconnecting after error ({url}): {exc}")
                url_idx += 1
                try:
                    await asyncio.to_thread(self._light_rest_poll)
                except Exception:
                    pass
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 60)

    def _rest_get_json(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "autotrade-paper/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload)
        except Exception as exc:  # pragma: no cover
            print(f"REST fallback fetch failed for {url}: {exc}")
            return None

    def _fallback_rest_poll(self) -> None:
        symbol = cfg.SYMBOL
        ticker_url = f"{REST_BASE}/v2/tickers/{symbol}"
        data = self._rest_get_json(ticker_url)
        if data and isinstance(data, dict):
            result = data.get("result")
            if isinstance(result, dict):
                price = float(result.get("close") or result.get("last_price") or result.get("mark_price") or 0.0)
                self._save_snapshot(
                    symbol=symbol,
                    price=price,
                    bid=float(result.get("bid") or 0.0),
                    ask=float(result.get("ask") or 0.0),
                    mark_price=float(result.get("mark_price") or price),
                    volume=float(result.get("volume") or 0.0),
                    oi=float(result.get("oi_value") or result.get("open_interest") or 0.0),
                    oi_change_6h=float(result.get("oi_change_usd_6h") or 0.0),
                    funding_rate=float(result.get("funding_rate") or 0.0) if result.get("funding_rate") is not None else None,
                    last_trade_side=str(result.get("side") or ""),
                )

        end = int(time.time())
        lookback_hours = {"1m": 8, "3m": 24, "5m": 48, "15m": 96, "1h": 240}
        for resolution, interval in RESOLUTIONS.items():
            hours = lookback_hours.get(interval, 48)
            start = int((now_ist() - timedelta(hours=hours)).timestamp())
            candles_url = (
                f"{REST_BASE}/v2/history/candles?symbol={symbol}"
                f"&resolution={resolution}&start={start}&end={end}&limit=500"
            )
            candle_payload = self._rest_get_json(candles_url)
            if not candle_payload or not isinstance(candle_payload, dict):
                continue
            rows = candle_payload.get("result") or []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                self._save_candle(
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "open": float(row.get("open") or 0.0),
                        "high": float(row.get("high") or 0.0),
                        "low": float(row.get("low") or 0.0),
                        "close": float(row.get("close") or 0.0),
                        "volume": float(row.get("volume") or 0.0),
                        "time": row.get("time"),
                        "closed": True,
                    }
                )

        tradebook_url = f"{REST_BASE}/v2/trades/{symbol}"
        trade_payload = self._rest_get_json(tradebook_url)
        if isinstance(trade_payload, dict) and isinstance(trade_payload.get("result"), dict):
            trades = trade_payload["result"].get("trades") or []
            for trade in trades[:10]:
                if isinstance(trade, dict):
                    self._save_trade(
                        symbol=symbol,
                        side=str(trade.get("side") or "buy").lower(),
                        price=float(trade.get("price") or 0.0),
                        quantity=float(trade.get("size") or trade.get("quantity") or 0.0),
                    )

        orderbook_url = f"{REST_BASE}/v2/l2orderbook/{symbol}?depth=10"
        orderbook_payload = self._rest_get_json(orderbook_url)
        if isinstance(orderbook_payload, dict) and isinstance(orderbook_payload.get("result"), dict):
            result = orderbook_payload["result"]
            bids = result.get("buy") or []
            asks = result.get("sell") or []
            if bids and asks:
                bid_payload = [[float(level.get("price", 0.0)), float(level.get("size", 0.0))] for level in bids[:10]]
                ask_payload = [[float(level.get("price", 0.0)), float(level.get("size", 0.0))] for level in asks[:10]]
                best_bid = float(bids[0].get("price", 0.0))
                best_ask = float(asks[0].get("price", 0.0))
                self._save_orderbook(
                    symbol=symbol,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    spread=best_ask - best_bid,
                    bids=json.dumps(bid_payload),
                    asks=json.dumps(ask_payload),
                )

    async def _handle_message(self, raw_message: str) -> None:
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return

        if not isinstance(payload, dict):
            return

        event_type = payload.get("type")
        channel = payload.get("channel") or payload.get("name") or event_type
        data = payload.get("data")
        if payload.get("payload") and isinstance(payload["payload"], dict):
            data = payload["payload"].get("data") or payload["payload"]
            channel = payload["payload"].get("channel") or payload["payload"].get("name") or channel

        if event_type in {"subscribe", "subscriptions", "pong", "auth"}:
            return

        if channel in {"ticker", "v2/ticker"} or (isinstance(event_type, str) and "ticker" in event_type):
            # Compact public feed OR legacy v2/ticker object
            ticker = payload.get("d")
            if isinstance(ticker, list):
                ticker = ticker[0] if ticker else {}
            if not isinstance(ticker, dict):
                ticker = payload if isinstance(payload.get("q"), list) else {}
            if isinstance(data, dict) and (data.get("mark_price") or data.get("close") or data.get("symbol")):
                symbol = str(data.get("symbol") or payload.get("symbol") or cfg.SYMBOL)
                price = float(data.get("close") or data.get("last_price") or data.get("mark_price") or 0.0)
                self._save_snapshot(
                    symbol=symbol,
                    price=price,
                    bid=float(data.get("bid") or data.get("best_bid") or 0.0),
                    ask=float(data.get("ask") or data.get("best_ask") or 0.0),
                    mark_price=float(data.get("mark_price") or price),
                    volume=float(data.get("volume") or 0.0),
                    oi=float(data.get("oi") or data.get("open_interest") or 0.0),
                    oi_change_6h=float(data.get("oi_change_usd_6h") or data.get("oi_change_6h") or 0.0),
                    funding_rate=float(data["funding_rate"]) if data.get("funding_rate") is not None else None,
                    last_trade_side=str(data.get("side") or ""),
                )
                return
            symbol = ticker.get("s") or payload.get("sy") or cfg.SYMBOL
            quotes = ticker.get("q") or []
            oi_info = ticker.get("oi") or []
            ohlc = ticker.get("ohlc") or []
            price = float(
                (ohlc[3] if len(ohlc) > 3 else None)
                or ticker.get("p")
                or ticker.get("m")
                or payload.get("close")
                or 0.0
            )
            bid = float(quotes[2]) if len(quotes) > 2 and quotes[2] is not None else float(payload.get("bid") or 0.0)
            ask = float(quotes[0]) if len(quotes) > 0 and quotes[0] is not None else float(payload.get("ask") or 0.0)
            oi = float(oi_info[0]) if len(oi_info) > 0 and oi_info[0] is not None else float(payload.get("oi") or 0.0)
            oi_change_6h = float(oi_info[1]) if len(oi_info) > 1 and oi_info[1] is not None else 0.0
            self._save_snapshot(
                symbol=symbol,
                price=price,
                bid=bid,
                ask=ask,
                mark_price=float(ticker.get("m") or payload.get("mark_price") or price),
                volume=float(payload.get("v") or ticker.get("v") or payload.get("volume") or 0.0),
                oi=oi,
                oi_change_6h=oi_change_6h,
                best_bid_size=float(quotes[3]) if len(quotes) > 3 and quotes[3] is not None else 0.0,
                best_ask_size=float(quotes[1]) if len(quotes) > 1 and quotes[1] is not None else 0.0,
                last_trade_side=str(payload.get("side") or ""),
            )
            return

        if channel in {"mark_price", "mark"} or (
            isinstance(event_type, str) and event_type.startswith("mark_price")
        ):
            symbol = (
                payload.get("sy")
                or payload.get("symbol")
                or (data.get("symbol") if isinstance(data, dict) else None)
                or cfg.SYMBOL
            )
            if isinstance(symbol, str) and symbol.startswith("MARK:"):
                symbol = symbol.replace("MARK:", "", 1)
            mark = (
                payload.get("p")
                or payload.get("mark_price")
                or payload.get("price")
                or (data.get("mark_price") if isinstance(data, dict) else None)
                or (data.get("price") if isinstance(data, dict) else None)
            )
            if mark is not None:
                self._save_snapshot(symbol=symbol, price=float(mark), mark_price=float(mark))
            return

        if channel in {"funding_rate", "funding"}:
            symbol = payload.get("sy") or payload.get("symbol") or cfg.SYMBOL
            rate = payload.get("funding_rate") or payload.get("rate") or (data.get("funding_rate") if isinstance(data, dict) else None)
            if rate is not None:
                self._save_snapshot(symbol=symbol, price=0.0, funding_rate=float(rate))
            return

        if channel in {"trades", "all_trades"}:
            items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            if not items and isinstance(payload.get("p"), str):
                items = [{
                    "symbol": payload.get("sy") or cfg.SYMBOL,
                    "side": "buy" if payload.get("r") == "m" else "sell",
                    "price": payload.get("p"),
                    "size": payload.get("s"),
                }]
            for item in items:
                if isinstance(item, dict):
                    symbol = item.get("symbol") or item.get("sy") or item.get("product_symbol") or cfg.SYMBOL
                    side = str(item.get("side") or ("buy" if item.get("r") == "m" else "sell") or "buy").lower()
                    self._save_trade(
                        symbol=symbol,
                        side=side,
                        price=float(item.get("price") or item.get("p") or 0.0),
                        quantity=float(item.get("size") or item.get("s") or item.get("quantity") or item.get("volume") or 0.0),
                    )
            return

        if channel in {"orderbook", "orderbook_l2", "book", "ob_l2", "ob_updates", "ob_l1"}:
            if isinstance(data, dict):
                symbol = data.get("symbol") or data.get("sy") or data.get("product_symbol") or cfg.SYMBOL
                bids = data.get("bids") or data.get("buy") or data.get("b") or []
                asks = data.get("asks") or data.get("sell") or data.get("a") or []
            else:
                symbol = payload.get("sy") or cfg.SYMBOL
                bids = payload.get("b") or []
                asks = payload.get("a") or []
            if bids and asks:
                def normalize_levels(levels):
                    normalized = []
                    for level in levels[:10]:
                        if isinstance(level, (list, tuple)):
                            if len(level) >= 2:
                                normalized.append([float(level[0]), float(level[1])])
                        elif isinstance(level, dict):
                            normalized.append([float(level.get("price", 0.0)), float(level.get("size", 0.0))])
                    return normalized
                bid_levels = normalize_levels(bids)
                ask_levels = normalize_levels(asks)
                if bid_levels and ask_levels:
                    self._save_orderbook(
                        symbol=symbol,
                        best_bid=bid_levels[0][0],
                        best_ask=ask_levels[0][0],
                        spread=ask_levels[0][0] - bid_levels[0][0],
                        bids=json.dumps(bid_levels),
                        asks=json.dumps(ask_levels),
                    )
            return

        if isinstance(channel, str) and (channel.startswith("candlestick_") or channel == "candlesticks"):
            interval = str(payload.get("res") or "1m")
            if channel.startswith("candlestick_"):
                interval = channel.replace("candlestick_", "") or interval
            if interval == "60m":
                interval = "1h"
            identifier = payload.get("sy") or payload.get("symbol") or payload.get("product_symbol") or cfg.SYMBOL
            timestamp = (
                payload.get("candle_start_time")
                or payload.get("ts")
                or payload.get("timestamp")
            )
            candle_time = None
            if timestamp is not None:
                raw_ts = float(timestamp)
                # Delta: seconds ~1e9, ms ~1e12, µs ~1e15, ns ~1e18
                if raw_ts >= 1e17:
                    timestamp_seconds = raw_ts / 1_000_000_000
                elif raw_ts >= 1e14:
                    timestamp_seconds = raw_ts / 1_000_000
                elif raw_ts >= 1e11:
                    timestamp_seconds = raw_ts / 1_000
                else:
                    timestamp_seconds = raw_ts
                candle_time = floor_candle_time(datetime.fromtimestamp(timestamp_seconds, IST), interval)
            # Forming bar until the bucket duration has elapsed
            closed = False
            if candle_time is not None:
                closed = (now_ist() - candle_time).total_seconds() >= interval_seconds(interval)
            self._save_candle(
                {
                    "symbol": identifier,
                    "interval": interval,
                    "close": float(payload.get("c") or payload.get("close") or 0.0),
                    "open": float(payload.get("o") or payload.get("open") or 0.0),
                    "high": float(payload.get("h") or payload.get("high") or 0.0),
                    "low": float(payload.get("l") or payload.get("low") or 0.0),
                    "volume": float(payload.get("v") or payload.get("volume") or 0.0),
                    "candle_time": candle_time,
                    "closed": closed,
                }
            )

    def _save_snapshot(
        self,
        symbol: str,
        price: float,
        bid: float = 0.0,
        ask: float = 0.0,
        mark_price: float = 0.0,
        volume: float = 0.0,
        oi: float = 0.0,
        oi_change_6h: float = 0.0,
        funding_rate: Optional[float] = None,
        best_bid_size: float = 0.0,
        best_ask_size: float = 0.0,
        last_trade_side: str = "",
    ) -> None:
        with SessionLocal() as session:
            entry = session.query(MarketSnapshot).filter(MarketSnapshot.symbol == symbol).order_by(MarketSnapshot.updated_at.desc()).first()
            snapshot = entry or MarketSnapshot(symbol=symbol)
            snapshot.symbol = symbol
            if price:
                snapshot.price = price
            snapshot.bid = bid or snapshot.bid
            snapshot.ask = ask or snapshot.ask
            snapshot.mark_price = mark_price or snapshot.mark_price
            snapshot.volume = volume or snapshot.volume
            snapshot.oi = oi or snapshot.oi
            snapshot.oi_change_6h = oi_change_6h if oi_change_6h else snapshot.oi_change_6h
            if funding_rate is not None:
                snapshot.funding_rate = funding_rate
            snapshot.best_bid_size = best_bid_size or snapshot.best_bid_size
            snapshot.best_ask_size = best_ask_size or snapshot.best_ask_size
            snapshot.last_trade_side = last_trade_side or snapshot.last_trade_side
            snapshot.source = "delta_ws"
            snapshot.updated_at = store_naive_ist()
            session.add(snapshot)
            session.commit()

    def _save_trade(self, symbol: str, side: str, price: float, quantity: float) -> None:
        with SessionLocal() as session:
            event = TradeEvent(symbol=symbol, side=side, price=price, quantity=quantity, trade_time=store_naive_ist())
            session.add(event)
            session.commit()

    def _save_orderbook(self, symbol: str, best_bid: float, best_ask: float, spread: float, bids: str, asks: str) -> None:
        with SessionLocal() as session:
            snapshot = OrderBookSnapshot(
                symbol=symbol,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                bids=bids,
                asks=asks,
                updated_at=store_naive_ist(),
            )
            session.add(snapshot)
            session.commit()

    def _save_candle(self, candle: Dict[str, Any]) -> None:
        candle_time = candle.get("candle_time")
        if candle_time is None and candle.get("time") is not None:
            # Exchange epoch is UTC; convert to IST wall clock for single-TZ storage
            candle_time = datetime.fromtimestamp(float(candle["time"]), UTC).astimezone(IST)
        if candle_time is None:
            raise ValueError("Candle payload is missing its exchange timestamp")
        if candle_time.tzinfo is None:
            candle_time = candle_time.replace(tzinfo=IST)
        else:
            candle_time = candle_time.astimezone(IST)
        interval = str(candle.get("interval") or "1m")
        if interval == "60m":
            interval = "1h"
        candle_time = floor_candle_time(candle_time, interval).replace(tzinfo=None)
        closed = bool(candle.get("closed", True))

        with SessionLocal() as session:
            symbol = str(candle["symbol"])
            item = (
                session.query(Candle)
                .filter(
                    Candle.symbol == symbol,
                    Candle.interval == interval,
                    Candle.candle_time == candle_time,
                )
                .first()
            )
            if item is None:
                item = Candle(symbol=symbol, interval=interval, candle_time=candle_time)
            item.open_price = float(candle.get("open") or 0.0)
            item.high = float(candle.get("high") or 0.0)
            item.low = float(candle.get("low") or 0.0)
            item.close = float(candle.get("close") or 0.0)
            item.volume = float(candle.get("volume") or 0.0)
            item.closed = closed
            session.add(item)
            # Mark older same-interval candles closed once a newer bucket arrives
            session.query(Candle).filter(
                Candle.symbol == symbol,
                Candle.interval == interval,
                Candle.candle_time < candle_time,
                Candle.closed.is_(False),
            ).update({"closed": True}, synchronize_session=False)
            session.commit()

        try:
            from app.ws_hubs import candle_hub
            from app.timeutil import iso_ist

            # lightweight-charts expects unix seconds
            ts = int(candle_time.replace(tzinfo=IST).timestamp())
            candle_hub.publish_sync(
                symbol,
                interval,
                {
                    "type": "candle",
                    "symbol": symbol,
                    "interval": interval,
                    "candle": {
                        "time": ts,
                        "open": float(candle.get("open") or 0.0),
                        "high": float(candle.get("high") or 0.0),
                        "low": float(candle.get("low") or 0.0),
                        "close": float(candle.get("close") or 0.0),
                        "volume": float(candle.get("volume") or 0.0),
                        "closed": closed,
                        "iso_time": iso_ist(candle_time),
                    },
                },
            )
        except Exception:
            pass
