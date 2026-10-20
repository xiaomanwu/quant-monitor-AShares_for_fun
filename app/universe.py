"""
股票池：构建（沪深300/中证500/全A/自选）+ 本地缓存 + 历史K线批量下载（带磁盘缓存与断点续传）。
回测性能的关键：K线一次性落盘，后续回测只读本地。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import market

DATA = Path(__file__).resolve().parent.parent / "data"
DATA.mkdir(exist_ok=True)
UNIVERSE_DIR = DATA / "universe"
KLINE_DIR = DATA / "kline"
UNIVERSE_DIR.mkdir(exist_ok=True)
KLINE_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE_TTL = 86400  # 股票池缓存 1 天
KLINE_TTL = 3600      # 日线缓存 1 小时（盘中会更新当日bar）


# ----------------------------------------------------------------------------
# 股票池
# ----------------------------------------------------------------------------
def _all_a_rows() -> list[dict]:
    """全市场股票列表（新浪分页，10 线程并发）。"""
    from .market import _breadth_page
    with ThreadPoolExecutor(max_workers=10) as ex:
        pages = list(ex.map(lambda p: _breadth_page(p, 100), range(1, 60)))
    rows = [x for d in pages for x in d]
    out = []
    for r in rows:
        sym = r.get("symbol", "")
        name = r.get("name", "")
        price = market._f(r.get("trade"))
        mktcap = market._f(r.get("mktcap")) / 10000  # 万元 -> 亿元
        if not sym or price <= 0:
            continue
        if "ST" in name.upper() or "退" in name:      # 剔除 ST / 退市整理
            continue
        if sym.startswith("bj"):                      # 剔除北交所（流动性差异大）
            continue
        out.append({
            "code": sym, "name": name, "price": price,
            "mktcap": mktcap,
            "nmc": market._f(r.get("nmc")) / 10000,
        })
    return out


def build_universe(kind: str = "csi300", watchlist: list[str] | None = None, top: int | None = None) -> list[dict]:
    """
    kind: csi300 | csi500 | all_a | watchlist
    指数成分用市值近似（免费源无稳定成分股接口，按总市值排序取前 N 是最接近的降级方案）。
    """
    cache_file = UNIVERSE_DIR / f"{kind}.json"

    if kind == "watchlist":
        codes = watchlist or []
        qs = market.fetch_quotes(codes)
        return [{"code": c, "name": qs[c].name if c in qs else c, "price": qs[c].price if c in qs else 0,
                 "mktcap": qs[c].market_cap if c in qs else 0,
                 "nmc": qs[c].float_cap if c in qs else 0}
                for c in codes if c in qs]

    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < UNIVERSE_TTL:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("items"):
                return cached["items"]
        except Exception:
            pass

    rows = _all_a_rows()
    # 按市值降序
    rows.sort(key=lambda x: x["mktcap"], reverse=True)

    if kind == "csi300":
        items = rows[:300]
    elif kind == "csi500":
        items = rows[300:800]
    elif kind == "all_a":
        # 剔除市值过小（<20亿）与百元以下流动性差的，控制回测规模
        items = [x for x in rows if x["mktcap"] >= 20]
    else:
        items = rows[:300]

    if top:
        items = items[:top]

    try:
        cache_file.write_text(json.dumps({"time": time.time(), "items": items}, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        pass
    return items


# ----------------------------------------------------------------------------
# 历史 K 线（磁盘缓存）
# ----------------------------------------------------------------------------
def kline_path(code: str) -> Path:
    return KLINE_DIR / f"{market.normalize(code)}.json"


def get_history(code: str, length: int = 1000, force: bool = False) -> list[dict]:
    """优先读本地缓存；过期则重新拉取。返回按时间升序。"""
    sym = market.normalize(code)
    p = kline_path(sym)
    if not force and p.exists() and time.time() - p.stat().st_mtime < KLINE_TTL:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if len(data) >= 60:
                return data
        except Exception:
            pass
    bars = market.fetch_kline(sym, 240, length, use_cache=False)
    if bars:
        try:
            p.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return bars


def download_history(codes: list[str], workers: int = 12, progress=None) -> dict:
    """批量下载日线到本地。progress(done, total) 回调。"""
    total = len(codes)
    done = 0
    ok = fail = 0

    def task(code):
        return code, get_history(code, 1000)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(task, c): c for c in codes}
        for f in as_completed(futs):
            code = futs[f]
            try:
                _, bars = f.result()
                if bars:
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
            done += 1
            if progress:
                progress(done, total)
    return {"total": total, "ok": ok, "fail": fail}


def load_panel(codes: list[str]) -> dict[str, list[dict]]:
    """从本地缓存加载多只股票的日线，返回 {code: bars}。缺数据的剔除。"""
    panel = {}
    for c in codes:
        p = kline_path(c)
        if not p.exists():
            continue
        try:
            bars = json.loads(p.read_text(encoding="utf-8"))
            if len(bars) >= 60:
                panel[market.normalize(c)] = bars
        except Exception:
            continue
    return panel


def cache_stats() -> dict:
    files = list(KLINE_DIR.glob("*.json"))
    size = sum(f.stat().st_size for f in files)
    names = {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            names[f.stem] = data[-1]["time"] if data else None
        except Exception:
            pass
    return {"files": len(files), "size_mb": round(size / 1024 / 1024, 2), "codes": list(names)}
