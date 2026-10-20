"""
技术指标与信号引擎（纯 Python 实现，无 pandas 依赖，便于部署）。
每个指标函数输入 list[float]，返回 list[float]（不足周期处为 None）。
"""
from __future__ import annotations

import math


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if not values:
        return out
    k = 2 / (n + 1)
    prev = values[0]
    for i, v in enumerate(values):
        prev = v if i == 0 else v * k + prev * (1 - k)
        if i >= n - 1:
            out[i] = prev
    return out


def rsi(values: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(values, fast), ema(values, slow)
    dif = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    valid = [d for d in dif if d is not None]
    dea_valid = ema(valid, signal)
    dea: list[float | None] = [None] * len(dif)
    j = 0
    for i, d in enumerate(dif):
        if d is not None:
            dea[i] = dea_valid[j]
            j += 1
    hist = [None if (a is None or b is None) else (a - b) * 2 for a, b in zip(dif, dea)]
    return dif, dea, hist


def boll(values: list[float], n: int = 20, k: float = 2.0):
    mid = sma(values, n)
    up: list[float | None] = [None] * len(values)
    dn: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1:i + 1]
        m = mid[i]
        if m is None:
            continue
        var = sum((x - m) ** 2 for x in window) / n
        sd = math.sqrt(var)
        up[i] = m + k * sd
        dn[i] = m - k * sd
    return up, mid, dn


def atr(bars: list[dict], n: int = 14) -> list[float | None]:
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["high"] - b["low"])
        else:
            pc = bars[i - 1]["close"]
            trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return sma(trs, n)


def kdj(bars: list[dict], n: int = 9, m1: int = 3, m2: int = 3):
    k_list: list[float | None] = [None] * len(bars)
    d_list: list[float | None] = [None] * len(bars)
    j_list: list[float | None] = [None] * len(bars)
    k = d = 50.0
    for i in range(len(bars)):
        if i < n - 1:
            continue
        window = bars[i - n + 1:i + 1]
        hh = max(x["high"] for x in window)
        ll = min(x["low"] for x in window)
        rsv = 50.0 if hh == ll else (bars[i]["close"] - ll) / (hh - ll) * 100
        k = (m1 - 1) / m1 * k + rsv / m1
        d = (m2 - 1) / m2 * d + k / m2
        k_list[i], d_list[i], j_list[i] = k, d, 3 * k - 2 * d
    return k_list, d_list, j_list


def indicators(bars: list[dict]) -> dict:
    """一次性计算全部指标，返回与 bars 对齐的序列 + 最新值。"""
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    ma5, ma10, ma20, ma60 = sma(closes, 5), sma(closes, 10), sma(closes, 20), sma(closes, 60)
    vma5 = sma(vols, 5)
    dif, dea, hist = macd(closes)
    r6, r12, r24 = rsi(closes, 6), rsi(closes, 12), rsi(closes, 24)
    bu, bm, bd = boll(closes)
    k, d, j = kdj(bars)
    a = atr(bars)
    return {
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "vma5": vma5, "dif": dif, "dea": dea, "macd": hist,
        "rsi6": r6, "rsi12": r12, "rsi24": r24,
        "boll_up": bu, "boll_mid": bm, "boll_dn": bd,
        "k": k, "d": d, "j": j, "atr": a,
    }


def last(seq: list[float | None], idx: int = -1):
    try:
        return seq[idx]
    except IndexError:
        return None


