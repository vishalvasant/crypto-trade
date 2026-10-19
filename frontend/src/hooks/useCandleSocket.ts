import { useEffect, useRef, useState } from "react";
import { getToken, wsUrl } from "../lib/api";

export type CandleBar = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  closed?: boolean;
};

export function useCandleSocket(enabled: boolean, interval: string, symbol = "BTCUSD") {
  const [candles, setCandles] = useState<CandleBar[]>([]);
  const [connected, setConnected] = useState(false);
  const [liveBar, setLiveBar] = useState<CandleBar | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: number | undefined;
    let pingTimer: number | undefined;
    let retry = 1000;

    setCandles([]);
    setLiveBar(null);
    setConnected(false);

    const connect = () => {
      const token = getToken();
      if (!token || cancelled) return;
      const ws = new WebSocket(wsUrl("/ws/candles", { token, symbol, interval, limit: "300" }));
      wsRef.current = ws;
      ws.onopen = () => {
        setConnected(true);
        retry = 1000;
        if (pingTimer) window.clearInterval(pingTimer);
        pingTimer = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send("ping");
        }, 20000);
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "pong") return;
          if (msg.type === "history") {
            const bars = (msg.candles || []) as CandleBar[];
            setCandles(bars);
            setLiveBar(bars.length ? bars[bars.length - 1] : null);
          } else if (msg.type === "candle" && msg.candle) {
            const bar = msg.candle as CandleBar;
            setLiveBar(bar);
            setCandles((prev) => {
              if (!prev.length) return [bar];
              const last = prev[prev.length - 1];
              if (last.time === bar.time) {
                const next = prev.slice(0, -1);
                next.push(bar);
                return next;
              }
              if (bar.time > last.time) return [...prev, bar];
              return prev;
            });
          }
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (pingTimer) window.clearInterval(pingTimer);
        if (!cancelled) {
          timer = window.setTimeout(connect, retry);
          retry = Math.min(retry * 1.6, 8000);
        }
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
      if (pingTimer) window.clearInterval(pingTimer);
      wsRef.current?.close();
    };
  }, [enabled, interval, symbol]);

  return { candles, liveBar, connected };
}
