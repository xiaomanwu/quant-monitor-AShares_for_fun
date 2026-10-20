"""
策略对比：对同一股票池、同一区间、同一成本模型批量回测多个策略，供横向比较。
结果落盘缓存，避免重复计算（回测本身很快，但下载数据慢）。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import universe
from .backtest import run_backtest
from .config import BacktestConfig, STRATEGY_META, STRATEGY_PARAMS

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "compare"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(strategy: str, universe_name: str, cfg: BacktestConfig) -> str:
    return f"{strategy}_{universe_name}_{cfg.start}_{cfg.end}_{cfg.top_n}_{cfg.rebalance_days}"


def compare_strategies(universe_name: str, cfg: BacktestConfig,
                       strategies: list[str] | None = None,
                       use_cache: bool = True) -> dict:
    strategies = strategies or list(STRATEGY_META.keys())
    uni_items = universe.build_universe(universe_name)
    codes = [x["code"] for x in uni_items]
    names = {x["code"]: x["name"] for x in uni_items}

    results = []
    for key in strategies:
        ck = _cache_key(key, universe_name, cfg) + ".json"
        cache_file = CACHE_DIR / ck
        if use_cache and cache_file.exists() and time.time() - cache_file.stat().st_mtime < 3600:
            try:
                results.append(json.loads(cache_file.read_text(encoding="utf-8")))
                continue
            except Exception:
                pass
        try:
            r = run_backtest(key, STRATEGY_PARAMS.get(key, {}), codes, names, cfg)
            item = {
                "key": key,
                "name": STRATEGY_META[key]["name"],
                "tag": STRATEGY_META[key]["tag"],
                "metrics": r.metrics,
                "navs": [round(x, 4) for x in r.navs],
                "dates": r.dates,
            }
            cache_file.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
            results.append(item)
        except Exception as e:
            results.append({"key": key, "name": STRATEGY_META.get(key, {}).get("name", key),
                            "error": str(e)})

    # 按年化排序
    ranked = sorted([r for r in results if "metrics" in r],
                    key=lambda x: x["metrics"]["annual_return"], reverse=True)
    dates = ranked[0]["dates"] if ranked else []
    return {
        "universe": universe_name,
        "config": {"start": cfg.start, "end": cfg.end, "top_n": cfg.top_n,
                   "rebalance_days": cfg.rebalance_days},
        "results": ranked,
        "dates": dates,
        "bench_navs": [],   # 由接口层填充
    }
