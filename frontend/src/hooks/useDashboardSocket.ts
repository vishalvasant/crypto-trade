import { useEffect, useRef, useState } from "react";
import { getToken, wsUrl } from "../lib/api";

export type DashboardState = Record<string, any>;

function mergePositionRisk(prev: DashboardState | null, tick: DashboardState): DashboardState {
  const base: DashboardState = prev ? { ...prev } : { type: "dashboard", live: true };
  base.ist_now = tick.ist_now ?? base.ist_now;
  base.server_ts = tick.server_ts ?? base.server_ts;
  base.live = true;
  if (tick.account) base.account = tick.account;
  if (tick.funding) base.funding = tick.funding;
  if (tick.symbol) {
    const sym = { ...(base.symbol || {}), ...tick.symbol };
    // Preserve non-tick fields from prior snapshot when tick omits them
    if (prev?.symbol) {
      for (const key of ["oi", "oi_change_6h", "volume", "high_24h", "low_24h", "funding_rate", "last_trade_side", "last_signal"]) {
        if (sym[key] == null && prev.symbol[key] != null) sym[key] = prev.symbol[key];
      }
    }
    base.symbol = sym;
  }
  const pos = tick.symbol?.position;
  if (pos && Array.isArray(base.positions)) {
    base.positions = base.positions.map((row: any) =>
      row?.is_open
        ? {
            ...row,
            mark_price: pos.mark_price ?? row.mark_price,
            pnl: pos.pnl ?? row.pnl,
            updated_at: pos.updated_at ?? row.updated_at,
          }
        : row
    );
  }
  return base;
}

export function useDashboardSocket(enabled: boolean) {
  const [data, setData] = useState<DashboardState | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(1000);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: number | undefined;

    const connect = () => {
      const token = getToken();
      if (!token || cancelled) return;
      const ws = new WebSocket(wsUrl("/ws/dashboard", { token }));
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        retryRef.current = 1000;
        ws.send("refresh");
      };
      ws.onmessage = (ev) => {
        try {
          const payload = JSON.parse(ev.data);
          if (payload?.type === "pong") return;
          if (payload?.type === "position_risk") {
            setData((prev) => mergePositionRisk(prev, payload));
            return;
          }
          setData(payload);
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) {
          timer = window.setTimeout(connect, retryRef.current);
          retryRef.current = Math.min(retryRef.current * 1.6, 8000);
        }
      };
      ws.onerror = () => ws.close();
    };

    connect();
    const ping = window.setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send("ping");
    }, 15000);

    return () => {
      cancelled = true;
      window.clearInterval(ping);
      if (timer) window.clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [enabled]);

  const refresh = () => {
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send("refresh");
  };

  return { data, connected, refresh };
}
