import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { useDashboardSocket } from "../hooks/useDashboardSocket";
import { useCandleSocket } from "../hooks/useCandleSocket";
import LiveChart from "../components/chart/LiveChart";
import { api } from "../lib/api";
import { fmt, fmtCompact, fmtPct, fmtTimeIST, sideClass } from "../lib/format";
import { liveEquity, liveUnrealizedPnl } from "../lib/positionMath";

const NAV = {
  Trading: [
    { id: "overview", label: "Overview", ico: "▣", href: "#overview" },
    { id: "live-market", label: "Live Market", ico: "◎", href: "#live-market" },
    { id: "signals", label: "Signals", ico: "⚡", href: "#signals" },
    { id: "position", label: "Open Position", ico: "◫", href: "#position" },
    { id: "trades", label: "Trade Book", ico: "☰", href: "#trades" },
  ],
  Analysis: [
    { id: "market-data", label: "Market Data", ico: "▦", href: "#live-market" },
    { id: "orderbook", label: "Order Book", ico: "▤", href: "#levels" },
    { id: "oi", label: "Open Interest", ico: "◉", href: "#live-market" },
    { id: "funding", label: "Funding Rate", ico: "$", href: "#live-market" },
    { id: "liq", label: "Liquidations", ico: "↯", href: "#levels" },
    { id: "mtf", label: "Multi Timeframe", ico: "⏱", href: "#regime" },
  ],
  Configuration: [
    { id: "strategy", label: "Strategy Settings", ico: "⚙", href: "#signals" },
    { id: "risk", label: "Risk Management", ico: "🛡", href: "#position" },
    { id: "exchange", label: "Exchange (Delta)", ico: "Δ", href: "#overview" },
    { id: "notify", label: "Notifications", ico: "🔔", href: "#trades" },
  ],
};

const TOP_TABS = ["Dashboard", "Strategy", "Live Trade", "Backtest", "Trade Book", "Analytics", "Settings"];

function clamp(n: number, a: number, b: number) {
  return Math.max(a, Math.min(b, n));
}

function signalMessage(signal: any, side: string) {
  const reject = signal.rejection_reason;
  const setup = signal.setup_name;
  if (side === "LONG" || side === "SHORT") {
    return setup ? `${String(setup).replaceAll("_", " ")} · live signal` : "Validated setup";
  }
  if (reject === "WATCHLIST_SCORE_60_74") return "Watchlist — waiting for confirmation";
  if (reject === "POSITION_ALREADY_OPEN") return "Position open — managing trade";
  if (reject === "LOSS_COOLDOWN") return "Loss cooldown active (IST)";
  if (reject === "NO_VALID_SETUP") {
    const dist = signal.dist_atr != null ? ` · ${Number(signal.dist_atr).toFixed(2)} ATR from level` : "";
    return `Scanning setups${dist}`;
  }
  if (reject === "SETUP_SCORE_BELOW_60") return "Setup forming — score below watch threshold";
  if (reject) return String(reject).replaceAll("_", " ").toLowerCase();
  return "No strong setup.";
}

