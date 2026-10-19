import { useEffect, useRef } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  IPriceLine,
  CandlestickData,
  UTCTimestamp,
} from "lightweight-charts";
import { CandleBar } from "../../hooks/useCandleSocket";

export type ChartLevel = {
  label: string;
  price: number;
  side?: string;
};

type Props = {
  candles: CandleBar[];
  interval: string;
  onInterval: (tf: string) => void;
  connected: boolean;
  levels?: ChartLevel[];
};

const TF_BUTTONS = [
  { id: "1m", label: "1m" },
  { id: "3m", label: "3m" },
  { id: "5m", label: "5m" },
  { id: "15m", label: "15m" },
  { id: "1h", label: "1H" },
];

function toBar(c: CandleBar): CandlestickData | null {
  if (!c.time || !(c.open > 0) || !(c.high > 0) || !(c.low > 0) || !(c.close > 0)) return null;
  return {
    time: c.time as UTCTimestamp,
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
  };
}

function fmtPrice(n: number) {
  return n.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

export default function LiveChart({ candles, interval, onInterval, connected, levels = [] }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const seededRef = useRef(false);
  const lastLenRef = useRef(0);
  const lastTimeRef = useRef<number | null>(null);

  useEffect(() => {
    if (!hostRef.current) return;
    const chart = createChart(hostRef.current, {
      autoSize: true,
      layout: {
        background: { color: "#0a1018" },
        textColor: "#9fb0c7",
        fontFamily: "IBM Plex Mono, monospace",
      },
      grid: {
        vertLines: { color: "rgba(120,150,190,0.08)" },
        horzLines: { color: "rgba(120,150,190,0.08)" },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#00e6a0",
      downColor: "#ff4d6d",
      borderVisible: false,
      wickUpColor: "#00e6a0",
      wickDownColor: "#ff4d6d",
    });
    chartRef.current = chart;
    seriesRef.current = series;
    seededRef.current = false;
    lastLenRef.current = 0;
    lastTimeRef.current = null;
    return () => {
      priceLinesRef.current = [];
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Reset series when timeframe changes
  useEffect(() => {
    seededRef.current = false;
    lastLenRef.current = 0;
    lastTimeRef.current = null;
    seriesRef.current?.setData([]);
  }, [interval]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !candles.length) return;

    const data = candles.map(toBar).filter((b): b is CandlestickData => b != null);
    if (!data.length) return;

    const last = data[data.length - 1];
    const lastTime = last.time as number;
    const needFull =
      !seededRef.current ||
      data.length < lastLenRef.current ||
      (lastTimeRef.current != null && lastTime < lastTimeRef.current);

    if (needFull) {
      series.setData(data);
      chartRef.current?.timeScale().scrollToRealTime();
      seededRef.current = true;
    } else {
      try {
        series.update(last);
      } catch {
        series.setData(data);
      }
    }
    lastLenRef.current = data.length;
    lastTimeRef.current = lastTime;
  }, [candles]);

  // Draw R3–S2 (and any other) key levels as price lines
  const levelsKey = levels.map((l) => `${l.label}:${Number(l.price).toFixed(1)}`).join("|");
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;

    for (const line of priceLinesRef.current) {
      try {
        series.removePriceLine(line);
      } catch {
        /* ignore */
      }
    }
    priceLinesRef.current = [];

    for (const lv of levels) {
      const price = Number(lv.price);
      if (!Number.isFinite(price) || price <= 0) continue;
      const isRes = String(lv.side || "").toLowerCase().includes("res") || /^R\d/i.test(lv.label);
      const color = isRes ? "rgba(255, 77, 109, 0.85)" : "rgba(0, 230, 160, 0.85)";
      const line = series.createPriceLine({
        price,
        color,
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: `${lv.label}  $${fmtPrice(price)}`,
      });
      priceLinesRef.current.push(line);
    }
    // levelsKey captures price changes; levels read from closure
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [levelsKey]);

  return (
    <article className="panel chart-panel">
      <div className="panel-head">
        <div>
          <h2>BTCUSD Perpetual</h2>
          <p className="muted">
            {interval === "1h" ? "1H" : interval} · live candles · R/S levels{" "}
            <span className={connected ? "bull" : "bear"}>{connected ? "● LIVE" : "○ reconnecting"}</span>
          </p>
        </div>
        <div className="tf-pills">
          {TF_BUTTONS.map((tf) => (
            <button
              key={tf.id}
              className={`tf ${interval === tf.id ? "active" : ""}`}
              onClick={() => onInterval(tf.id)}
              type="button"
            >
              {tf.label}
            </button>
          ))}
        </div>
      </div>
      <div ref={hostRef} className="tv-chart" />
    </article>
  );
}
