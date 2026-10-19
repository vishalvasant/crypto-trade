"""WebSocket fan-out hubs for dashboard and candle streams."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket


class DashboardHub:
    """Per-user dashboard clients; each receives user-scoped snapshots."""

    def __init__(self) -> None:
        self.clients: Dict[int, Set[WebSocket]] = {}
        self.latest: Dict[int, Dict[str, Any]] = {}

    async def connect(self, websocket: WebSocket, user_id: int) -> None:
        await websocket.accept()
        self.clients.setdefault(user_id, set()).add(websocket)
        if user_id in self.latest:
            await websocket.send_text(json.dumps(self.latest[user_id], default=str))

    def disconnect(self, websocket: WebSocket, user_id: Optional[int] = None) -> None:
        if user_id is not None and user_id in self.clients:
            self.clients[user_id].discard(websocket)
            if not self.clients[user_id]:
                del self.clients[user_id]
            return
        for uid, group in list(self.clients.items()):
            if websocket in group:
                group.discard(websocket)
                if not group:
                    del self.clients[uid]

    def connected_user_ids(self) -> Set[int]:
        return set(self.clients.keys())

    async def send_user(self, user_id: int, payload: Dict[str, Any]) -> None:
        # Merge lightweight position ticks into the last full dashboard snapshot
        # so reconnect clients still get a coherent state.
        prev = self.latest.get(user_id)
        if payload.get("type") == "position_risk" and isinstance(prev, dict):
            merged = dict(prev)
            merged["type"] = "dashboard"
            merged["ist_now"] = payload.get("ist_now", merged.get("ist_now"))
            merged["server_ts"] = payload.get("server_ts", merged.get("server_ts"))
            if isinstance(payload.get("account"), dict):
                merged["account"] = payload["account"]
            if isinstance(payload.get("funding"), dict):
                merged["funding"] = payload["funding"]
            if isinstance(payload.get("symbol"), dict):
                sym = dict(merged.get("symbol") or {})
                incoming = payload["symbol"]
                for key in ("price", "mark_price", "bid", "ask", "updated_at", "funding", "position"):
                    if key in incoming:
                        sym[key] = incoming[key]
                merged["symbol"] = sym
            # Keep open-row P&L in the trades table in sync with live mark
            pos = (payload.get("symbol") or {}).get("position")
            if pos and isinstance(merged.get("positions"), list):
                rows = []
                for row in merged["positions"]:
                    if row.get("is_open"):
                        rows.append(
                            {
                                **row,
                                "mark_price": pos.get("mark_price", row.get("mark_price")),
                                "pnl": pos.get("pnl", row.get("pnl")),
                                "updated_at": pos.get("updated_at", row.get("updated_at")),
                            }
                        )
                    else:
                        rows.append(row)
                merged["positions"] = rows
            self.latest[user_id] = merged
            message = json.dumps(payload, default=str)
        else:
            self.latest[user_id] = payload
            message = json.dumps(payload, default=str)

        dead: list[WebSocket] = []
        for client in list(self.clients.get(user_id, set())):
            try:
                await client.send_text(message)
            except Exception:
                dead.append(client)
        for client in dead:
            self.disconnect(client, user_id)


class CandleHub:
    """Broadcast candle updates keyed by symbol:interval."""

    def __init__(self) -> None:
        self.clients: Dict[str, Set[WebSocket]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _key(self, symbol: str, interval: str) -> str:
        return f"{symbol}:{interval}"

    async def connect(self, websocket: WebSocket, symbol: str, interval: str) -> None:
        await websocket.accept()
        key = self._key(symbol, interval)
        self.clients.setdefault(key, set()).add(websocket)

    def disconnect(self, websocket: WebSocket, symbol: Optional[str] = None, interval: Optional[str] = None) -> None:
        if symbol and interval:
            key = self._key(symbol, interval)
            if key in self.clients:
                self.clients[key].discard(websocket)
                if not self.clients[key]:
                    del self.clients[key]
            return
        for key, group in list(self.clients.items()):
            if websocket in group:
                group.discard(websocket)
                if not group:
                    del self.clients[key]

    async def broadcast(self, symbol: str, interval: str, payload: Dict[str, Any]) -> None:
        key = self._key(symbol, interval)
        dead: list[WebSocket] = []
        message = json.dumps(payload, default=str)
        for client in list(self.clients.get(key, set())):
            try:
                await client.send_text(message)
            except Exception:
                dead.append(client)
        for client in dead:
            self.disconnect(client, symbol, interval)

    def publish_sync(self, symbol: str, interval: str, payload: Dict[str, Any]) -> None:
        """Thread-safe publish from market_ws worker thread."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(symbol, interval, payload), loop)


dashboard_hub = DashboardHub()
candle_hub = CandleHub()
