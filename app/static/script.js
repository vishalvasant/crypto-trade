const $ = (id) => document.getElementById(id);
const IST_TZ = "Asia/Kolkata";

let chartWidget = null;
let lastPrice = null;
let selectedTf = "5";
let lastFunding = null;
let activeLevelsTab = "sr";
let latestOrderbook = null;
let ws = null;
let wsRetryMs = 1000;
let restFallbackTimer = null;

function fmt(n, d = 2) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

function fmtCompact(n, d = 1) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(2)}K`;
  return fmt(v, d);
}

function fmtTimeIST(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-IN", {
    timeZone: IST_TZ,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
}

function fmtPct(n, d = 2) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(d)}%`;
}

function sideClass(side) {
  const s = String(side || "").toUpperCase();
  if (s === "LONG" || s === "BUY" || s === "BULLISH") return "long";
  if (s === "SHORT" || s === "SELL" || s === "BEARISH") return "short";
  return "wait";
}

function clamp(n, a, b) {
  return Math.max(a, Math.min(b, n));
}

function setLiveStatus(connected) {
  const pill = $("livePill");
  const label = $("liveLabel");
  if (!pill || !label) return;
  if (connected) {
    pill.classList.remove("offline");
    label.textContent = "Live WS";
  } else {
    pill.classList.add("offline");
    label.textContent = "Reconnecting";
  }
}

function renderAssetHeader(symbol) {
  const price = Number(symbol.mark_price || symbol.price || 0);
  const el = $("markPrice");
  const prev = lastPrice;
  el.textContent = `$${fmt(price, 1)}`;
  const up = prev == null ? true : price >= prev;
  el.className = `price-xl ${up ? "bull" : "bear"}`;
  if (prev != null && price !== prev) {
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth;
    el.classList.add(price > prev ? "flash-up" : "flash-down");
  }

  let chg = 0;
  if (prev && prev > 0) chg = ((price - prev) / prev) * 100;
  const chgEl = $("priceChange");
  const abs = price - (prev || price);
  chgEl.textContent = `${abs >= 0 ? "+" : ""}${fmt(abs, 1)} (${fmtPct(chg)})`;
  chgEl.className = `chg ${chg >= 0 ? "bull" : "bear"}`;
  lastPrice = price;

  $("statHigh").textContent = `$${fmt(symbol.high_24h || price, 1)}`;
  $("statLow").textContent = `$${fmt(symbol.low_24h || price, 1)}`;
  $("statVol").textContent = fmtCompact(symbol.volume || 0, 1);
  $("statOi").textContent = fmtCompact(symbol.oi || 0, 1);
  const oi = Number(symbol.oi || 0);
  const oiChg = Number(symbol.oi_change_6h || 0);
  const oiEl = $("statOiChg");
  if (oi > 0 && oiChg && Math.abs(oiChg / oi) >= 0.001) {
    const pct = (oiChg / oi) * 100;
    oiEl.textContent = `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}% 6h`;
    oiEl.className = pct >= 0 ? "bull" : "bear";
  } else {
    oiEl.textContent = "";
    oiEl.className = "";
  }

  const funding = symbol.funding_rate;
  const fundEl = $("statFunding");
  if (funding == null) fundEl.textContent = "—";
  else {
    const f = Number(funding);
    fundEl.textContent = fmtPct(f, 4);
    fundEl.className = f >= 0 ? "bull" : "bear";
  }
  lastFunding = symbol.funding || lastFunding;
}

function renderRegime(decision) {
  const signal = decision.signal || {};
  const feature = decision.feature || {};
  const state = (signal.regime_state || feature.regime_state || "RANGE").toUpperCase();
  const score = Number(signal.regime_score ?? feature.regime_score ?? 0);
  const conf = clamp(Math.abs(score), 0, 100);

  const biasClass = state.includes("BULL") ? "bull" : state.includes("BEAR") ? "bear" : "wait";
  $("overallBias").textContent = state === "RANGE" ? "Neutral" : state.charAt(0) + state.slice(1).toLowerCase();
  $("overallBias").className = `bias-text ${biasClass}`;
  $("regimeChip").textContent = `Score ${score.toFixed(0)}`;
  $("regimeChip").className = `chip ${biasClass}`;
  $("confPct").textContent = `${conf.toFixed(0)}%`;
  $("confBar").style.width = `${conf}%`;

  const cells = [
    { tf: "15m", bias: score <= -20 ? "Bearish" : score >= 20 ? "Bullish" : "Sideways" },
    { tf: "5m", bias: score <= -15 ? "Bearish" : score >= 15 ? "Bullish" : "Sideways" },
    { tf: "3m", bias: score <= -10 ? "Bearish" : score >= 10 ? "Bullish" : "Sideways" },
    { tf: "1m", bias: score <= -5 ? "Bearish" : score >= 5 ? "Bullish" : "Sideways" },
  ];
  $("regimeGrid").innerHTML = cells
    .map((c) => {
      const cls = c.bias === "Bullish" ? "bull" : c.bias === "Bearish" ? "bear" : "";
      return `<div class="regime-cell"><span>${c.tf}</span><strong class="${cls}">${c.bias}</strong></div>`;
    })
    .join("");
}

