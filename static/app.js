/* 量化信号监控台 —— 前端逻辑（原生 JS + Canvas） */
(() => {
  "use strict";

  const UP = "#ff4d5e", DOWN = "#17c17d", FLAT = "#9aa8bd";
  const state = {
    selected: null,
    quotes: [],
    watchlist: [],
    detail: null,
    scale: 240,
    len: 120,
    tab: "feed",
    rankAsc: 0,
    sectorSummary: null,
    feedIds: new Set(),
    hoverIdx: -1,
    connected: false,
  };

  const $ = (s) => document.querySelector(s);
  const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);
  const fmtAmt = (v) => {
    if (!v) return "—";
    if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
    if (v >= 1e4) return (v / 1e4).toFixed(1) + "万";
    return v.toFixed(0);
  };
  const cls = (v) => v > 0 ? "up-c" : v < 0 ? "down-c" : "flat-c";
  const signed = (v, d = 2) => (v > 0 ? "+" : "") + fmt(v, d);

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._tm);
    t._tm = setTimeout(() => t.classList.remove("show"), 1800);
  }

  // ---------------- 顶栏 ----------------
  let latestIndexes = [];
  function renderIndexes(list) {
    latestIndexes = list || [];
    $("#indexStrip").innerHTML = latestIndexes.map((i) => {
      const c = i.pct > 0 ? "up" : i.pct < 0 ? "down" : "flat";
      return `<div class="idx ${c}">
        <span class="n">${i.name}</span>
        <span class="p">${fmt(i.price)}</span>
        <span class="c">${signed(i.pct)}%</span>
      </div>`;
    }).join("");
  }

  function tickClock() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    $("#clock").textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  setInterval(tickClock, 1000); tickClock();

  function setSession(session, trading) {
    $("#sessionText").textContent = session;
    $("#sessionBadge").classList.toggle("live", !!trading);
  }

  // ---------------- Sparkline ----------------
  function sparkSVG(points, color) {
    if (!points || points.length < 2) return '<svg class="spark"></svg>';
    const w = 46, h = 26, pad = 1.5;
    const min = Math.min(...points), max = Math.max(...points);
    const rng = max - min || 1;
    const step = (w - pad * 2) / (points.length - 1);
    const d = points.map((v, i) => {
      const x = pad + i * step;
      const y = h - pad - ((v - min) / rng) * (h - pad * 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<svg class="spark" viewBox="0 0 ${w} ${h}"><path d="${d}" fill="none" stroke="${color}" stroke-width="1.2"/></svg>`;
  }

  // ---------------- 自选列表 ----------------
  function renderWatchlist() {
    const list = $("#watchList");
    $("#watchCount").textContent = state.quotes.length;
    if (!state.quotes.length) {
      list.innerHTML = '<div class="empty">暂无自选股，上方输入代码添加</div>';
      return;
    }
    list.innerHTML = state.quotes.map((q) => {
      const c = q.pct > 0 ? "up-c" : q.pct < 0 ? "down-c" : "flat-c";
      const tone = q.score ? q.score.tone : "neutral";
      const active = state.selected === q.code ? "active" : "";
      const color = q.pct > 0 ? UP : q.pct < 0 ? DOWN : FLAT;
      return `<div class="watch-item ${active}" data-code="${q.code}">
        <div class="wi-left">
          <div class="nm">${q.name}<span class="tag ${tone}">${q.score ? q.score.score : 0}</span></div>
          <div class="cd">${q.code.slice(2)}</div>
        </div>
        ${sparkSVG(q.spark, color)}
        <div class="wi-right">
          <div class="pr ${c}">${fmt(q.price)}</div>
          <div class="pc ${c}">${signed(q.pct)}%</div>
        </div>
        <div class="wi-del" data-del="${q.code}" title="移除">×</div>
      </div>`;
    }).join("");
  }

  $("#watchList").addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      fetch("/api/watchlist/" + del.dataset.del, { method: "DELETE" }).then(() => {
        if (state.selected === del.dataset.del) state.selected = null;
        refreshWatch();
      });
      return;
    }
    const item = e.target.closest(".watch-item");
    if (item) selectStock(item.dataset.code);
  });

  // ---------------- 搜索 ----------------
  let searchTimer = null;
  $("#searchInput").addEventListener("input", (e) => {
    const v = e.target.value.trim();
    clearTimeout(searchTimer);
    if (!v) { $("#suggest").classList.remove("show"); return; }
    searchTimer = setTimeout(async () => {
      try {
        const r = await (await fetch("/api/search?q=" + encodeURIComponent(v))).json();
        const box = $("#suggest");
        if (!r.results.length) { box.classList.remove("show"); return; }
        box.innerHTML = r.results.map((x) =>
          `<div class="suggest-item" data-code="${x.code}"><span>${x.name}</span><span class="c">${x.code}</span></div>`
        ).join("");
        box.classList.add("show");
      } catch (err) { /* ignore */ }
    }, 220);
  });

  $("#suggest").addEventListener("click", async (e) => {
    const it = e.target.closest(".suggest-item");
    if (!it) return;
    await addStock(it.dataset.code);
    $("#suggest").classList.remove("show");
    $("#searchInput").value = "";
  });

  $("#searchInput").addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const v = e.target.value.trim();
    if (!v) return;
    await addStock(v);
    e.target.value = "";
    $("#suggest").classList.remove("show");
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search-wrap")) $("#suggest").classList.remove("show");
  });

  async function addStock(code) {
    try {
      const r = await fetch("/api/watchlist/" + encodeURIComponent(code), { method: "POST" });
      const j = await r.json();
      if (!r.ok) { toast(j.detail || "添加失败"); return; }
      toast(j.msg);
      await refreshWatch();
      selectStock(code);
    } catch (err) {
      toast("添加失败：" + err.message);
    }
  }

  async function refreshWatch() {
    const r = await (await fetch("/api/watchlist")).json();
    state.quotes = r.quotes;
    state.watchlist = r.codes;
    renderWatchlist();
  }

  // ---------------- 选股 & 详情 ----------------
  async function selectStock(code) {
    state.selected = code;
    renderWatchlist();
    try {
      const d = await (await fetch("/api/quote/" + code)).json();
      state.detail = d;
      renderDetail();
    } catch (err) { toast("获取详情失败"); }
  }

  function renderDetail() {
    const d = state.detail;
    if (!d) return;
    const q = d.quote;
    const c = q.pct > 0 ? "up-c" : q.pct < 0 ? "down-c" : "flat-c";
    $("#detailName").textContent = q.name;
    $("#detailCode").textContent = q.code.slice(2);
    const sc = d.score || {};
    $("#detailScore").textContent = `${sc.label} · ${sc.score > 0 ? "+" : ""}${sc.score}`;
    $("#detailScore").className = "score-pill";
    $("#detailScore").style.color = sc.tone === "bull" ? UP : sc.tone === "bear" ? DOWN : "var(--text-2)";

    const stats = [
      ["最新价", fmt(q.price), c],
      ["涨跌幅", signed(q.pct) + "%", c],
      ["今开", fmt(q.open), ""],
      ["最高", fmt(q.high), "up-c"],
      ["最低", fmt(q.low), "down-c"],
      ["昨收", fmt(q.prev_close), ""],
      ["成交额", fmtAmt(q.amount), ""],
      ["换手率", fmt(q.turnover) + "%", ""],
      ["量比", fmt(q.volume_ratio), q.volume_ratio > 2 ? "up-c" : ""],
      ["振幅", fmt(q.amplitude) + "%", ""],
      ["市盈率", fmt(q.pe), ""],
      ["总市值", fmt(q.market_cap, 0) + "亿", ""],
    ];
    $("#detailStats").innerHTML = stats.map(([k, v, cc]) =>
      `<div class="stat"><div class="k">${k}</div><div class="v ${cc}">${v}</div></div>`
    ).join("");

    // 信号
    const sigs = d.signals || [];
    if (!sigs.length) {
      $("#detailSignals").innerHTML = '<div class="chip info"><span class="ttl">无触发信号</span><span class="dsc">当前指标处于中性区间</span></div>';
    } else {
      $("#detailSignals").innerHTML = sigs.map((s) =>
        `<div class="chip ${s.direction}"><span class="ttl">${s.title}</span><span class="dsc">${s.desc}</span></div>`
      ).join("");
    }

    drawAll();
  }

  // ---------------- 图表 ----------------
  const kc = $("#klineCanvas"), vc = $("#volCanvas");
  const kctx = kc.getContext("2d"), vctx = vc.getContext("2d");

  function setupCanvas(canvas, ctx) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return null;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w: rect.width, h: rect.height };
  }

  function drawAll() {
    if (!state.detail) return;
    drawKline();
    drawVolume();
  }

  function drawKline() {
    const size = setupCanvas(kc, kctx);
    if (!size) return;
    const { w, h } = size;
    kctx.clearRect(0, 0, w, h);
    const bars = state.detail.bars || [];
    if (bars.length < 2) {
      kctx.fillStyle = "#5d6b82";
      kctx.font = "12px sans-serif";
      kctx.fillText("暂无数据", w / 2 - 24, h / 2);
      return;
    }
    const padL = 6, padR = 52, padT = 10, padB = 18;
    const cw = w - padL - padR, ch = h - padT - padB;
    const n = bars.length;
    const highs = bars.map(b => b.high), lows = bars.map(b => b.low);
    let max = Math.max(...highs), min = Math.min(...lows);
    const pad = (max - min) * 0.08 || 1;
    max += pad; min -= pad;
    const y = (v) => padT + (max - v) / (max - min) * ch;
    const bw = cw / n;
    const body = Math.max(1, Math.min(bw * 0.68, 9));

    // 网格 + 右侧价格轴
    kctx.strokeStyle = "rgba(30,39,54,.7)";
    kctx.lineWidth = 1;
    kctx.font = "10px " + getComputedStyle(document.body).getPropertyValue("--mono");
    kctx.textAlign = "left";
    for (let i = 0; i <= 4; i++) {
      const py = padT + (ch / 4) * i;
      const val = max - ((max - min) / 4) * i;
      kctx.beginPath();
      kctx.moveTo(padL, py); kctx.lineTo(padL + cw, py);
      kctx.strokeStyle = "rgba(30,39,54,.55)";
      kctx.stroke();
      kctx.fillStyle = "#5d6b82";
      kctx.fillText(fmt(val), padL + cw + 6, py + 3.5);
    }

    // 均线
    const maCfg = [["ma5", "#f5b342"], ["ma10", "#4d8dff"], ["ma20", "#c084fc"]];
    maCfg.forEach(([key, color]) => {
      const seq = (state.detail.indicators || {})[key] || [];
      kctx.beginPath();
      let started = false;
      seq.forEach((v, i) => {
        if (v === null || v === undefined) { started = false; return; }
        const x = padL + i * bw + bw / 2;
        if (!started) { kctx.moveTo(x, y(v)); started = true; }
        else kctx.lineTo(x, y(v));
      });
      kctx.strokeStyle = color; kctx.lineWidth = 1.1; kctx.globalAlpha = .85;
      kctx.stroke(); kctx.globalAlpha = 1;
    });

    // K 线
    bars.forEach((b, i) => {
      const x = padL + i * bw + bw / 2;
      const isUp = b.close >= b.open;
      const color = isUp ? UP : DOWN;
      kctx.strokeStyle = color;
      kctx.fillStyle = color;
      kctx.lineWidth = 1;
      kctx.beginPath();
      kctx.moveTo(x, y(b.high));
      kctx.lineTo(x, y(b.low));
      kctx.stroke();
      const yo = y(b.open), ycl = y(b.close);
      const top = Math.min(yo, ycl);
      const hgt = Math.max(1, Math.abs(ycl - yo));
      kctx.fillRect(x - body / 2, top, body, hgt);
    });

    // 最新价线
    const last = bars[bars.length - 1];
    const ly = y(last.close);
    kctx.setLineDash([3, 3]);
    kctx.strokeStyle = last.close >= last.open ? "rgba(255,77,94,.55)" : "rgba(23,193,125,.55)";
    kctx.beginPath(); kctx.moveTo(padL, ly); kctx.lineTo(padL + cw, ly); kctx.stroke();
    kctx.setLineDash([]);

    // 时间轴
    kctx.fillStyle = "#5d6b82";
    kctx.textAlign = "center";
    const step = Math.max(1, Math.floor(n / 5));
    for (let i = 0; i < n; i += step) {
      const t = String(bars[i].time);
      const label = state.scale === 240 ? t.slice(5, 10) : t.slice(5, 16);
      kctx.fillText(label, padL + i * bw + bw / 2, h - 5);
    }
  }

  function drawVolume() {
    const size = setupCanvas(vc, vctx);
    if (!size) return;
    const { w, h } = size;
    vctx.clearRect(0, 0, w, h);
    const bars = state.detail.bars || [];
    if (bars.length < 2) return;
    const padL = 6, padR = 52, padT = 4, padB = 4;
    const cw = w - padL - padR, ch = h - padT - padB;
    const bw = cw / bars.length;
    const maxV = Math.max(...bars.map(b => b.volume)) || 1;
    const body = Math.max(1, Math.min(bw * 0.68, 9));
    bars.forEach((b, i) => {
      const x = padL + i * bw + bw / 2;
      const bh = (b.volume / maxV) * ch;
      vctx.fillStyle = b.close >= b.open ? "rgba(255,77,94,.55)" : "rgba(23,193,125,.55)";
      vctx.fillRect(x - body / 2, padT + ch - bh, body, bh);
    });
    vctx.fillStyle = "#5d6b82";
    vctx.font = "10px monospace";
    vctx.textAlign = "left";
    vctx.fillText(fmtAmt(maxV), padL + cw + 6, padT + 9);
  }

  // 图例
  function renderChartMeta() {
    const d = state.detail;
    if (!d) return;
    const ind = d.indicators || {};
    const lastOf = (a) => { const arr = a || []; for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return arr[i]; return null; };
    $("#chartMeta").innerHTML = `
      <span><b style="color:#f5b342">MA5</b> ${fmt(lastOf(ind.ma5))}</span>
      <span><b style="color:#4d8dff">MA10</b> ${fmt(lastOf(ind.ma10))}</span>
      <span><b style="color:#c084fc">MA20</b> ${fmt(lastOf(ind.ma20))}</span>
      <span><b style="color:#9aa8bd">MA60</b> ${fmt(lastOf(ind.ma60))}</span>`;
  }

  const _renderDetail = renderDetail;
  renderDetail = function () { _renderDetail(); renderChartMeta(); };

  // 周期切换
  $("#rangeTabs").addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn || !state.selected) return;
    document.querySelectorAll("#rangeTabs button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.scale = Number(btn.dataset.scale);
    state.len = Number(btn.dataset.len);
    const r = await (await fetch(`/api/kline/${state.selected}?scale=${state.scale}&length=${state.len}`)).json();
    state.detail.bars = r.bars;
    drawAll();
  });

  window.addEventListener("resize", () => { clearTimeout(window._rz); window._rz = setTimeout(drawAll, 150); });

  // ---------------- 信号流 ----------------
  function renderFeed(signals) {
    const feed = $("#signalFeed");
    const fresh = signals.filter(s => !state.feedIds.has(s.id));
    if (!signals.length) {
      feed.innerHTML = '<div class="empty">等待信号…</div>';
      return;
    }
    if (feed.querySelector(".empty")) feed.innerHTML = "";
    // 新信号插入顶部
    fresh.slice().reverse().forEach((s) => {
      state.feedIds.add(s.id);
      const el = document.createElement("div");
      el.className = `sig-card ${s.direction}`;
      const c = s.pct > 0 ? "up-c" : s.pct < 0 ? "down-c" : "flat-c";
      el.innerHTML = `
        <div class="sig-top">
          <div class="sig-name">${s.stock_name || s.name}<span class="cd">${s.code.slice(2)}</span></div>
          <div class="sig-time">${s.time}</div>
        </div>
        <div class="sig-body">
          <span class="sig-ttl">${s.title || s.signal || ""}</span>
          <span class="sig-desc">${s.desc}</span>
          <span class="sig-price ${c}">${fmt(s.price)}</span>
        </div>`;
      feed.appendChild(el);
      if (feed.children.length > 80) feed.removeChild(feed.lastChild);
    });
  }

  // ---------------- 涨跌分布 ----------------
  function renderBreadth(b) {
    if (!b || !b.total) return;
    const ratio = ((b.up / (b.up + b.down || 1)) * 100).toFixed(1);
    $("#breadthSummary").innerHTML = `
      <div class="bs-item"><div class="k">上涨家数</div><div class="v up-c">${b.up}</div></div>
      <div class="bs-item"><div class="k">下跌家数</div><div class="v down-c">${b.down}</div></div>
      <div class="bs-item"><div class="k">涨停</div><div class="v up-c">${b.limit_up}</div></div>
      <div class="bs-item"><div class="k">跌停</div><div class="v down-c">${b.limit_down}</div></div>
      <div class="bs-item" style="grid-column:span 2"><div class="k">上涨占比 · 共 ${b.total} 只</div><div class="v">${ratio}%</div></div>`;

    const buckets = b.buckets || {};
    const keys = ["<-7", "-7~-5", "-5~-3", "-3~0", "0~3", "3~5", "5~7", ">7"];
    const max = Math.max(1, ...keys.map(k => buckets[k] || 0));
    $("#breadthChart").innerHTML = keys.map(k => {
      const v = buckets[k] || 0;
      const neg = k.startsWith("-") || k.startsWith("<");
      const color = neg ? DOWN : UP;
      return `<div class="brow">
        <span class="lbl">${k}%</span>
        <div class="bar-wrap"><div class="bar" style="width:${(v / max * 100).toFixed(1)}%;background:${color}"></div></div>
        <span class="num">${v}</span>
      </div>`;
    }).join("");
  }

  // ---------------- 板块 ----------------
  function renderSectors(list) {
    if (state.sectorSummary || !list || !list.length) return;
    $("#sectorList").innerHTML = list.map(s => {
      const c = s.pct > 0 ? "up-c" : s.pct < 0 ? "down-c" : "flat-c";
      return `<div class="sector-row">
        <div><div class="nm">${s.name}</div>
          <div class="lead">领涨 ${s.lead_name} ${signed(s.lead_pct)}%</div></div>
        <div class="amt">${fmtAmt(s.amount)}</div>
        <div class="pc ${c}">${signed(s.pct)}%</div>
      </div>`;
    }).join("");
  }

  function renderSectorSummary(data) {
    state.sectorSummary = data;
    $("#sectorSummary").textContent = data.summary;
    $("#sectorList").innerHTML = data.sectors.map(s => {
      const c = s.pct > 0 ? "up-c" : s.pct < 0 ? "down-c" : "flat-c";
      const name = String(s.name).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      })[ch]);
      const lead = String(s.lead_name).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      })[ch]);
      return `<div class="sector-row">
        <div><div class="nm">${name}</div>
          <div class="lead">${lead} 领涨 ${signed(s.lead_pct)}%</div>
          <div class="sector-text">${name}今日${s.pct > 0 ? "上涨" : s.pct < 0 ? "下跌" : "持平"}${fmt(Math.abs(s.pct))}%，领涨股为${lead}（${signed(s.lead_pct)}%）。</div>
        </div>
        <div class="amt">${fmtAmt(s.amount)}</div>
        <div class="pc ${c}">${signed(s.pct)}%</div>
      </div>`;
    }).join("");
  }

  async function loadSectorSummary() {
    $("#sectorSummary").textContent = "正在获取最新板块行情并生成文字总结…";
    try {
      const response = await fetch("/api/sectors/summary");
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "板块行情获取失败");
      renderSectorSummary(result);
    } catch (error) {
      $("#sectorSummary").textContent = error.message;
    }
  }

  // ---------------- 榜单 ----------------
  async function loadRank(asc) {
    state.rankAsc = asc;
    document.querySelectorAll(".rank-tabs button").forEach(b =>
      b.classList.toggle("active", Number(b.dataset.asc) === asc));
    const r = await (await fetch(`/api/rank?asc=${asc}&num=20&sort=changepercent`)).json();
    $("#rankList").innerHTML = r.rows.map((x, i) => {
      const c = x.pct > 0 ? "up-c" : "down-c";
      return `<div class="rank-row">
        <span class="i">${i + 1}</span>
        <div class="nm">${x.name}<span>${x.code.slice(2)}</span></div>
        <span class="pr">${fmt(x.price)}</span>
        <span class="pc ${c}">${signed(x.pct)}</span>
      </div>`;
    }).join("");
  }
  document.querySelector(".rank-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) loadRank(Number(b.dataset.asc));
  });

  // ---------------- Tab 切换 ----------------
  $("#feedTabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.tab = b.dataset.tab;
    document.querySelectorAll("#feedTabs button").forEach(x => x.classList.toggle("active", x === b));
    const map = { feed: "tabFeed", breadth: "tabBreadth", sector: "tabSector", rank: "tabRank" };
    Object.entries(map).forEach(([k, id]) => $("#" + id).classList.toggle("hidden", k !== state.tab));
    if (state.tab === "rank") loadRank(state.rankAsc);
    if (state.tab === "sector") loadSectorSummary();
  });

  // ---------------- 应用快照 ----------------
  function applySnapshot(s) {
    if (!s) return;
    setSession(s.session, s.trading);
    $("#footStatus").textContent = `${s.status} · 第 ${s.tick} 轮 · 更新于 ${s.last_update || "—"}`;
    if (s.quotes && s.quotes.length) {
      state.quotes = s.quotes;
      state.watchlist = s.watchlist;
      renderWatchlist();
      // 选中股实时刷新价格
      if (state.selected) {
        const q = s.quotes.find(x => x.code === state.selected);
        if (q) {
          state.detail = state.detail || {};
          state.detail.quote = q;
          state.detail.score = q.score;
          state.detail.signals = q.signals;
          const c = q.pct > 0 ? "up-c" : q.pct < 0 ? "down-c" : "flat-c";
          const stats = document.querySelectorAll("#detailStats .stat .v");
          if (stats.length >= 12) {
            stats[0].textContent = fmt(q.price); stats[0].className = "v " + c;
            stats[1].textContent = signed(q.pct) + "%"; stats[1].className = "v " + c;
            stats[3].textContent = fmt(q.high);
            stats[4].textContent = fmt(q.low);
            stats[6].textContent = fmtAmt(q.amount);
            stats[7].textContent = fmt(q.turnover) + "%";
            stats[8].textContent = fmt(q.volume_ratio);
          }
        }
      }
    }
    if (s.signals) renderFeed(s.signals);
    if (s.overview) applyOverview(s.overview);
  }

  function applyOverview(o) {
    if (!o) return;
    if (o.indexes && o.indexes.length) renderIndexes(o.indexes);
    if (o.breadth && o.breadth.total) renderBreadth(o.breadth);
    if (o.sectors && o.sectors.length) renderSectors(o.sectors);
  }

  // ---------------- WebSocket ----------------
  let ws = null, retry = 0;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { state.connected = true; retry = 0; };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "hello" || msg.type === "tick") {
        applySnapshot(msg.payload);
        if (msg.payload && msg.payload.overview) applyOverview(msg.payload.overview);
        if (msg.type === "hello" && !state.selected && msg.payload.quotes && msg.payload.quotes.length) {
          selectStock(msg.payload.quotes[0].code);
        }
      } else if (msg.type === "overview") {
        applyOverview(msg.payload);
      }
    };
    ws.onclose = () => {
      state.connected = false;
      setSession("重连中", false);
      retry++;
      setTimeout(connect, Math.min(8000, 1000 * retry));
    };
    ws.onerror = () => ws.close();
  }

  // 心跳
  setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 15000);

  // 兜底轮询（WebSocket 不通时仍可用）
  setInterval(() => { if (!state.connected) { fetch("/api/snapshot").then(r => r.json()).then(applySnapshot).catch(() => {}); } }, 6000);

  // 启动
  (async () => {
    try {
      const s = await (await fetch("/api/snapshot")).json();
      applySnapshot(s);
      const o = await (await fetch("/api/overview")).json();
      applyOverview(o);
      if (!state.selected && s.quotes.length) selectStock(s.quotes[0].code);
    } catch (e) { /* ignore */ }
    connect();
  })();
})();