export default function DashboardPage() {
  const { user, logout, token } = useAuth();
  const { data, connected, refresh } = useDashboardSocket(!!token);
  const [interval, setIntervalTf] = useState("5m");
  const { candles, connected: chartLive } = useCandleSocket(!!token, interval);
  const [levelsTab, setLevelsTab] = useState<"sr" | "liq" | "ob">("sr");
  const [activeNav, setActiveNav] = useState("overview");
  const [activeTop, setActiveTop] = useState("Dashboard");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const lastPriceRef = useRef<number | null>(null);
  const [priceFlash, setPriceFlash] = useState<{ abs: number; pct: number; up: boolean } | null>(null);
  const [pnlFlash, setPnlFlash] = useState<"up" | "down" | null>(null);
  const lastPnlRef = useRef<number | null>(null);

  const symbol = data?.symbol || {};
  const account = data?.account || {};
  const decision = data?.decision || {};
  const orderbook = data?.orderbook || {};
  const positions = data?.positions || [];
  const signal = decision.signal || {};
  const levels = decision.levels || [];
  const checklist = decision.checklist || [];
  const mark = Number(symbol.mark_price || symbol.price || 0);
  const setupScore = Number(signal.score ?? decision.feature?.setup_score ?? 0);
  const scoreKind = String(signal.live_score_kind || "");
  const breakdown = signal.score_breakdown || {};
  const pos = symbol.position;
  const feeRate = Number(data?.config?.taker_fee_rate ?? account.taker_fee_rate ?? 0.0005);
  const liveMark = mark || Number(pos?.mark_price || 0);
  const livePosPnl = useMemo(
    () => liveUnrealizedPnl(pos, liveMark, feeRate),
    [pos, liveMark, feeRate]
  );

  useEffect(() => {
    if (!mark) return;
    const prev = lastPriceRef.current;
    if (prev != null && prev > 0 && mark !== prev) {
      const abs = mark - prev;
      const pct = (abs / prev) * 100;
      setPriceFlash({ abs, pct, up: mark >= prev });
    }
    lastPriceRef.current = mark;
  }, [mark]);

  useEffect(() => {
    if (!pos?.is_open) {
      lastPnlRef.current = null;
      return;
    }
    const prev = lastPnlRef.current;
    if (prev != null && Math.abs(livePosPnl - prev) > 0.0005) {
      setPnlFlash(livePosPnl >= prev ? "up" : "down");
      const t = window.setTimeout(() => setPnlFlash(null), 450);
      lastPnlRef.current = livePosPnl;
      return () => window.clearTimeout(t);
    }
    lastPnlRef.current = livePosPnl;
  }, [livePosPnl, pos?.is_open]);

  const evaluate = async () => {
    await api("/api/generate-signal/BTCUSD", { method: "POST" });
    refresh();
  };

  const score = Number(signal.regime_score ?? decision.feature?.regime_score ?? 0);
  const state = String(signal.regime_state || decision.feature?.regime_state || "RANGE").toUpperCase();
  const biasClass = state.includes("BULL") ? "bull" : state.includes("BEAR") ? "bear" : "wait";
  const biasLabel =
    state === "RANGE" ? "Neutral" : state.charAt(0) + state.slice(1).toLowerCase();
  const conf = clamp(Math.abs(score), 0, 100);
  const side = String(signal.side || "WAIT").toUpperCase();
  const orb = side === "BUY" ? "LONG" : side === "SELL" ? "SHORT" : side;
  const dailyPnl = Number(account.daily_realized_pnl || 0);
  const unreal = pos?.is_open ? livePosPnl : Number(account.unrealized_pnl || 0);
  const equity = pos?.is_open
    ? liveEquity(
        Number(account.initial_balance || 250),
        Number(account.realized_pnl || 0),
        livePosPnl
      )
    : Number(account.equity || 0);
  const dailyStart = Number(account.daily_starting_equity || account.initial_balance || 250);
  // Daily loss meter uses realized + live unrealized so open drawdown counts
  const dailyCombined = dailyPnl + (pos?.is_open ? Math.min(0, livePosPnl) : 0);
  const dailyLossUsed = dailyCombined < 0 ? Math.abs(dailyCombined) / dailyStart : 0;
  const maxDaily = 0.03;
  const riskPct = Number(account.risk_per_trade || 0.01);
  const bids = orderbook.bids || [];
  const asks = orderbook.asks || [];
  const bid = Number(symbol.bid || 0);
  const ask = Number(symbol.ask || 0);
  const mid = bid && ask ? (bid + ask) / 2 : mark;

  let longPct = 50;
  const tradeSide = String(symbol.last_trade_side || "").toLowerCase();
  const oiChg = Number(symbol.oi_change_6h || 0);
  if (tradeSide.includes("buy")) longPct += 6;
  if (tradeSide.includes("sell")) longPct -= 6;
  if (oiChg > 0) longPct += 4;
  if (oiChg < 0) longPct -= 4;
  longPct = clamp(longPct, 35, 65);
  const shortPct = 100 - longPct;
  const imbalance = Number(orderbook.imbalance_pct || 0);

  const oi = Number(symbol.oi || 0);
  let oiChgLabel = "";
  let oiChgClass = "";
  if (oi > 0 && oiChg && Math.abs(oiChg / oi) >= 0.001) {
    const pct = (oiChg / oi) * 100;
    oiChgLabel = `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}% 6h`;
    oiChgClass = pct >= 0 ? "bull" : "bear";
  }

  const regimeCells = [
    { tf: "15m", bias: score <= -20 ? "Bearish" : score >= 20 ? "Bullish" : "Sideways" },
    { tf: "5m", bias: score <= -15 ? "Bearish" : score >= 15 ? "Bullish" : "Sideways" },
    { tf: "3m", bias: score <= -10 ? "Bearish" : score >= 10 ? "Bullish" : "Sideways" },
    { tf: "1m", bias: score <= -5 ? "Bearish" : score >= 5 ? "Bullish" : "Sideways" },
  ];

  const goNav = (item: { id: string; href: string }) => {
    setActiveNav(item.id);
    setSidebarOpen(false);
    if (item.id === "orderbook" || item.id === "liq") {
      setLevelsTab(item.id === "orderbook" ? "ob" : "liq");
    }
    const el = document.querySelector(item.href);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`} id="sidebar">
        <div className="brand">
          <span className="brand-mark">AT</span>
          <div>
            <strong>AutoTrade</strong>
            <span>Delta Futures</span>
          </div>
        </div>

        <nav className="side-nav">
          {Object.entries(NAV).map(([group, items]) => (
            <div key={group}>
              <p className="nav-group">{group}</p>
              {items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`nav-item ${activeNav === item.id ? "active" : ""}`}
                  onClick={() => goNav(item)}
                >
                  <span className="nav-ico">{item.ico}</span> {item.label}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="user-row">
          {user?.email}
          <div style={{ marginTop: 8 }}>
            <button type="button" className="btn-ghost sm" onClick={logout}>
              Logout
            </button>
          </div>
        </div>
      </aside>

      <div className="main-wrap">
        <header className="top-header">
          <button
            className="icon-btn menu-toggle"
            aria-label="Toggle menu"
            type="button"
            onClick={() => setSidebarOpen((v) => !v)}
          >
            ☰
          </button>
          <div className="top-tabs">
            {TOP_TABS.map((tab) => (
              <button
                key={tab}
                type="button"
                className={`top-tab ${activeTop === tab ? "active" : ""}`}
                onClick={() => setActiveTop(tab)}
              >
                {tab}
              </button>
            ))}
          </div>
          <div className="header-actions">
            <button className="pill pill-purple" type="button">
              Paper · IST
            </button>
            <button
              className={`pill pill-live ${connected ? "" : "offline"}`}
              type="button"
              title="WebSocket live feed"
            >
              <span className="live-dot" />
              {connected ? "Live WS" : "Reconnecting"}
            </button>
            <button className="btn-ghost" type="button" onClick={refresh}>
              Refresh
            </button>
            <button
              className="btn-ghost"
              type="button"
              title="Reset paper balance, positions and P&L"
              onClick={async () => {
                if (!window.confirm("Reset paper account to $250 and clear trades?")) return;
                await api("/api/reset-paper", { method: "POST" });
                refresh();
              }}
            >
              Reset Paper
            </button>
            <button className="btn-primary" type="button" onClick={evaluate}>
              Evaluate
            </button>
          </div>
        </header>

        <main className="dashboard" id="overview">
          <section className="panel asset-header">
            <div className="asset-left">
              <div className="btc-icon">₿</div>
              <div>
                <h1>
                  BTCUSD <span>Futures · Delta Exchange</span>
                </h1>
                <div className="price-row">
                  <strong className={`price-xl ${priceFlash ? (priceFlash.up ? "bull" : "bear") : "bull"}`}>
                    ${fmt(mark, 1)}
                  </strong>
                  <span className={`chg ${priceFlash ? (priceFlash.up ? "bull" : "bear") : "bull"}`}>
                    {priceFlash
                      ? `${priceFlash.abs >= 0 ? "+" : ""}${fmt(priceFlash.abs, 1)} (${fmtPct(priceFlash.pct)})`
                      : "+0.0 (0.00%)"}
                  </span>
                </div>
              </div>
            </div>
            <div className="asset-stats">
              <div className="mini-stat">
                <span>24h High</span>
                <strong>${fmt(symbol.high_24h, 1)}</strong>
              </div>
              <div className="mini-stat">
                <span>24h Low</span>
                <strong>${fmt(symbol.low_24h, 1)}</strong>
              </div>
              <div className="mini-stat">
                <span>24h Volume</span>
                <strong>{fmtCompact(symbol.volume)}</strong>
              </div>
              <div className="mini-stat">
                <span>Open Interest</span>
                <strong>{fmtCompact(symbol.oi)}</strong>
                {oiChgLabel && <em className={oiChgClass}>{oiChgLabel}</em>}
              </div>
              <div className="mini-stat">
                <span>Funding Rate</span>
                <strong className={Number(symbol.funding_rate) >= 0 ? "bull" : "bear"}>
                  {symbol.funding_rate == null ? "—" : fmtPct(Number(symbol.funding_rate), 4)}
                </strong>
              </div>
            </div>
          </section>

          <section className="grid-chart-row">
            <LiveChart
              candles={candles}
              interval={interval}
              onInterval={setIntervalTf}
              connected={chartLive}
              levels={levels}
            />

            <div className="right-stack">
              <article className="panel" id="regime">
                <div className="panel-head">
                  <h2>Market Regime</h2>
                  <span className={`chip ${biasClass}`}>Score {score.toFixed(0)}</span>
                </div>
                <div className="regime-grid">
                  {regimeCells.map((c) => (
                    <div className="regime-cell" key={c.tf}>
                      <span>{c.tf}</span>
                      <strong
                        className={
                          c.bias === "Bullish" ? "bull" : c.bias === "Bearish" ? "bear" : ""
                        }
                      >
                        {c.bias}
                      </strong>
                    </div>
                  ))}
                </div>
                <div className="bias-block">
                  <div>
                    <span className="muted">Overall Bias</span>
                    <strong className={`bias-text ${biasClass}`}>{biasLabel}</strong>
                  </div>
                  <div className="confidence">
                    <div className="conf-meta">
                      <span>Confidence</span>
                      <span>{conf.toFixed(0)}%</span>
                    </div>
                    <div className="bar">
                      <i style={{ width: `${conf}%` }} />
                    </div>
                  </div>
                </div>
              </article>

              <article className="panel" id="levels">
                <div className="panel-head">
                  <h2>Key Levels</h2>
                </div>
                <div className="tabs" id="levelsTabs">
                  {(
                    [
                      ["sr", "Support / Resistance"],
                      ["liq", "Liquidation Heatmap"],
                      ["ob", "Order Book"],
                    ] as const
                  ).map(([id, label]) => (
                    <button
                      key={id}
                      type="button"
                      className={`tab ${levelsTab === id ? "active" : ""}`}
                      onClick={() => setLevelsTab(id)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {levelsTab === "sr" && (
                  <div className="levels-table">
                    {!levels.length && <p className="muted">Collecting levels…</p>}
                    {levels.map((lv: any) => {
                      const isRes = lv.side === "resistance";
                      const pct = clamp(Number(lv.strength || 0) * 100, 8, 100);
                      return (
                        <div className="level-row" key={`${lv.label}-${lv.price}`}>
                          <span className={`lbl ${isRes ? "res" : "sup"}`}>{lv.label}</span>
                          <span className="px">${fmt(lv.price, 1)}</span>
                          <div className={`strength ${isRes ? "res" : "sup"}`}>
                            <i style={{ width: `${pct}%` }} />
                          </div>
                          <span className="bell">🔔</span>
                        </div>
                      );
                    })}
                  </div>
                )}
                {levelsTab === "liq" && (
                  <div className="levels-table">
                    {levels.slice(0, 6).map((lv: any) => {
                      const isRes = lv.side === "resistance";
                      const dens = clamp(Number(lv.strength || 0.5) * 100, 20, 100);
                      return (
                        <div className="level-row" key={`liq-${lv.price}`}>
                          <span className={`lbl ${isRes ? "res" : "sup"}`}>
                            {isRes ? "Shorts" : "Longs"}
                          </span>
                          <span className="px">${fmt(lv.price, 1)}</span>
                          <div className={`strength ${isRes ? "res" : "sup"}`}>
                            <i style={{ width: `${dens}%` }} />
                          </div>
                          <span className="muted" style={{ fontSize: 10 }}>
                            est.
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
                {levelsTab === "ob" && (
                  <div className="ob-depth">
                    <div className="ob-col">
                      <h4>Bids</h4>
                      {Array.from({ length: 8 }).map((_, i) => {
                        const b = bids[i] || [0, 0];
                        return (
                          <div className="ob-row bid" key={`b${i}`}>
                            <strong>{fmt(b[0], 1)}</strong>
                            <span>{fmt(b[1], 2)}</span>
                          </div>
                        );
                      })}
                    </div>
                    <div className="ob-col">
                      <h4>Asks</h4>
                      {Array.from({ length: 8 }).map((_, i) => {
                        const a = asks[i] || [0, 0];
                        return (
                          <div className="ob-row ask" key={`a${i}`}>
                            <strong>{fmt(a[0], 1)}</strong>
                            <span>{fmt(a[1], 2)}</span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </article>
            </div>
          </section>

          <section className="grid-mid-row">
            <article className="panel" id="live-market">
              <div className="panel-head">
                <h2>Live Market Data</h2>
              </div>
              <div className="market-grid">
                <div className="m-card">
                  <span>Mark Price</span>
                  <strong>${fmt(mark, 1)}</strong>
                </div>
                <div className="m-card">
                  <span>Index / Mid</span>
                  <strong>${fmt(mid, 1)}</strong>
                </div>
                <div className="m-card">
                  <span>24h Volume</span>
                  <strong>{fmtCompact(symbol.volume)}</strong>
                </div>
                <div className="m-card">
                  <span>Open Interest</span>
                  <strong>{fmtCompact(symbol.oi)}</strong>
                </div>
                <div className="m-card">
                  <span>Funding Rate</span>
                  <strong>
                    {symbol.funding_rate == null ? "—" : fmtPct(Number(symbol.funding_rate), 4)}
                  </strong>
                </div>
                <div className="m-card">
                  <span>Next Funding (IST)</span>
                  <strong title={symbol.funding?.next_funding_label || ""}>
                    {symbol.funding?.countdown || "—"}
                  </strong>
                </div>
                <div className="m-card">
                  <span>Best Bid / Ask</span>
                  <strong>
                    {fmt(bid, 1)} / {fmt(ask, 1)}
                  </strong>
                </div>
                <div className="m-card">
                  <span>Timezone</span>
                  <strong>IST · Asia/Kolkata</strong>
                </div>
              </div>
              <div className="ratio-block">
                <div className="ratio-row">
                  <span>Long / Short ratio</span>
                  <strong>
                    {longPct.toFixed(1)}% / {shortPct.toFixed(1)}%
                  </strong>
                </div>
                <div className="dual-bar">
                  <i className="long" style={{ width: `${longPct}%` }} />
                  <i className="short" style={{ width: `${shortPct}%` }} />
                </div>
                <div className="ratio-row">
                  <span>Order Book Imbalance</span>
                  <strong className={imbalance >= 0 ? "bull" : "bear"}>
                    {imbalance >= 0 ? "+" : ""}
                    {Number(imbalance).toFixed(1)}%
                  </strong>
                </div>
                <div className={`bar accent ${imbalance < 0 ? "danger" : ""}`}>
                  <i style={{ width: `${clamp(Math.abs(imbalance), 5, 100)}%` }} />
                </div>
              </div>
            </article>

            <article className="panel signal-panel" id="signals">
              <div className="panel-head">
                <h2>AI / Strategy Signal</h2>
                <span className={`chip ${connected ? "bull" : ""}`}>
                  {connected ? "● live" : "○ off"}
                </span>
              </div>
              <div className="signal-hero">
                <div className={`signal-orb ${sideClass(orb)}`}>{orb}</div>
                <p className="signal-msg">{signalMessage(signal, orb)}</p>
                <div className="live-score-block">
                  <div className="live-score-row">
                    <span>{scoreKind === "setup" ? "Setup score" : "Live readiness"}</span>
                    <strong className={setupScore >= 75 ? "bull" : setupScore >= 60 ? "wait" : ""}>
                      {setupScore.toFixed(0)}
                      <small>/100</small>
                    </strong>
                  </div>
                  <div className="bar accent score-bar">
                    <i
                      style={{
                        width: `${clamp(setupScore, 0, 100)}%`,
                        background:
                          setupScore >= 75
                            ? "var(--green)"
                            : setupScore >= 60
                              ? "var(--yellow)"
                              : "var(--blue)",
                      }}
                    />
                  </div>
                  <div className="score-thresholds">
                    <span>watch 60</span>
                    <span>entry 75</span>
                  </div>
                </div>
                <div className="signal-meta">
                  <span className="chip">
                    Regime {Number(signal.regime_score ?? score).toFixed(0)}
                  </span>
                  {signal.setup_name && (
                    <span className="chip">{String(signal.setup_name).replaceAll("_", " ")}</span>
                  )}
                  {signal.stop != null && <span className="chip">SL {fmt(signal.stop, 1)}</span>}
                  {signal.target != null && <span className="chip">TP {fmt(signal.target, 1)}</span>}
                  {signal.reward_risk != null && (
                    <span className="chip">R:R {Number(signal.reward_risk).toFixed(2)}</span>
                  )}
                </div>
                {!!Object.keys(breakdown).length && (
                  <div className="score-breakdown">
                    {Object.entries(breakdown).map(([k, v]) => (
                      <span key={k}>
                        {k.replaceAll("_", " ")} <b>{Number(v)}</b>
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <div className="checklist">
                {(checklist.length
                  ? checklist
                  : [
                      { label: "Regime aligned", ok: false },
                      { label: "Near key level", ok: false },
                      { label: "Volume confirm", ok: false },
                      { label: "Risk:Reward OK", ok: false },
                      { label: "Funding neutral", ok: false },
                      { label: "Liquidity OK", ok: false },
                    ]
                ).map((c: any) => (
                  <div className="check-item" key={c.label}>
                    <span className={`check-ico ${c.ok ? "ok" : "no"}`}>{c.ok ? "✓" : "✕"}</span>
                    <span>{c.label}</span>
                  </div>
                ))}
              </div>
            </article>

            <article className="panel" id="position">
              <div className="panel-head">
                <h2>Position &amp; Risk</h2>
                <div className="pos-head-chips">
                  <span className={`chip ${connected ? "bull" : ""}`}>
                    {connected ? "● live WS" : "○ reconnecting"}
                  </span>
                  <span className={`chip ${pos?.is_open ? sideClass(pos.side) : ""}`}>
                    {pos?.is_open ? `OPEN · ${pos.side}` : "No Open Position"}
                  </span>
                </div>
              </div>
              <div className="position-body">
                {pos?.is_open ? (
                  <div className="pos-card">
                    <div className="row">
                      <span>Side / Lots</span>
                      <strong className={sideClass(pos.side)}>
                        {pos.side} · {fmt(pos.size, 0)}
                      </strong>
                    </div>
                    <div className="row">
                      <span>Entry</span>
                      <strong>${fmt(pos.entry_price, 1)}</strong>
                    </div>
                    <div className="row">
                      <span>Mark (live)</span>
                      <strong>${fmt(liveMark, 1)}</strong>
                    </div>
                    <div className="row">
                      <span>Stop / Target</span>
                      <strong>
                        ${fmt(pos.trailing_stop ?? pos.stop_price, 1)} / ${fmt(pos.target_price, 1)}
                      </strong>
                    </div>
                    <div className="row">
                      <span>Margin · Lev</span>
                      <strong>
                        ${fmt(pos.margin, 2)} · {fmt(pos.leverage, 0)}×
                      </strong>
                    </div>
                    <div className="row">
                      <span>Unrealized P&amp;L</span>
                      <strong
                        className={`pnl-live ${livePosPnl >= 0 ? "bull" : "bear"} ${
                          pnlFlash === "up" ? "flash-up" : pnlFlash === "down" ? "flash-down" : ""
                        }`}
                      >
                        ${fmt(livePosPnl, 2)}
                      </strong>
                    </div>
                    <div className="row">
                      <span>Setup</span>
                      <strong>{(pos.setup_name || "—").replaceAll("_", " ")}</strong>
                    </div>
                    <div className="row">
                      <span>Opened (IST)</span>
                      <strong>{fmtTimeIST(pos.opened_at || pos.updated_at)}</strong>
                    </div>
                  </div>
                ) : (
                  <div className="pos-empty">
                    Flat · waiting for next qualified setup
                    <div className="muted" style={{ marginTop: 8, fontSize: 11 }}>
                      Live mark ${fmt(liveMark || mark, 1)} · equity ${fmt(equity, 2)}
                    </div>
                  </div>
                )}
              </div>

              <div className="risk-meters">
                <div>
                  <div className="conf-meta">
                    <span>Max Daily Loss</span>
                    <span>
                      {(dailyLossUsed * 100).toFixed(2)}% / {(maxDaily * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="bar danger">
                    <i style={{ width: `${clamp((dailyLossUsed / maxDaily) * 100, 0, 100)}%` }} />
                  </div>
                </div>
                <div>
                  <div className="conf-meta">
                    <span>Risk per Trade</span>
                    <span>{(riskPct * 100).toFixed(2)}% · engine sized</span>
                  </div>
                  <div className="bar warn">
                    <i style={{ width: "100%" }} />
                  </div>
                </div>
              </div>

              <p className="pos-auto-note muted">
                Paper fills are automatic when a setup scores ≥75 and risk checks pass (Requ).
                Size, stop and target come from the decision engine — not manual entry.
              </p>

              <div className="account-grid">
                <div className="a-card">
                  <span>Daily P&amp;L</span>
                  <strong className={dailyPnl >= 0 ? "bull" : "bear"}>${fmt(dailyPnl, 2)}</strong>
                </div>
                <div className="a-card">
                  <span>Unrealized</span>
                  <strong className={unreal >= 0 ? "bull" : "bear"}>${fmt(unreal, 2)}</strong>
                </div>
                <div className="a-card">
                  <span>Closed / Win Rate</span>
                  <strong>
                    {decision.stats?.total_closed_trades || 0} · {fmt(decision.stats?.win_rate || 0, 1)}%
                  </strong>
                </div>
                <div className="a-card balance">
                  <span>Equity (IST)</span>
                  <strong>${fmt(equity, 2)}</strong>
                </div>
              </div>
            </article>
          </section>

          <section className="panel trades-panel" id="trades">
            <div className="panel-head">
              <h2>
                Recent Trades <span className="muted">Paper Trading</span>
              </h2>
              <span className="chip chip-blue">{positions.length} trades</span>
            </div>
            <div className="table-wrap">
              <table className="trades-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Time</th>
                    <th>Side</th>
                    <th>Entry</th>
                    <th>Exit / Mark</th>
                    <th>Qty (lots)</th>
                    <th>P&amp;L</th>
                    <th>Setup</th>
                    <th>Leverage</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {!positions.length && (
                    <tr>
                      <td colSpan={10} className="muted">
                        No paper trades yet.
                      </td>
                    </tr>
                  )}
                  {positions.slice(0, 25).map((p: any, idx: number) => {
                    const pnl = Number(p.pnl || p.realized_pnl || 0);
                    const reason = String(p.close_reason || "").toLowerCase();
                    let tag = "other";
                    let tagLabel = p.is_open
                      ? "OPEN"
                      : (p.close_reason || "CLOSED").replaceAll("_", " ");
                    if (p.is_open) tag = "open";
                    else if (reason.includes("target")) {
                      tag = "tp";
                      tagLabel = "TP";
                    } else if (reason.includes("stop")) {
                      tag = "sl";
                      tagLabel = "SL";
                    }
                    return (
                      <tr key={p.id}>
                        <td>{idx + 1}</td>
                        <td>{fmtTimeIST(p.closed_at || p.updated_at || p.opened_at)}</td>
                        <td>
                          <span className={`side-pill ${sideClass(p.side)}`}>{p.side}</span>
                        </td>
                        <td>${fmt(p.entry_price, 1)}</td>
                        <td>${fmt(p.mark_price, 1)}</td>
                        <td>{fmt(p.size, 0)}</td>
                        <td className={pnl >= 0 ? "bull" : "bear"}>${fmt(pnl, 2)}</td>
                        <td>{(p.setup_name || "—").replaceAll("_", " ")}</td>
                        <td>{fmt(p.leverage || 100, 0)}×</td>
                        <td>
                          <span className={`status-tag ${tag}`}>{tagLabel}</span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </main>
      </div>

      <div
        className={`sidebar-backdrop ${sidebarOpen ? "show" : ""}`}
        onClick={() => setSidebarOpen(false)}
      />
    </div>
  );
}
