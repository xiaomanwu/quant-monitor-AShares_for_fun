/* 交易台前端：视图切换 + 回测 + 对比 + 模拟盘 */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);
  const UP = "#ff4d5e", DOWN = "#17c17d", ACCENT = "#4d8dff", BENCH = "#8b93a7";
  const PALETTE = ["#4d8dff", "#ff4d5e", "#17c17d", "#f5b342", "#c084fc", "#38bdf8"];

  const fmt = (v, d = 2) => (v == null || isNaN(v)) ? "—" : Number(v).toFixed(d);
  const pct = (v, d = 2) => (v > 0 ? "+" : "") + fmt(v, d) + "%";
  const cls = (v) => v > 0 ? "up-c" : v < 0 ? "down-c" : "flat-c";
  const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
  const money = (v) => {
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + "亿";
    if (a >= 1e4) return (v / 1e4).toFixed(1) + "万";
    return v.toFixed(0);
  };

  const state = {
    view: "monitor",
    strategies: [],
    universes: [],
    currentStrategy: "momentum",
    params: {},
    lastBacktest: null,
    compareData: null,
    paper: null,
    factorData: null,
  };

  // ================= 视图切换 =================
  function switchView(name) {
    state.view = name;
    $$(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === name));
    const map = { monitor: "viewMonitor", backtest: "viewBacktest", compare: "viewCompare", factors: "viewFactors", paper: "viewPaper" };
    Object.entries(map).forEach(([k, id]) => {
      const el = $("#" + id);
      if (el) el.classList.toggle("hidden", k !== name);
    });
    if (name === "backtest") setTimeout(() => { drawNav(); }, 50);
    if (name === "compare" && state.compareData) setTimeout(drawCompare, 50);
    if (name === "factors" && !state.factorData) loadFactors();
    if (name === "paper") loadPaper();
    if (name === "monitor") setTimeout(() => window.dispatchEvent(new Event("resize")), 50);
  }
  $$(".nav-btn").forEach(b => b.addEventListener("click", () => switchView(b.dataset.view)));

  async function loadFactors() {
    const button = $("#runFactors");
    button.disabled = true;
    button.textContent = "挖掘中…";
    $("#factorTableBody").innerHTML = '<tr><td colspan="9" class="factor-placeholder">正在读取近一年全市场日线并计算因子指标…</td></tr>';
    try {
      const response = await fetch("/api/factors/mine?horizon=5");
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "因子挖掘失败");
      state.factorData = result;
      renderFactors(result);
    } catch (error) {
      $("#factorTableBody").innerHTML =
        `<tr><td colspan="9" class="factor-placeholder">${escapeHTML(error.message)}</td></tr>`;
    } finally {
      button.disabled = false;
      button.textContent = "重新挖掘";
    }
  }

  function renderFactors(result) {
    $("#factorMeta").textContent =
      `数据区间 ${result.start_date} 至 ${result.end_date} · 可用股票 ${result.stock_count} 只 · ` +
      `截面样本日 ${result.snapshot_count} · 预测未来 ${result.horizon} 个交易日`;
    $("#factorNote").textContent = `${result.method}。五类因子均以当日未复权收盘数据计算，样本为当前可用 A 股；可能受公司行为和幸存者偏差影响。分组收益为样本期平均值，仅供研究，不代表未来表现或投资建议。`;
    $("#factorTableBody").innerHTML = result.factors.map((factor, index) => `
      <tr class="${index === 0 ? "rank1" : ""}">
        <td class="sname">${factor.name}</td>
        <td class="factor-definition">${factor.description}</td>
        <td class="r ${cls(factor.mean_ic)}">${fmt(factor.mean_ic, 4)}</td>
        <td class="r ${cls(factor.icir)}">${fmt(factor.icir, 3)}</td>
        <td class="r">${fmt(factor.positive_ratio * 100, 1)}%</td>
        <td class="r ${cls(factor.top_return)}">${pct(factor.top_return * 100)}</td>
        <td class="r ${cls(factor.bottom_return)}">${pct(factor.bottom_return * 100)}</td>
        <td class="r ${cls(factor.spread)}">${pct(factor.spread * 100)}</td>
        <td class="r">${factor.samples.toLocaleString()}</td>
      </tr>`).join("") ||
      '<tr><td colspan="9" class="factor-placeholder">样本不足，暂无法计算因子统计。</td></tr>';
  }

  $("#runFactors").addEventListener("click", () => {
    state.factorData = null;
    loadFactors();
  });

  // ================= 初始化策略/股票池 =================
  async function initMeta() {
    try {
      const d = await (await fetch("/api/strategies")).json();
      state.strategies = d.strategies;
      state.universes = d.universes;
      renderStrategyList();
      renderParams();

      // 回测股票池
      $("#btUniverse").innerHTML = state.universes
        .map(u => `<option value="${u.key}">${u.name}</option>`).join("");
      // 模拟盘策略
      $("#paperStrategy").innerHTML = state.strategies
        .map(s => `<option value="${s.key}">${s.name}</option>`).join("");
      $("#paperStrategy").value = "momentum";
    } catch (e) {
      console.warn("初始化失败", e);
    }
  }

  const PARAM_LABEL = {
    min_score: ["最低评分", 1], fast: ["快线周期", 1], slow: ["慢线周期", 1],
    lookback: ["动量回看天数", 1], skip: ["跳过最近天数", 1],
    period: ["布林周期", 1], k: ["带宽倍数", 0.1], signal: ["信号线周期", 1],
  };

  function renderStrategyList() {
    $("#strategyList").innerHTML = state.strategies.map(s => `
      <div class="strategy-item ${s.key === state.currentStrategy ? "active" : ""}" data-key="${s.key}">
        <div class="si-name">${s.name}<span class="si-tag">${s.tag}</span></div>
        <div class="si-desc">${s.desc}</div>
      </div>`).join("");
    $$(".strategy-item").forEach(el => el.addEventListener("click", () => {
      state.currentStrategy = el.dataset.key;
      state.params = {};
      renderStrategyList();
      renderParams();
    }));
  }

  function renderParams() {
    const s = state.strategies.find(x => x.key === state.currentStrategy);
    if (!s) return;
    const entries = Object.entries(s.params || {});
    $("#paramInputs").innerHTML = entries.length ? entries.map(([k, v]) => {
      const [label, step] = PARAM_LABEL[k] || [k, 1];
      return `<div class="row2" style="margin-bottom:8px">
        <div><span class="tip">${label}</span>
          <input type="number" class="inp param-inp" data-k="${k}" value="${v}" step="${step}"></div>
      </div>`;
    }).join("") : '<div class="mini">该策略无额外参数</div>';
  }

  // ================= 回测 =================
  function collectConfig() {
    const params = {};
    $$(".param-inp").forEach(el => { params[el.dataset.k] = Number(el.value); });
    return {
      strategy: state.currentStrategy,
      params,
      universe: $("#btUniverse").value,
      start: $("#btStart").value,
      end: $("#btEnd").value,
      initial_cash: Number($("#btCash").value) * 10000,
      top_n: Number($("#btTopN").value),
      rebalance_days: Number($("#btRebal")) || 5,
      weighting: $("#btWeight").value,
      commission_rate: Number($("#btComm").value) / 10000,
      slippage: Number($("#btSlip").value) / 100,
      max_position_pct: Number($("#btMaxPos").value) / 100,
      stop_loss: Number($("#btStopLoss").value) / 100,
      trailing_stop: Number($("#btTrail").value) / 100,
      max_drawdown_stop: Number($("#btDdStop").value) / 100,
    };
  }

  $("#runBacktest").addEventListener("click", async () => {
    const btn = $("#runBacktest");
    btn.disabled = true; btn.textContent = "回测中…";
    $("#btProgress").classList.remove("hidden");
    try {
      const cfg = collectConfig();
      const r = await fetch("/api/backtest", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "回测失败");
      state.lastBacktest = d;
      $("#btPlaceholder").classList.add("hidden");
      $("#btContent").classList.remove("hidden");
      renderMetrics(d.metrics);
      renderTrades(d.trades, d.metrics);
      renderHoldings(d.holdings);
      drawNav();
    } catch (e) {
      // 简化提示，避免依赖主文件的 toast
      const box = $("#btPlaceholder");
      box.classList.remove("hidden");
      $("#btContent").classList.add("hidden");
      box.innerHTML = `<div class="ph-icon">⚠</div><h3>回测失败</h3><p>${e.message}</p>`;
    } finally {
      btn.disabled = false; btn.textContent = "运行回测";
      $("#btProgress").classList.add("hidden");
    }
  });

  function renderMetrics(m) {
    const items = [
      ["总收益", pct(m.total_return), cls(m.total_return), `期末 ${money(m.final_nav)}`],
      ["年化收益", pct(m.annual_return), cls(m.annual_return), `基准 ${pct(m.bench_total_return)}`],
      ["超额收益", pct(m.excess_return), cls(m.excess_return), "相对沪深300"],
      ["夏普比率", fmt(m.sharpe), m.sharpe > 0 ? "up-c" : "down-c", `年化波动 ${fmt(m.annual_volatility)}%`],
      ["最大回撤", fmt(m.max_drawdown) + "%", "down-c", `基准 ${fmt(m.max_drawdown_bench)}%`],
      ["卡玛比率", fmt(m.calmar), m.calmar > 0 ? "up-c" : "down-c", `年化换手 ${fmt(m.turnover_ann, 0)}%`],
      ["胜率", fmt(m.win_rate) + "%", m.win_rate > 50 ? "up-c" : "", `${m.closed_trades} 笔平仓`],
      ["盈亏比", fmt(m.profit_factor), m.profit_factor > 1 ? "up-c" : "down-c", `交易 ${m.trade_count} 笔`],
      ["Alpha", pct(m.alpha), cls(m.alpha), `Beta ${fmt(m.beta)}`],
      ["索提诺", fmt(m.sortino), m.sortino > 0 ? "up-c" : "down-c", "下行风险调整"],
      ["交易日", m.days, "", `${m.start} 起`],
      ["期末现金", money(m.final_cash), "", "含未投资资金"],
    ];
    $("#metricGrid").innerHTML = items.map(([k, v, c, s]) => `
      <div class="metric">
        <div class="mk">${k}</div>
        <div class="mv ${c}">${v}</div>
        <div class="ms">${s || ""}</div>
      </div>`).join("");
  }

  function renderTrades(trades, m) {
    $("#tradeCount").textContent = `共 ${trades.length} 笔`;
    if (!trades.length) {
      $("#tradeTable").innerHTML = '<div class="empty">无成交</div>';
      return;
    }
    const rows = trades.slice().reverse().slice(0, 100);
    $("#tradeTable").innerHTML = `<table class="tt">
      <thead><tr><th>日期</th><th>股票</th><th>方向</th><th>价格</th><th>数量</th><th>金额</th></tr></thead>
      <tbody>${rows.map(t => `<tr>
        <td>${t.date}</td>
        <td class="nm">${t.name}</td>
        <td class="${t.side === "buy" ? "side-b" : "side-s"}">${t.side === "buy" ? "买入" : "卖出"}</td>
        <td>${fmt(t.price)}</td>
        <td>${t.shares}</td>
        <td>${money(t.amount)}</td>
      </tr>`).join("")}</tbody></table>`;
  }

  function renderHoldings(list) {
    if (!list || !list.length) {
      $("#holdingList").innerHTML = '<div class="empty">空仓</div>';
      return;
    }
    $("#holdingList").innerHTML = list.map(h => `
      <div class="hold-row">
        <div class="h-nm">${h.name}<span>${h.code.slice(2)} · ${h.shares}股</span></div>
        <div class="h-v">${money(h.value)}</div>
        <div class="h-v">${fmt(h.avg_price)}</div>
        <div class="h-p ${cls(h.pnl_pct)}">${pct(h.pnl_pct)}</div>
      </div>`).join("");
  }

  // ================= 净值曲线绘制 =================
  function drawNav() {
    const d = state.lastBacktest;
    const canvas = $("#navChart");
    if (!d || !canvas || canvas.offsetParent === null) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = rect.width, h = rect.height;
    ctx.clearRect(0, 0, w, h);

    const padL = 46, padR = 52, padT = 12, padB = 20;
    const cw = w - padL - padR;
    // 上方 72% 画净值，下方 28% 画回撤
    const navH = (h - padT - padB) * 0.72;
    const ddH = (h - padT - padB) * 0.22;
    const ddTop = padT + navH + (h - padT - padB) * 0.06;

    const navs = d.navs, bench = d.bench_navs, dds = d.drawdowns;
    const n = navs.length;
    if (n < 2) return;
    const all = navs.concat(bench && bench.length ? bench : []);
    let max = Math.max(...all), min = Math.min(...all);
    const padding = (max - min) * 0.08 || 0.02;
    max += padding; min -= padding;
    const X = (i) => padL + i / (n - 1) * cw;
    const Y = (v) => padT + (max - v) / (max - min) * navH;

    // 网格
    ctx.font = "10px monospace";
    ctx.textAlign = "left";
    for (let i = 0; i <= 4; i++) {
      const y = padT + navH / 4 * i;
      const v = max - (max - min) / 4 * i;
      ctx.strokeStyle = "rgba(30,39,54,.55)";
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + cw, y); ctx.stroke();
      ctx.fillStyle = "#5d6b82";
      ctx.fillText(v.toFixed(2), padL + cw + 6, y + 3.5);
    }

    // 净值 1.0 基准线
    if (min < 1 && max > 1) {
      ctx.strokeStyle = "rgba(154,168,189,.18)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(padL, Y(1)); ctx.lineTo(padL + cw, Y(1)); ctx.stroke();
      ctx.setLineDash([]);
    }

    // 基准线
    if (bench && bench.length) {
      ctx.beginPath();
      bench.forEach((v, i) => {
        const x = padL + i / (bench.length - 1) * cw;
        i ? ctx.lineTo(x, Y(v)) : ctx.moveTo(x, Y(v));
      });
      ctx.strokeStyle = BENCH; ctx.lineWidth = 1.1; ctx.globalAlpha = .75;
      ctx.stroke(); ctx.globalAlpha = 1;
    }

    // 策略线 + 渐变填充
    const grad = ctx.createLinearGradient(0, padT, 0, padT + navH);
    grad.addColorStop(0, "rgba(77,141,255,.28)");
    grad.addColorStop(1, "rgba(77,141,255,0)");
    ctx.beginPath();
    navs.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
    ctx.lineTo(X(n - 1), padT + navH);
    ctx.lineTo(X(0), padT + navH);
    ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();

    ctx.beginPath();
    navs.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
    ctx.strokeStyle = ACCENT; ctx.lineWidth = 1.6; ctx.stroke();

    // 末端圆点
    ctx.beginPath();
    ctx.arc(X(n - 1), Y(navs[n - 1]), 3, 0, Math.PI * 2);
    ctx.fillStyle = ACCENT; ctx.fill();

    // 回撤区域
    const minDd = Math.min(...dds, -1);
    const ddY = (v) => ddTop + (v / minDd) * ddH;
    ctx.beginPath();
    ctx.moveTo(X(0), ddTop);
    dds.forEach((v, i) => ctx.lineTo(X(i), ddY(v)));
    ctx.lineTo(X(n - 1), ddTop);
    ctx.closePath();
    ctx.fillStyle = "rgba(255,77,94,.28)"; ctx.fill();
    ctx.beginPath();
    dds.forEach((v, i) => i ? ctx.lineTo(X(i), ddY(v)) : ctx.moveTo(X(i), ddY(v)));
    ctx.strokeStyle = "rgba(255,77,94,.6)"; ctx.lineWidth = 1; ctx.stroke();

    // 回撤标签
    ctx.fillStyle = "#5d6b82"; ctx.textAlign = "left";
    ctx.fillText(`回撤 ${minDd.toFixed(1)}%`, padL + 2, ddTop - 3);

    // 时间轴
    ctx.fillStyle = "#5d6b82"; ctx.textAlign = "center";
    const dts = d.dates;
    const step = Math.max(1, Math.floor(n / 6));
    for (let i = 0; i < n; i += step) {
      ctx.fillText(dts[i].slice(0, 7), X(i), h - 5);
    }
  }
  window.addEventListener("resize", () => {
    clearTimeout(window._rz2);
    window._rz2 = setTimeout(() => { drawNav(); drawCompare(); }, 160);
  });

  // ================= 策略对比 =================
  $("#runCompare").addEventListener("click", async () => {
    const btn = $("#runCompare");
    btn.disabled = true; btn.textContent = "计算中…";
    try {
      const r = await fetch("/api/compare", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          universe: $("#cmpUniverse").value,
          start: $("#cmpStart").value,
          end: new Date().toISOString().slice(0, 10),
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "对比失败");
      state.compareData = d;
      $("#cmpChartCard").style.display = "";
      $("#cmpTableWrap").style.display = "";
      renderCompareTable(d);
      drawCompare();
    } catch (e) {
      alert(e.message);
    } finally {
      btn.disabled = false; btn.textContent = "开始对比";
    }
  });

  function renderCompareTable(d) {
    $("#cmpTableBody").innerHTML = d.results.map((r, i) => {
      const m = r.metrics;
      return `<tr class="rank${i + 1}">
        <td class="sname"><span class="rank-badge">${i + 1}</span>${r.name}</td>
        <td class="sname" style="color:var(--text-3);font-size:11px">${r.tag}</td>
        <td class="r ${cls(m.total_return)}">${pct(m.total_return)}</td>
        <td class="r ${cls(m.annual_return)}">${pct(m.annual_return)}</td>
        <td class="r">${fmt(m.sharpe)}</td>
        <td class="r down-c">${fmt(m.max_drawdown)}%</td>
        <td class="r">${fmt(m.calmar)}</td>
        <td class="r">${fmt(m.win_rate)}%</td>
        <td class="r">${m.trade_count}</td>
        <td class="r">${fmt(m.turnover_ann, 0)}%</td>
      </tr>`;
    }).join("");
    $("#cmpLegend").innerHTML = d.results.map((r, i) =>
      `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${r.name}</span>`).join("")
      + '<span><i style="background:#8b93a7"></i>基准</span>';
  }

  function drawCompare() {
    const d = state.compareData;
    const canvas = $("#cmpChart");
    if (!d || !canvas || canvas.offsetParent === null) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = rect.width, h = rect.height;
    ctx.clearRect(0, 0, w, h);

    const padL = 46, padR = 52, padT = 14, padB = 20;
    const cw = w - padL - padR, ch = h - padT - padB;
    const n = d.dates.length;
    const all = [];
    d.results.forEach(r => all.push(...r.navs));
    if (d.bench_navs && d.bench_navs.length) all.push(...d.bench_navs);
    let max = Math.max(...all), min = Math.min(...all);
    const pad = (max - min) * 0.08 || 0.02;
    max += pad; min -= pad;
    const X = (i) => padL + i / (n - 1) * cw;
    const Y = (v) => padT + (max - v) / (max - min) * ch;

    ctx.font = "10px monospace";
    for (let i = 0; i <= 4; i++) {
      const y = padT + ch / 4 * i;
      const v = max - (max - min) / 4 * i;
      ctx.strokeStyle = "rgba(30,39,54,.55)";
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + cw, y); ctx.stroke();
      ctx.fillStyle = "#5d6b82"; ctx.textAlign = "left";
      ctx.fillText(v.toFixed(2), padL + cw + 6, y + 3.5);
    }

    if (d.bench_navs && d.bench_navs.length) {
      ctx.beginPath();
      d.bench_navs.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
      ctx.strokeStyle = BENCH; ctx.lineWidth = 1.2; ctx.setLineDash([4, 3]);
      ctx.stroke(); ctx.setLineDash([]);
    }

    d.results.forEach((r, idx) => {
      ctx.beginPath();
      r.navs.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
      ctx.strokeStyle = PALETTE[idx % PALETTE.length];
      ctx.lineWidth = idx === 0 ? 1.9 : 1.2;
      ctx.globalAlpha = idx === 0 ? 1 : 0.75;
      ctx.stroke();
      ctx.globalAlpha = 1;
    });

    ctx.fillStyle = "#5d6b82"; ctx.textAlign = "center";
    const step = Math.max(1, Math.floor(n / 6));
    for (let i = 0; i < n; i += step) {
      ctx.fillText(d.dates[i].slice(0, 7), X(i), h - 5);
    }
  }

  // ================= 模拟盘 =================
  async function loadPaper() {
    try {
      const s = await (await fetch("/api/paper")).json();
      state.paper = s;
      if (!s.created) {
        $("#noAccount").classList.remove("hidden");
        $("#paperMain").classList.add("hidden");
        return;
      }
      $("#noAccount").classList.add("hidden");
      $("#paperMain").classList.remove("hidden");
      renderPaper(s);
    } catch (e) { console.warn(e); }
  }

  function renderPaper(s) {
    const items = [
      ["总资产", money(s.equity), "", `本金 ${money(s.initial_cash)}`],
      ["总收益", pct(s.total_return), cls(s.total_return), `${money(s.total_pnl)}`],
      ["可用现金", money(s.cash), "", `仓位 ${s.equity ? ((s.equity - s.cash) / s.equity * 100).toFixed(1) : 0}%`],
      ["持仓市值", money(s.market_value), "", `${s.position_count} 只`],
      ["最大回撤", fmt(s.max_drawdown) + "%", "down-c", "累计"],
      ["成交笔数", s.trade_count, "", `上次 ${s.last_run || "—"}`],
    ];
    $("#paperMetrics").innerHTML = items.map(([k, v, c, x]) => `
      <div class="metric"><div class="mk">${k}</div>
        <div class="mv ${c}">${v}</div><div class="ms">${x || ""}</div></div>`).join("");

    const stratName = (state.strategies.find(x => x.key === s.strategy) || {}).name || s.strategy;
    $("#paStrategy").textContent = stratName;
    $("#paMeta").textContent = `股票池 ${s.universe} · 创建于 ${(s.created || "").slice(0, 10)}`;
    $("#paStatus").textContent = s.status;

    $("#paPosCount").textContent = `${s.position_count} 只`;
    $("#paperPositions").innerHTML = s.positions.length ? s.positions.map(h => `
      <div class="hold-row">
        <div class="h-nm">${h.name}<span>${h.code.slice(2)} · ${h.shares}股 · ${fmt(h.weight)}%</span></div>
        <div class="h-v">${money(h.value)}</div>
        <div class="h-v">${fmt(h.avg_price)}</div>
        <div class="h-p ${cls(h.pnl_pct)}">${pct(h.pnl_pct)}</div>
      </div>`).join("") : '<div class="empty">空仓</div>';

    $("#paperTrades").innerHTML = s.trades.length ? `<table class="tt">
      <thead><tr><th>日期</th><th>股票</th><th>方向</th><th>价格</th><th>数量</th><th>原因</th></tr></thead>
      <tbody>${s.trades.map(t => `<tr>
        <td>${t.date}</td><td class="nm">${t.name}</td>
        <td class="${t.side === "buy" ? "side-b" : "side-s"}">${t.side === "buy" ? "买入" : "卖出"}</td>
        <td>${fmt(t.price)}</td><td>${t.shares}</td>
        <td style="color:var(--text-3)">${t.reason || ""}</td>
      </tr>`).join("")}</tbody></table>` : '<div class="empty">暂无成交</div>';
  }

  $("#createPaper").addEventListener("click", async () => {
    const btn = $("#createPaper");
    btn.disabled = true; btn.textContent = "创建中…";
    try {
      const r = await fetch("/api/paper/init", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          strategy: $("#paperStrategy").value,
          universe: $("#paperUniverse").value,
          initial_cash: Number($("#paperCash").value) * 10000,
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "创建失败");
      await loadPaper();
    } catch (e) { alert(e.message); }
    finally { btn.disabled = false; btn.textContent = "创建账户"; }
  });

  $("#runPaper").addEventListener("click", async () => {
    const btn = $("#runPaper");
    btn.disabled = true; btn.textContent = "执行中…";
    try {
      const r = await fetch("/api/paper/run?force=true", { method: "POST" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "执行失败");
      $("#paLog").textContent = `已执行：目标 ${d.target} 只，委托 ${d.orders} 笔，成交 ${d.filled} 笔`
        + (d.log && d.log.length ? " | " + d.log.join("；") : "");
      state.paper = d.summary;
      renderPaper(d.summary);
    } catch (e) {
      $("#paLog").textContent = "失败：" + e.message;
    } finally { btn.disabled = false; btn.textContent = "执行今日调仓"; }
  });

  $("#resetPaper").addEventListener("click", async () => {
    if (!confirm("确认重置模拟盘？所有持仓与成交记录将清空。")) return;
    await fetch("/api/paper/reset", { method: "POST" });
    await loadPaper();
  });

  // ================= 启动 =================
  initMeta();
  window.__switchView = switchView;
})();
