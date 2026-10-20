"""Cross-sectional technical-factor evaluation on locally stored daily bars."""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "daily_a"
FACTOR_META = {
    "momentum_20": ("20日动量", "过去20个交易日收益率"),
    "reversal_5": ("5日反转", "过去5个交易日收益率（负向可解释为反转）"),
    "volatility_20": ("20日波动率", "过去20个交易日收益率标准差"),
    "volume_ratio_20": ("成交量活跃度", "当日成交量 / 前20日平均成交量 - 1"),
    "price_vs_ma20": ("价格偏离均线", "收盘价 / 20日均线 - 1"),
}
_CACHE_TTL = 900
_cache: dict[tuple[int, int], tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _load_bars(path: Path, cutoff: str) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    bars = []
    for bar in raw:
        if not isinstance(bar, dict):
            continue
        day = str(bar.get("time", ""))[:10]
        try:
            close = float(bar["close"])
            volume = float(bar["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        if len(day) != 10 or day < cutoff or close <= 0 or volume < 0:
            continue
        bars.append({"time": day, "close": close, "volume": volume})
    bars.sort(key=lambda bar: bar["time"])
    return bars


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average_rank = (start + 1 + end) / 2
        for pos in range(start, end):
            ranks[order[pos]] = average_rank
        start = end
    return ranks


def _spearman(observations: list[tuple[float, float]]) -> float:
    x_rank = _rank([row[0] for row in observations])
    y_rank = _rank([row[1] for row in observations])
    x_mean = statistics.fmean(x_rank)
    y_mean = statistics.fmean(y_rank)
    covariance = sum((x - x_mean) * (y - y_mean)
                     for x, y in zip(x_rank, y_rank))
    x_ss = sum((x - x_mean) ** 2 for x in x_rank)
    y_ss = sum((y - y_mean) ** 2 for y in y_rank)
    denominator = math.sqrt(x_ss * y_ss)
    return covariance / denominator if denominator else 0.0


def _factor_values(bars: list[dict], index: int) -> dict[str, float]:
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(index - 19, index + 1)]
    avg_volume = statistics.fmean(volumes[index - 20:index])
    avg_close = statistics.fmean(closes[index - 19:index + 1])
    return {
        "momentum_20": closes[index] / closes[index - 20] - 1,
        "reversal_5": closes[index] / closes[index - 5] - 1,
        "volatility_20": statistics.pstdev(returns),
        "volume_ratio_20": volumes[index] / avg_volume - 1 if avg_volume else 0.0,
        "price_vs_ma20": closes[index] / avg_close - 1,
    }


def _calculate(horizon: int) -> dict:
    paths = sorted(path for path in DATA_DIR.glob("*.json") if path.name != "manifest.json")
    if not paths:
        raise RuntimeError("尚无全市场日线数据，请先运行 data.update_daily_a")

    cutoff = (date.today() - timedelta(days=365)).isoformat()
    all_dates: set[str] = set()
    for path in paths:
        all_dates.update(bar["time"] for bar in _load_bars(path, cutoff))
    if not all_dates:
        raise RuntimeError("近一年日线数据为空或格式无效")

    snapshot_dates = set(sorted(all_dates)[::horizon])
    cross_sections: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    usable_stocks = 0
    for path in paths:
        bars = _load_bars(path, cutoff)
        if len(bars) < 26:
            continue
        usable_stocks += 1
        for index in range(20, len(bars) - horizon):
            day = bars[index]["time"]
            if day not in snapshot_dates:
                continue
            forward_return = bars[index + horizon]["close"] / bars[index]["close"] - 1
            for key, value in _factor_values(bars, index).items():
                if math.isfinite(value) and math.isfinite(forward_return):
                    cross_sections[day][key].append((value, forward_return))

    factor_results = []
    for key, (name, description) in FACTOR_META.items():
        daily_ic = []
        top_returns = []
        bottom_returns = []
        sample_count = 0
        for day in sorted(cross_sections):
            observations = cross_sections[day].get(key, [])
            if len(observations) < 30:
                continue
            daily_ic.append(_spearman(observations))
            sample_count += len(observations)
            ordered = sorted(observations, key=lambda row: row[0])
            group_size = max(1, len(ordered) // 5)
            bottom_returns.append(statistics.fmean(row[1] for row in ordered[:group_size]))
            top_returns.append(statistics.fmean(row[1] for row in ordered[-group_size:]))

        if not daily_ic:
            continue
        mean_ic = statistics.fmean(daily_ic)
        ic_std = statistics.pstdev(daily_ic)
        factor_results.append({
            "key": key,
            "name": name,
            "description": description,
            "mean_ic": mean_ic,
            "icir": mean_ic / ic_std * math.sqrt(252 / horizon) if ic_std else 0.0,
            "positive_ratio": sum(value > 0 for value in daily_ic) / len(daily_ic),
            "top_return": statistics.fmean(top_returns),
            "bottom_return": statistics.fmean(bottom_returns),
            "spread": statistics.fmean(top_returns) - statistics.fmean(bottom_returns),
            "periods": len(daily_ic),
            "samples": sample_count,
        })

    manifest_path = DATA_DIR / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    return {
        "horizon": horizon,
        "start_date": min(all_dates),
        "end_date": max(all_dates),
        "stock_count": usable_stocks,
        "snapshot_count": len(cross_sections),
        "data_updated_at": manifest.get("updated_at"),
        "method": "每5个交易日抽样的横截面 Spearman IC；ICIR 年化；按因子值分五组比较最高/最低组未来收益",
        "factors": sorted(factor_results, key=lambda result: abs(result["mean_ic"]), reverse=True),
    }


def mine_factors(horizon: int = 5) -> dict:
    if horizon not in (5, 10, 20):
        raise ValueError("预测周期仅支持 5、10、20 个交易日")
    manifest_path = DATA_DIR / "manifest.json"
    manifest_mtime = manifest_path.stat().st_mtime_ns if manifest_path.exists() else 0
    cache_key = (horizon, manifest_mtime)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]
        result = _calculate(horizon)
        _cache.clear()
        _cache[cache_key] = (now, result)
        return result
