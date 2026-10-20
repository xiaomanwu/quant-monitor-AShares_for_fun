"""
策略库：5 个内置策略，统一接口。

设计要点（避免未来函数）：
- score() 只能使用 bars[:i+1]（截至当日收盘的数据），返回打分供次日决策
- 回测引擎在 t 日收盘后调 score，t+1 日开盘成交
- 策略本身不关心资金、持仓、成本，只管"打分/排序"
"""
from __future__ import annotations

import math
from typing import Protocol

from . import indicators
from .config import STRATEGY_META, STRATEGY_PARAMS


class Strategy(Protocol):
    key: str
    name: str

    def score(self, bars: list[dict], i: int) -> float | None:
        """返回该股票在 i 日的评分（越高越看多）；None = 不参与候选。"""
        ...


# ----------------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------------
def _closes(bars, i, n):
    lo = i - n + 1
    if lo < 0:
        return None
    return [b["close"] for b in bars[lo:i + 1]]


def _sma_at(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _std_at(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    w = closes[-n:]
    m = sum(w) / n
    return math.sqrt(sum((x - m) ** 2 for x in w) / n)


# ----------------------------------------------------------------------------
# 1. 信号评分选股（复用监控台 12 条规则）
# ----------------------------------------------------------------------------
class SignalScoreStrategy:
    key = "signal_score"
    name = "信号评分选股"

    def __init__(self, min_score: float = 20):
        self.min_score = min_score
        self._pre: dict[int, indicators.Precomputed] = {}

    def bind(self, bars: list[dict]) -> None:
        """回测前调用：预计算该股票全部指标序列，后续逐日 O(1) 取值。"""
        self._pre[id(bars)] = indicators.Precomputed(bars)

    def score(self, bars, i):
        if i < 65:
            return None
        pre = self._pre.get(id(bars))
        if pre is None:
            pre = self._pre.setdefault(id(bars), indicators.Precomputed(bars))
        sc = pre.score(i)
        # 低于门槛直接排除（返回 None，不进候选池）
        return sc if sc >= self.min_score else None

    def signals_at(self, bars, i):
        if i < 65:
            return []
        pre = self._pre.get(id(bars)) or indicators.Precomputed(bars)
        # 复用原规则做可读描述（仅用于展示，不参与回测）
        return indicators.evaluate_signals(bars[max(0, i - 260):i + 1], None)


# ----------------------------------------------------------------------------
# 2. 双均线交叉
# ----------------------------------------------------------------------------
class MaCrossStrategy:
    key = "ma_cross"
    name = "双均线交叉"

    def __init__(self, fast: int = 5, slow: int = 20):
        self.fast, self.slow = fast, slow

    def score(self, bars, i):
        if i < self.slow + 2:
            return None
        closes = _closes(bars, i, self.slow + 2)
        if not closes:
            return None
        f_now = _sma_at(closes, self.fast)
        s_now = _sma_at(closes, self.slow)
        f_prev = _sma_at(closes[:-1], self.fast)
        s_prev = _sma_at(closes[:-1], self.slow)
        if None in (f_now, s_now, f_prev, s_prev):
            return None
        # 站上慢线才可买入；金叉得分最高
        if f_now <= s_now:
            return None
        gold = f_prev <= s_prev
        # 用「偏离度」做细分：趋势刚形成时偏离小，买入性价比高
        dev = (f_now - s_now) / s_now * 100
        base = 100 if gold else 60
        return base - abs(dev) * 2


# ----------------------------------------------------------------------------
# 3. 动量轮动
# ----------------------------------------------------------------------------
class MomentumStrategy:
    key = "momentum"
    name = "动量轮动"

    def __init__(self, lookback: int = 20, skip: int = 0):
        self.lookback, self.skip = lookback, skip

    def score(self, bars, i):
        end = i - self.skip
        start = end - self.lookback
        if start < 0:
            return None
        p0 = bars[start]["close"]
        p1 = bars[end]["close"]
        if p0 <= 0:
            return None
        # 过滤：价格需在 20 日均线上方，避免接刀
        closes = _closes(bars, i, 20)
        ma20 = _sma_at(closes, 20) if closes else None
        if ma20 and bars[i]["close"] < ma20:
            return None
        return (p1 / p0 - 1) * 100


# ----------------------------------------------------------------------------
# 4. 布林带均值回归
# ----------------------------------------------------------------------------
class BollRevertStrategy:
    key = "boll_revert"
    name = "布林带均值回归"

    def __init__(self, period: int = 20, k: float = 2.0):
        self.period, self.k = period, k

    def score(self, bars, i):
        if i < self.period + 1:
            return None
        closes = _closes(bars, i, self.period)
        if not closes:
            return None
        mid = _sma_at(closes, self.period)
        sd = _std_at(closes, self.period)
        if not mid or not sd:
            return None
        lower = mid - self.k * sd
        price = bars[i]["close"]
        # 只在下轨附近买入；越接近下轨分越高
        if price > lower * 1.01:
            return None
        # 长期趋势不能是崩盘状态：60 日跌幅过大的排除
        if i >= 60:
            chg60 = bars[i]["close"] / bars[i - 60]["close"] - 1
            if chg60 < -0.35:
                return None
        z = (price - mid) / sd
        return (-z) * 30   # z 越负（越超跌）分数越高


# ----------------------------------------------------------------------------
# 5. MACD 趋势
# ----------------------------------------------------------------------------
class MacdTrendStrategy:
    key = "macd_trend"
    name = "MACD 趋势"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast, self.slow, self.signal = fast, slow, signal

    def score(self, bars, i):
        need = self.slow + self.signal + 2
        if i < need:
            return None
        # 增量计算：EMA 只依赖「上一日 EMA + 今日收盘」，无需回溯整段序列
        # 预热 200 日足够让 EMA 收敛
        start = max(0, i - 200)
        kf, ks, kd = 2 / (self.fast + 1), 2 / (self.slow + 1), 2 / (self.signal + 1)
        ef = es = bars[start]["close"]
        dea = 0.0
        prev_dif = prev_dea = None
        for k in range(start, i + 1):
            c = bars[k]["close"]
            ef = c * kf + ef * (1 - kf)
            es = c * ks + es * (1 - ks)
            dif = ef - es
            dea = dif * kd + dea * (1 - kd)
            if k < i:
                prev_dif, prev_dea = dif, dea
        if prev_dif is None or prev_dea is None:
            return None
        # 必须 DIF > DEA 且 DIF > 0（零轴上方 = 中期趋势向上）
        if dif <= dea or dif <= 0:
            return None
        just_crossed = prev_dif <= prev_dea
        hist = (dif - dea) * 2
        strength = hist / (abs(dif) + 1e-9)
        base = 100 if just_crossed else 55
        return base + min(strength * 50, 30)


# ----------------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------------
_REGISTRY = {
    "signal_score": SignalScoreStrategy,
    "ma_cross": MaCrossStrategy,
    "momentum": MomentumStrategy,
    "boll_revert": BollRevertStrategy,
    "macd_trend": MacdTrendStrategy,
}


def build_strategy(key: str, params: dict | None = None) -> Strategy:
    if key not in _REGISTRY:
        raise ValueError(f"未知策略：{key}")
    p = dict(STRATEGY_PARAMS.get(key, {}))
    if params:
        # 只接受已知参数，避免前端传错导致 TypeError
        p.update({k: v for k, v in params.items() if k in p})
    return _REGISTRY[key](**p)


def list_strategies() -> list[dict]:
    out = []
    for k, meta in STRATEGY_META.items():
        out.append({
            "key": k, "name": meta["name"], "desc": meta["desc"], "tag": meta["tag"],
            "params": STRATEGY_PARAMS.get(k, {}),
        })
    return out


# ----------------------------------------------------------------------------
# 策略信号序列化（供前端画买卖点）
# ----------------------------------------------------------------------------
def decision_series(strategy: Strategy, bars: list[dict], threshold: float = 0) -> list[dict]:
    """逐日给出策略是否看多，用于绘制买卖点。"""
    out = []
    prev = False
    for i in range(len(bars)):
        s = strategy.score(bars, i)
        hold = s is not None and s > threshold
        if hold != prev:
            out.append({"time": bars[i]["time"], "price": bars[i]["close"],
                        "action": "buy" if hold else "sell", "score": s})
        prev = hold
    return out