def pct_change(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return (a - b) / b * 100


# ----------------------------------------------------------------------------
# 信号规则
# ----------------------------------------------------------------------------
def rolling_max(values: list[float], n: int) -> list[float | None]:
    """过去 n 日（不含当日）的最高值。"""
    out: list[float | None] = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = max(values[i - n:i])
    return out


def rolling_min(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = min(values[i - n:i])
    return out


class Precomputed:
    """一次性算完全部指标序列，供回测逐日 O(1) 取值（避免重复计算）。"""

    def __init__(self, bars: list[dict]):
        closes = [b["close"] for b in bars]
        vols = [b["volume"] for b in bars]
        self.bars = bars
        self.closes = closes
        self.vols = vols
        self.ind = indicators(bars)
        self.hh20 = rolling_max([b["high"] for b in bars], 20)
        self.ll20 = rolling_min([b["low"] for b in bars], 20)

    def score(self, i: int) -> float:
        """返回第 i 日的多空评分（与 evaluate_signals + score_signals 等价）。"""
        if i < 65:
            return 0.0
        ind = self.ind
        closes = self.closes
        n = i
        price = closes[n]
        score = 0.0

        ma5, ma10, ma20, ma60 = ind["ma5"], ind["ma10"], ind["ma20"], ind["ma60"]
        m5, m10, m20, m60 = ma5[n], ma10[n], ma20[n], ma60[n]

        # 均线金叉/死叉 (权重 2~3)
        if None not in (m5, m10, ma5[n - 1], ma10[n - 1]):
            if ma5[n - 1] <= ma10[n - 1] and m5 > m10:
                score += 36 if m20 and m5 > m20 else 24
            elif ma5[n - 1] >= ma10[n - 1] and m5 < m10:
                score -= 36 if m20 and m5 < m20 else 24

        # 多头/空头排列 (权重 2)
        if None not in (m5, m10, m20, m60):
            if m5 > m10 > m20 > m60:
                score += 24
            elif m5 < m10 < m20 < m60:
                score -= 24

        # MACD 金叉/死叉 (权重 2~3)
        dif, dea = ind["dif"], ind["dea"]
        if None not in (dif[n], dea[n], dif[n - 1], dea[n - 1]):
            if dif[n - 1] <= dea[n - 1] and dif[n] > dea[n]:
                score += 36 if dif[n] > 0 else 24
            elif dif[n - 1] >= dea[n - 1] and dif[n] < dea[n]:
                score -= 36 if dif[n] < 0 else 24

        # 放量突破 / 跌破支撑 (权重 3)
        hh, ll = self.hh20[n], self.ll20[n]
        if hh is not None and price > hh and self.vols[n] > (ind["vma5"][n] or 0) * 1.5:
            score += 36
        if ll is not None and price < ll:
            score -= 36

        # RSI (权重 2)
        r6 = ind["rsi6"][n]
        if r6 is not None:
            if r6 > 85:
                score -= 24
            elif r6 < 15:
                score += 24

        # 布林带 (权重 1)
        bu, bd = ind["boll_up"][n], ind["boll_dn"][n]
        if bu and bd:
            if price > bu:
                score -= 12
            elif price < bd:
                score += 12

        # KDJ 低位金叉 (权重 2)
        kk, dd = ind["k"][n], ind["d"][n]
        if None not in (kk, dd, ind["k"][n - 1], ind["d"][n - 1]):
            if ind["k"][n - 1] <= ind["d"][n - 1] and kk > dd and kk < 30:
                score += 24

        # 回踩 MA20 (权重 2)
        if m20 and ma20[n - 1] and m60:
            dist = (price - m20) / m20 * 100
            if -1.0 <= dist <= 1.0 and (m20 - ma20[n - 1]) > 0 and price > m60:
                score += 24

        return max(-100.0, min(100.0, score))


def _signal(key, name, direction, strength, desc, price) -> dict:
    return {"key": key, "title": name, "direction": direction,
            "strength": strength, "desc": desc, "price": price}


def evaluate_signals(bars: list[dict], quote: dict | None = None) -> list[dict]:
    """
    对单只股票计算信号。direction: bull(看多) / bear(看空) / info(提示)
    strength: 1~3
    """
    if len(bars) < 65:
        return []
    ind = indicators(bars)
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    n = len(bars) - 1
    price = quote["price"] if quote and quote.get("price") else closes[-1]
    pct = quote.get("pct", 0) if quote else 0
    sigs: list[dict] = []

    ma5, ma10, ma20, ma60 = ind["ma5"], ind["ma10"], ind["ma20"], ind["ma60"]
    dif, dea, hist = ind["dif"], ind["dea"], ind["macd"]

    # 1. 均线多空排列（金叉/死叉）
    if all(x is not None for x in (ma5[n], ma10[n], ma20[n], ma5[n - 1], ma10[n - 1])):
        cross_up = ma5[n - 1] <= ma10[n - 1] and ma5[n] > ma10[n]
        cross_dn = ma5[n - 1] >= ma10[n - 1] and ma5[n] < ma10[n]
        if cross_up:
            s = 3 if ma5[n] > ma20[n] else 2
            sigs.append(_signal("ma_golden", "均线金叉", "bull", s,
                                f"MA5 上穿 MA10（{ma5[n]:.2f} > {ma10[n]:.2f}）", price))
        elif cross_dn:
            s = 3 if ma5[n] < ma20[n] else 2
            sigs.append(_signal("ma_dead", "均线死叉", "bear", s,
                                f"MA5 下穿 MA10（{ma5[n]:.2f} < {ma10[n]:.2f}）", price))

    # 2. 多头排列
    if all(x is not None for x in (ma5[n], ma10[n], ma20[n], ma60[n])):
        if ma5[n] > ma10[n] > ma20[n] > ma60[n]:
            sigs.append(_signal("ma_bull_stack", "多头排列", "bull", 2,
                                "MA5 > MA10 > MA20 > MA60，中期趋势向上", price))
        elif ma5[n] < ma10[n] < ma20[n] < ma60[n]:
            sigs.append(_signal("ma_bear_stack", "空头排列", "bear", 2,
                                "MA5 < MA10 < MA20 < MA60，中期趋势向下", price))

    # 3. MACD 金叉 / 死叉 / 零轴
    if all(x is not None for x in (dif[n], dea[n], dif[n - 1], dea[n - 1])):
        if dif[n - 1] <= dea[n - 1] and dif[n] > dea[n]:
            s = 3 if dif[n] > 0 else 2
            zone = "零轴上方" if dif[n] > 0 else "零轴下方"
            sigs.append(_signal("macd_golden", "MACD 金叉", "bull", s,
                                f"DIF 上穿 DEA（{zone}，DIF={dif[n]:.3f}）", price))
        elif dif[n - 1] >= dea[n - 1] and dif[n] < dea[n]:
            s = 3 if dif[n] < 0 else 2
            sigs.append(_signal("macd_dead", "MACD 死叉", "bear", s,
                                f"DIF 下穿 DEA（DIF={dif[n]:.3f}）", price))

    # 4. 放量突破 20 日新高
    if n >= 20:
        hh = max(b["high"] for b in bars[n - 20:n])
        if price > hh and vols[n] > (ind["vma5"][n] or 0) * 1.5:
            sigs.append(_signal("break_high", "放量突破", "bull", 3,
                                f"突破 20 日高点 {hh:.2f}，量能为 5 日均量 {vols[n] / ind['vma5'][n]:.1f} 倍", price))

    # 5. 跌破 20 日新低
    if n >= 20:
        ll = min(b["low"] for b in bars[n - 20:n])
        if price < ll:
            sigs.append(_signal("break_low", "跌破支撑", "bear", 3,
                                f"跌破 20 日低点 {ll:.2f}", price))

    # 6. RSI 超买超卖
    r6 = ind["rsi6"][n]
    if r6 is not None:
        if r6 > 85:
            sigs.append(_signal("rsi_overbought", "RSI 超买", "bear", 2,
                                f"RSI6 = {r6:.1f}，短线过热", price))
        elif r6 < 15:
            sigs.append(_signal("rsi_oversold", "RSI 超卖", "bull", 2,
                                f"RSI6 = {r6:.1f}，短线超跌", price))

    # 7. 布林带
    bu, bd = ind["boll_up"][n], ind["boll_dn"][n]
    if bu and bd:
        if price > bu:
            sigs.append(_signal("boll_upper", "突破布林上轨", "bear", 1,
                                f"价格 {price:.2f} 高于上轨 {bu:.2f}，注意回归", price))
        elif price < bd:
            sigs.append(_signal("boll_lower", "跌破布林下轨", "bull", 1,
                                f"价格 {price:.2f} 低于下轨 {bd:.2f}，关注反弹", price))

    # 8. KDJ
    kk, dd = ind["k"][n], ind["d"][n]
    if kk is not None and dd is not None and ind["k"][n - 1] is not None:
        if ind["k"][n - 1] <= ind["d"][n - 1] and kk > dd and kk < 30:
            sigs.append(_signal("kdj_golden", "KDJ 低位金叉", "bull", 2,
                                f"K={kk:.1f} 上穿 D={dd:.1f}，位于低位", price))

    # 9. 盘中急涨急跌（依赖实时行情）
    if quote and abs(pct) >= 5:
        if pct > 0:
            sigs.append(_signal("surge", "盘中急涨", "info", 2,
                                f"当日涨幅 {pct:+.2f}%，成交额 {quote.get('amount', 0) / 1e8:.2f} 亿", price))
        else:
            sigs.append(_signal("plunge", "盘中急跌", "info", 2,
                                f"当日跌幅 {pct:+.2f}%", price))

    # 10. 量比异动
    vr = (quote or {}).get("volume_ratio", 0)
    if vr >= 2.5:
        sigs.append(_signal("volume_spike", "量比异动", "info", 2,
                            f"量比 {vr:.2f}，明显放量", price))

    # 11. 回踩 20 日线（趋势中的低吸点）
    if ma20[n] and ma20[n - 1] and quote:
        dist = (price - ma20[n]) / ma20[n] * 100
        if -1.0 <= dist <= 1.0 and (ma20[n] - ma20[n - 1]) > 0 and price > ma60[n]:
            sigs.append(_signal("pullback_ma20", "回踩 20 日线", "bull", 2,
                                f"价格贴近 MA20（{ma20[n]:.2f}）且均线上行，趋势内回调", price))

    # 12. 涨停 / 接近涨停
    if quote:
        hl = quote.get("high_limit") or 0
        if hl and price >= hl - 1e-6:
            sigs.append(_signal("limit_up", "涨停", "info", 3,
                                f"封板价 {hl:.2f}", price))
        ll_price = quote.get("low_limit") or 0
        if ll_price and price <= ll_price + 1e-6:
            sigs.append(_signal("limit_down", "跌停", "info", 3,
                                f"跌停价 {ll_price:.2f}", price))

    return sigs


def score_signals(sigs: list[dict]) -> dict:
    """把信号汇总成 -100 ~ +100 的多空评分。"""
    score = 0
    for s in sigs:
        w = s["strength"] * 12
        if s["direction"] == "bull":
            score += w
        elif s["direction"] == "bear":
            score -= w
    score = max(-100, min(100, score))
    if score >= 40:
        label, tone = "强势看多", "bull"
    elif score >= 15:
        label, tone = "偏多", "bull"
    elif score > -15:
        label, tone = "中性", "neutral"
    elif score > -40:
        label, tone = "偏空", "bear"
    else:
        label, tone = "弱势看空", "bear"
    return {"score": score, "label": label, "tone": tone}