function renderLevels(levels) {
  if (!levels || !levels.length) {
    $("levelsTable").innerHTML = `<p class="muted" style="padding:8px 0">Collecting levels…</p>`;
    return;
  }
  $("levelsTable").innerHTML = levels
    .map((lv) => {
      const isRes = lv.side === "resistance";
      const pct = clamp(Number(lv.strength || 0) * 100, 8, 100);
      return `
        <div class="level-row">
          <span class="lbl ${isRes ? "res" : "sup"}">${lv.label}</span>
          <span class="px">$${fmt(lv.price, 1)}</span>
          <div class="strength ${isRes ? "res" : "sup"}"><i style="width:${pct}%"></i></div>
          <span class="bell">🔔</span>
        </div>`;
    })
    .join("");

  // Approximate liquidation clusters near key levels (inferred — no public liq feed)
  $("liqTable").innerHTML = levels
    .slice(0, 6)
    .map((lv) => {
      const isRes = lv.side === "resistance";
      const dens = clamp(Number(lv.strength || 0.5) * 100, 20, 100);
      return `
        <div class="level-row">
          <span class="lbl ${isRes ? "res" : "sup"}">${isRes ? "Shorts" : "Longs"}</span>
          <span class="px">$${fmt(lv.price, 1)}</span>
          <div class="strength ${isRes ? "res" : "sup"}"><i style="width:${dens}%"></i></div>
          <span class="muted" style="font-size:10px">est.</span>
        </div>`;
    })
    .join("");
}

function renderOrderBookDepth(orderbook) {
  latestOrderbook = orderbook || latestOrderbook;
  const book = latestOrderbook || { bids: [], asks: [] };
  const bids = book.bids || [];
  const asks = book.asks || [];
  const bidRows = Array.from({ length: 8 }, (_, i) => {
    const b = bids[i] || [0, 0];
    return `<div class="ob-row bid"><strong>${fmt(b[0], 1)}</strong><span>${fmt(b[1], 2)}</span></div>`;
  }).join("");
  const askRows = Array.from({ length: 8 }, (_, i) => {
    const a = asks[i] || [0, 0];
    return `<div class="ob-row ask"><strong>${fmt(a[0], 1)}</strong><span>${fmt(a[1], 2)}</span></div>`;
  }).join("");
  $("obDepth").innerHTML = `
    <div class="ob-col"><h4>Bids</h4>${bidRows}</div>
    <div class="ob-col"><h4>Asks</h4>${askRows}</div>
  `;
}

