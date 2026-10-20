"""FastAPI 应用入口：REST API + WebSocket 实时推送 + 静态页面。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from data.update_daily_a import refresh_daily_a
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import market, indicators
from . import universe as uni
from . import strategies as strat_mod
from . import paper as paper_mod
from . import compare as compare_mod
from . import factor_mining
from .backtest import run_backtest
from .config import BacktestConfig, CostModel, RiskConfig, STRATEGY_META, STRATEGY_PARAMS, UNIVERSE_PRESETS
from .engine import engine

BASE = Path(__file__).resolve().parent.parent
STATIC = BASE / "static"
logger = logging.getLogger(__name__)


def _refresh_daily_a_data() -> None:
    try:
        result = refresh_daily_a()
    except Exception:
        logger.exception("全市场 A 股日线自动补齐失败")
        return
    logger.info(
        "全市场 A 股日线补齐完成：成功 %s/%s，失败 %s",
        result["ok"], result["total"], result["failed"],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_refresh_daily_a_data, daemon=True, name="daily-a-refresh").start()
    engine.start()
    yield
    engine.stop()


app = FastAPI(title="A股量化信号监控台", version="1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))


# ---------------------------- 监控数据 ----------------------------
@app.get("/api/snapshot")
def snapshot():
    return engine.snapshot()


@app.get("/api/overview")
def overview():
    return engine.overview()


@app.get("/api/watchlist")
def get_watchlist():
    return {"codes": engine.watchlist, "quotes": list(engine.quotes.values())}


@app.post("/api/watchlist/{code}")
def add_watch(code: str):
    res = engine.add(code)
    if not res["ok"]:
        raise HTTPException(400, res["msg"])
    return res


@app.delete("/api/watchlist/{code}")
def del_watch(code: str):
    return engine.remove(code)


# ---------------------------- 个股详情 ----------------------------
@app.get("/api/quote/{code}")
def quote_detail(code: str):
    sym = market.normalize(code)
    qs = market.fetch_quotes([sym])
    q = qs.get(sym)
    if not q:
        raise HTTPException(404, "未找到该代码的行情")
    bars = market.fetch_kline(sym, 240, 260)
    d = q.to_dict()
    ind = indicators.indicators(bars) if len(bars) > 5 else {}
    sigs = indicators.evaluate_signals(bars, d)
    return {
        "quote": d,
        "bars": bars[-120:],
        "signals": sigs,
        "score": indicators.score_signals(sigs),
        "indicators": {
            "ma5": ind.get("ma5", [])[-120:],
            "ma10": ind.get("ma10", [])[-120:],
            "ma20": ind.get("ma20", [])[-120:],
            "ma60": ind.get("ma60", [])[-120:],
            "dif": ind.get("dif", [])[-120:],
            "dea": ind.get("dea", [])[-120:],
            "macd": ind.get("macd", [])[-120:],
            "rsi6": (ind.get("rsi6") or [])[-120:],
            "k": (ind.get("k") or [])[-120:],
            "d": (ind.get("d") or [])[-120:],
        },
    }


@app.get("/api/kline/{code}")
def kline(code: str, scale: int = Query(240, description="240日 60/30/5分钟"), length: int = 260):
    sym = market.normalize(code)
    bars = market.fetch_kline(sym, scale, length)
    return {"code": sym, "scale": scale, "bars": bars}


# ---------------------------- 工具接口 ----------------------------
@app.get("/api/search")
def search(q: str = ""):
    return {"results": market.search_symbols(q)}


@app.get("/api/sectors")
def sectors():
    return {"sectors": market.fetch_sectors()[:30]}


@app.get("/api/sectors/summary")
def sector_summary():
    rows = market.fetch_sectors()
    if not rows:
        raise HTTPException(503, "暂时无法获取板块行情")
    up = sum(row["pct"] > 0 for row in rows)
    down = sum(row["pct"] < 0 for row in rows)
    flat = len(rows) - up - down
    average_pct = sum(row["pct"] for row in rows) / len(rows)
    strongest = rows[:3]
    weakest = sorted(rows, key=lambda row: row["pct"])[:3]
    describe = lambda row: f'{row["name"]} {row["pct"]:+.2f}%（领涨：{row["lead_name"]} {row["lead_pct"]:+.2f}%）'
    overview_text = (
        f"行业板块共 {len(rows)} 个：上涨 {up} 个、下跌 {down} 个、平盘 {flat} 个，"
        f"平均涨跌幅 {average_pct:+.2f}%。"
        f'领涨板块：{"；".join(describe(row) for row in strongest)}。'
        f'偏弱板块：{"；".join(describe(row) for row in weakest)}。'
    )
    for row in rows:
        row["summary"] = (
            f'{row["name"]}今日{"上涨" if row["pct"] > 0 else "下跌" if row["pct"] < 0 else "持平"}'
            f'{abs(row["pct"]):.2f}%，领涨股为{row["lead_name"]}（{row["lead_pct"]:+.2f}%）。'
        )
    return {"summary": overview_text, "sectors": rows, "up": up, "down": down, "flat": flat}


@app.get("/api/factors/mine")
def mine_factors(horizon: int = Query(5, description="预测周期：5、10 或 20 个交易日")):
    try:
        return factor_mining.mine_factors(horizon)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.exception("量化因子挖掘失败")
        raise HTTPException(500, f"因子分析失败：{exc}") from exc


@app.get("/api/rank")
def rank(sort: str = "changepercent", asc: int = 0, num: int = 20):
    return {"rows": market.fetch_rank("hs_a", sort, asc, num)}


@app.get("/api/health")
def health():
    return {"ok": True, "tick": engine.tick_count, "status": engine.status}


# ============================================================================
# 量化交易模块：股票池 / 回测 / 策略对比 / 模拟盘
# ============================================================================
class BacktestRequest(BaseModel):
    strategy: str = "momentum"
    params: dict = Field(default_factory=dict)
    universe: str = "csi300"
    start: str = "2023-01-01"
    end: str = "2026-10-19"
    initial_cash: float = 1_000_000
    rebalance_days: int = 5
    top_n: int = 8
    weighting: str = "equal"
    benchmark: str = "sh000300"
    # 成本
    commission_rate: float = 0.00025
    stamp_tax: float = 0.0005
    slippage: float = 0.001
    # 风控
    max_position_pct: float = 0.20
    stop_loss: float = 0.12
    trailing_stop: float = 0.15
    max_drawdown_stop: float = 0.20
    max_holdings: int = 10

    def to_config(self) -> BacktestConfig:
        return BacktestConfig(
            start=self.start, end=self.end, initial_cash=self.initial_cash,
            rebalance_days=max(1, self.rebalance_days), top_n=max(1, self.top_n),
            weighting=self.weighting, benchmark=self.benchmark,
            cost=CostModel(commission_rate=self.commission_rate,
                           stamp_tax=self.stamp_tax, slippage=self.slippage),
            risk=RiskConfig(max_position_pct=self.max_position_pct,
                            max_holdings=self.max_holdings,
                            stop_loss=self.stop_loss,
                            trailing_stop=self.trailing_stop,
                            max_drawdown_stop=self.max_drawdown_stop),
        )


@app.get("/api/strategies")
def get_strategies():
    return {"strategies": strat_mod.list_strategies(),
            "universes": [{"key": k, **v} for k, v in UNIVERSE_PRESETS.items()]}


@app.get("/api/universe/{kind}")
def get_universe(kind: str, limit: int = 20):
    if kind == "watchlist":
        items = uni.build_universe("watchlist", watchlist=engine.watchlist)
    else:
        items = uni.build_universe(kind)
    cached = set(uni.cache_stats().get("codes", []))
    for it in items:
        it["cached"] = it["code"] in cached
    return {"total": len(items), "items": items[:limit],
            "cached_count": sum(1 for x in items if x["cached"])}


@app.post("/api/universe/{kind}/download")
def download_universe(kind: str):
    if kind == "watchlist":
        codes = list(engine.watchlist)
    else:
        codes = [x["code"] for x in uni.build_universe(kind)]
    res = uni.download_history(codes, workers=12)
    res["cached"] = uni.cache_stats()
    return res


@app.get("/api/cache")
def cache_info():
    return uni.cache_stats()


@app.post("/api/backtest")
def api_backtest(req: BacktestRequest):
    try:
        items = (uni.build_universe("watchlist", watchlist=engine.watchlist)
                 if req.universe == "watchlist" else uni.build_universe(req.universe))
        codes = [x["code"] for x in items]
        names = {x["code"]: x["name"] for x in items}
        if not codes:
            raise HTTPException(400, "股票池为空")

        # 数据缺失先补下载
        panel = uni.load_panel(codes)
        missing = [c for c in codes if market.normalize(c) not in panel]
        if len(missing) > len(codes) * 0.3:
            uni.download_history(missing, workers=10)
            panel = uni.load_panel(codes)

        cfg = req.to_config()
        result = run_backtest(req.strategy, req.params, list(panel.keys()), names, cfg)
        data = result.to_dict()
        data["universe"] = {"key": req.universe, "count": len(panel),
                            "missing": len(missing)}
        data["strategy"] = {"key": req.strategy,
                            "name": STRATEGY_META.get(req.strategy, {}).get("name", req.strategy),
                            "params": {**STRATEGY_PARAMS.get(req.strategy, {}), **req.params}}
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"回测失败：{e}")


@app.post("/api/compare")
def api_compare(req: BacktestRequest):
    try:
        cfg = req.to_config()
        res = compare_mod.compare_strategies(req.universe, cfg)
        # 附加基准
        bench = uni.get_history(cfg.benchmark, 1000)
        if bench:
            base = None
            navs = []
            dates = res["dates"]
            idx = {b["time"]: b["close"] for b in bench}
            last = None
            for d in dates:
                if d in idx:
                    if base is None:
                        base = idx[d]
                    last = idx[d] / base
                navs.append(round(last or 1.0, 4))
            res["bench_navs"] = navs
        return res
    except Exception as e:
        raise HTTPException(500, f"策略对比失败：{e}")


# ---------------------------- 模拟盘 ----------------------------
class PaperInitRequest(BaseModel):
    strategy: str = "momentum"
    params: dict = Field(default_factory=dict)
    universe: str = "csi300"
    initial_cash: float = 1_000_000


@app.get("/api/paper")
def paper_summary():
    try:
        acc = paper_mod.get_account()
        s = acc.summary()
        s["has_account"] = bool(s.get("created"))
        return s
    except Exception as e:
        raise HTTPException(500, f"读取模拟盘失败：{e}")


@app.post("/api/paper/init")
def paper_init(req: PaperInitRequest):
    acc = paper_mod.PaperAccount.create(req.strategy, req.params, req.universe, req.initial_cash)
    return {"ok": True, "summary": acc.summary()}


@app.post("/api/paper/run")
def paper_run(force: bool = True):
    try:
        acc = paper_mod.get_account()
        if not acc.state.get("created"):
            raise HTTPException(400, "请先创建模拟盘账户")
        res = acc.run_daily(force=force)
        res["summary"] = acc.summary()
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"模拟盘执行失败：{e}")


@app.post("/api/paper/reset")
def paper_reset():
    if paper_mod.STATE_FILE.exists():
        paper_mod.STATE_FILE.unlink()
    return {"ok": True}


# ---------------------------- WebSocket ----------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()

    def send(msg: dict):
        asyncio.run_coroutine_threadsafe(ws.send_text(json.dumps(msg, ensure_ascii=False)), loop)

    engine.register(send)
    try:
        send({"type": "hello", "payload": engine.snapshot(core=True)})
        while True:
            # 保持连接；客户端心跳
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        engine.unregister(send)