function showLevelsTab(tab) {
  activeLevelsTab = tab;
  $("levelsTable").hidden = tab !== "sr";
  $("liqTable").hidden = tab !== "liq";
  $("obDepth").hidden = tab !== "ob";
  document.querySelectorAll("#levelsTabs .tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
}

function renderMarketData(symbol, orderbook) {
  const mark = Number(symbol.mark_price || symbol.price || 0);
  const bid = Number(symbol.bid || 0);
  const ask = Number(symbol.ask || 0);
  const mid = bid && ask ? (bid + ask) / 2 : mark;
  const nextFund = (symbol.funding && symbol.funding.countdown) || (lastFunding && lastFunding.countdown) || "—";
  const nextFundLabel = (symbol.funding && symbol.funding.next_funding_label) || (lastFunding && lastFunding.next_funding_label) || "—";
  $("marketGrid").innerHTML = `
    <div class="m-card"><span>Mark Price</span><strong>$${fmt(mark, 1)}</strong></div>
    <div class="m-card"><span>Index / Mid</span><strong>$${fmt(mid, 1)}</strong></div>
    <div class="m-card"><span>24h Volume</span><strong>${fmtCompact(symbol.volume || 0, 1)}</strong></div>
    <div class="m-card"><span>Open Interest</span><strong>${fmtCompact(symbol.oi || 0, 1)}</strong></div>
    <div class="m-card"><span>Funding Rate</span><strong>${symbol.funding_rate == null ? "—" : fmtPct(symbol.funding_rate, 4)}</strong></div>
    <div class="m-card"><span>Next Funding (IST)</span><strong title="${nextFundLabel}">${nextFund}</strong></div>
    <div class="m-card"><span>Best Bid / Ask</span><strong>${fmt(bid, 1)} / ${fmt(ask, 1)}</strong></div>
    <div class="m-card"><span>Timezone</span><strong>IST · Asia/Kolkata</strong></div>
  `;

  let longPct = 50;
  const side = String(symbol.last_trade_side || "").toLowerCase();
  const oiChg = Number(symbol.oi_change_6h || 0);
  if (side.includes("buy")) longPct += 6;
  if (side.includes("sell")) longPct -= 6;
  if (oiChg > 0) longPct += 4;
  if (oiChg < 0) longPct -= 4;
  longPct = clamp(longPct, 35, 65);
  const shortPct = 100 - longPct;
  $("lsRatio").textContent = `${longPct.toFixed(1)}% / ${shortPct.toFixed(1)}%`;
  $("lsBar").innerHTML = `<i class="long" style="width:${longPct}%"></i><i class="short" style="width:${shortPct}%"></i>`;

  let imbalance = Number((orderbook && orderbook.imbalance_pct) || 0);
  if (!imbalance && orderbook) {
    const bids = orderbook.bids || [];
    const asks = orderbook.asks || [];
    const bidSz = bids.slice(0, 5).reduce((s, l) => s + Number(l[1] || 0), 0);
    const askSz = asks.slice(0, 5).reduce((s, l) => s + Number(l[1] || 0), 0);
    const tot = bidSz + askSz;
    imbalance = tot > 0 ? ((bidSz - askSz) / tot) * 100 : 0;
  }
  $("obImbalance").textContent = `${imbalance >= 0 ? "+" : ""}${Number(imbalance).toFixed(1)}%`;
  $("obImbalance").className = imbalance >= 0 ? "bull" : "bear";
  $("obBar").style.width = `${clamp(Math.abs(imbalance), 5, 100)}%`;
  const obBarHost = $("obBar").parentElement;
  if (obBarHost) obBarHost.classList.toggle("danger", imbalance < 0);
  renderOrderBookDepth(orderbook);
}

function renderSignal(decision) {
  const signal = decision.signal || {};
  const side = String(signal.side || "WAIT").toUpperCase();
  const cls = sideClass(side);
  const orb = $("signalOrb");
  orb.className = `signal-orb ${cls}`;
  orb.textContent = side === "BUY" ? "LONG" : side === "SELL" ? "SHORT" : side;

  const reject = signal.rejection_reason;
  const setup = signal.setup_name;
  let msg = "No strong setup.";
  if (side === "LONG" || side === "SHORT") msg = setup ? `${setup.replaceAll("_", " ")} · live signal` : "Validated setup";
  else if (reject === "WATCHLIST_SCORE_60_74") msg = "Watchlist — waiting for confirmation";
  else if (reject === "POSITION_ALREADY_OPEN") msg = "Position open — managing trade";
  else if (reject === "LOSS_COOLDOWN") msg = "Loss cooldown active (IST)";
  else if (reject) msg = reject.replaceAll("_", " ").toLowerCase();
  $("signalMsg").textContent = msg;

  const bits = [];
  if (signal.score != null) bits.push(`Score ${Number(signal.score).toFixed(0)}`);
  if (setup) bits.push(setup.replaceAll("_", " "));
  if (signal.stop != null) bits.push(`SL ${fmt(signal.stop, 1)}`);
  if (signal.target != null) bits.push(`TP ${fmt(signal.target, 1)}`);
  $("signalMeta").innerHTML = bits.map((b) => `<span class="chip">${b}</span>`).join("");

  const checks = decision.checklist || [];
  $("checklist").innerHTML = checks
    .map(
      (c) => `
      <div class="check-item">
        <span class="check-ico ${c.ok ? "ok" : "no"}">${c.ok ? "✓" : "✕"}</span>
        <span>${c.label}</span>
      </div>`
    )
    .join("");
}

function renderPosition(symbol, account, decision) {
  const pos = symbol.position;
  const status = $("posStatus");
  if (!pos || !pos.is_open) {
    status.textContent = "No Open Position";
    status.className = "chip";
    $("positionBody").innerHTML = `<div class="pos-empty">Flat · waiting for next qualified setup</div>`;
  } else {
    const cls = sideClass(pos.side);
    status.textContent = `OPEN · ${pos.side}`;
    status.className = `chip ${cls}`;
    const pnl = Number(pos.pnl || 0);
    $("positionBody").innerHTML = `
      <div class="pos-card">
        <div class="row"><span>Side / Lots</span><strong class="${cls}">${pos.side} · ${fmt(pos.size, 0)}</strong></div>
        <div class="row"><span>Entry</span><strong>$${fmt(pos.entry_price, 1)}</strong></div>
        <div class="row"><span>Mark</span><strong>$${fmt(pos.mark_price || symbol.mark_price, 1)}</strong></div>
        <div class="row"><span>Stop / Target</span><strong>$${fmt(pos.stop_price, 1)} / $${fmt(pos.target_price, 1)}</strong></div>
        <div class="row"><span>Margin · Lev</span><strong>$${fmt(pos.margin, 2)} · ${fmt(pos.leverage, 0)}×</strong></div>
        <div class="row"><span>Unrealized P&amp;L</span><strong class="${pnl >= 0 ? "bull" : "bear"}">$${fmt(pnl, 2)}</strong></div>
        <div class="row"><span>Setup</span><strong>${(pos.setup_name || "—").replaceAll("_", " ")}</strong></div>
        <div class="row"><span>Opened (IST)</span><strong>${fmtTimeIST(pos.opened_at || pos.updated_at)}</strong></div>
      </div>`;
  }

  const dailyStart = Number(account.daily_starting_equity || account.initial_balance || 250);
  const dailyPnl = Number(account.daily_realized_pnl || 0);
  const dailyLossUsed = dailyPnl < 0 ? Math.abs(dailyPnl) / dailyStart : 0;
  const maxDaily = 0.03;
  $("dailyLossPct").textContent = `${(maxDaily * 100).toFixed(0)}%`;
  $("dailyLossBar").style.width = `${clamp((dailyLossUsed / maxDaily) * 100, 0, 100)}%`;
  $("riskTradePct").textContent = `${(Number(account.risk_per_trade || 0.01) * 100).toFixed(0)}%`;
  $("riskTradeBar").style.width = "100%";

  const stats = (decision && decision.stats) || {};
  const equity = Number(account.equity || 0);
  const unreal = Number(account.unrealized_pnl || 0);
  $("accountGrid").innerHTML = `
    <div class="a-card"><span>Daily P&amp;L</span><strong class="${dailyPnl >= 0 ? "bull" : "bear"}">$${fmt(dailyPnl, 2)}</strong></div>
    <div class="a-card"><span>Unrealized</span><strong class="${unreal >= 0 ? "bull" : "bear"}">$${fmt(unreal, 2)}</strong></div>
    <div class="a-card"><span>Win Rate</span><strong>${fmt(stats.win_rate || 0, 1)}%</strong></div>
    <div class="a-card balance"><span>Equity (IST)</span><strong>$${fmt(equity, 2)}</strong></div>
  `;
}

function renderTrades(positions) {
  const rows = positions || [];
  $("tradeCount").textContent = `${rows.length} trades`;
  if (!rows.length) {
    $("tradesBody").innerHTML = `<tr><td colspan="10" class="muted">No paper trades yet.</td></tr>`;
    return;
  }
  $("tradesBody").innerHTML = rows
    .slice(0, 25)
    .map((p, idx) => {
      const cls = sideClass(p.side);
      const open = !!p.is_open;
      const pnl = Number(p.pnl || p.realized_pnl || 0);
      const reason = (p.close_reason || "").toLowerCase();
      let tag = "other";
      let tagLabel = open ? "OPEN" : (p.close_reason || "CLOSED").replaceAll("_", " ");
      if (open) tag = "open";
      else if (reason.includes("target")) {
        tag = "tp";
        tagLabel = "TP";
      } else if (reason.includes("stop")) {
        tag = "sl";
        tagLabel = "SL";
      }
      const time = p.closed_at || p.updated_at || p.opened_at;
      return `
        <tr>
          <td>${idx + 1}</td>
          <td>${fmtTimeIST(time)}</td>
          <td><span class="side-pill ${cls}">${p.side}</span></td>
          <td>$${fmt(p.entry_price, 1)}</td>
          <td>$${fmt(p.mark_price, 1)}</td>
          <td>${fmt(p.size, 0)}</td>
          <td class="${pnl >= 0 ? "bull" : "bear"}">$${fmt(pnl, 2)}</td>
          <td>${(p.setup_name || "—").replaceAll("_", " ")}</td>
          <td>${fmt(p.leverage || 100, 0)}×</td>
          <td><span class="status-tag ${tag}">${tagLabel}</span></td>
        </tr>`;
    })
    .join("");
}

function applyDashboardPayload(data) {
  if (!data || data.type === "pong") return;
  const symbol = data.symbol || {};
  const decision = data.decision || {};
  const account = data.account || {};
  const orderbook = data.orderbook || {};
  const positions = data.positions || [];

  renderAssetHeader(symbol);
  renderRegime(decision);
  renderLevels(decision.levels || []);
  renderMarketData(symbol, orderbook);
  renderSignal(decision);
  renderPosition(symbol, account, decision);
  renderTrades(positions);
  showLevelsTab(activeLevelsTab);
}

function initChart(tf) {
  if (!window.TradingView) return;
  const host = $("tv_chart");
  host.innerHTML = "";
  chartWidget = new window.TradingView.widget({
    autosize: true,
    symbol: "BINANCE:BTCUSDT",
    interval: String(tf || selectedTf),
    timezone: "Asia/Kolkata",
    theme: "dark",
    style: "1",
    locale: "en",
    toolbar_bg: "#0a1018",
    enable_publishing: false,
    hide_side_toolbar: false,
    allow_symbol_change: false,
    withdateranges: true,
    save_image: false,
    details: false,
    studies: ["MAExp@tv-basicstudies", "VWAP@tv-basicstudies"],
    container_id: "tv_chart",
    backgroundColor: "#0a1018",
    gridColor: "rgba(120,150,190,0.08)",
  });
}

async function refreshDashboardRest() {
  try {
    const data = await (await fetch("/api/dashboard")).json();
    applyDashboardPayload(data);
  } catch (err) {
    console.error(err);
  }
}

function connectDashboardSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/dashboard`;
  try {
    ws = new WebSocket(url);
  } catch (err) {
    setLiveStatus(false);
    scheduleRestFallback();
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    wsRetryMs = 1000;
    setLiveStatus(true);
    clearInterval(restFallbackTimer);
    restFallbackTimer = null;
    ws.send("refresh");
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      applyDashboardPayload(data);
      setLiveStatus(true);
    } catch (err) {
      console.error(err);
    }
  };

  ws.onclose = () => {
    setLiveStatus(false);
    scheduleRestFallback();
    scheduleReconnect();
  };

  ws.onerror = () => {
    setLiveStatus(false);
    try {
      ws.close();
    } catch (e) {
      /* ignore */
    }
  };
}

function scheduleReconnect() {
  setTimeout(() => {
    connectDashboardSocket();
  }, wsRetryMs);
  wsRetryMs = Math.min(wsRetryMs * 1.6, 8000);
}

function scheduleRestFallback() {
  if (restFallbackTimer) return;
  restFallbackTimer = setInterval(refreshDashboardRest, 3000);
}

async function evaluateNow() {
  try {
    await fetch("/api/generate-signal/BTCUSD", { method: "POST" });
    if (ws && ws.readyState === WebSocket.OPEN) ws.send("refresh");
    else await refreshDashboardRest();
  } catch (err) {
    console.error(err);
  }
}

function setupNav() {
  const sidebar = $("sidebar");
  const backdrop = $("sidebarBackdrop");
  $("menuToggle").addEventListener("click", () => {
    sidebar.classList.toggle("open");
    backdrop.classList.toggle("show");
  });
  backdrop.addEventListener("click", () => {
    sidebar.classList.remove("open");
    backdrop.classList.remove("show");
  });

  document.querySelectorAll(".nav-item").forEach((el) => {
    el.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
      el.classList.add("active");
      sidebar.classList.remove("open");
      backdrop.classList.remove("show");
    });
  });

  document.querySelectorAll(".top-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".top-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
    });
  });

  document.querySelectorAll("#tfPills .tf").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#tfPills .tf").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      selectedTf = btn.dataset.tf;
      initChart(selectedTf);
    });
  });

  document.querySelectorAll("#levelsTabs .tab").forEach((btn) => {
    btn.addEventListener("click", () => showLevelsTab(btn.dataset.tab));
  });
}

$("refreshBtn").addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send("refresh");
  else refreshDashboardRest();
});
$("signalBtn").addEventListener("click", evaluateNow);
setupNav();
initChart(selectedTf);
refreshDashboardRest();
connectDashboardSocket();
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send("ping");
}, 15000);
