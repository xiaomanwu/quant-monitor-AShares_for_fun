"""
绩效分析：由净值序列计算全部评价指标。
输入 navs = [(date, nav), ...]，trades = [dict, ...]
"""
from __future__ import annotations

import math

TRADING_DAYS = 252


def max_drawdown(navs: list[float]) -> tuple[float, int, int]:
    """返回 (最大回撤绝对值, 峰值索引, 谷值索引)"""
    peak = navs[0] if navs else 1.0
    peak_i = 0
    mdd = 0.0
    mdd_i = mdd_j = 0
    for i, v in enumerate(navs):
        if v > peak:
            peak, peak_i = v, i
        dd = (peak - v) / peak if peak else 0
        if dd > mdd:
            mdd, mdd_i, mdd_j = dd, peak_i, i
    return mdd, mdd_i, mdd_j


def drawdown_series(navs: list[float]) -> list[float]:
    out = []
    peak = navs[0] if navs else 1.0
    for v in navs:
        peak = max(peak, v)
        out.append((v - peak) / peak * 100 if peak else 0)
    return out


def annualized_return(nav_start: float, nav_end: float, days: int) -> float:
    if nav_start <= 0 or days <= 0:
        return 0.0
    return (nav_end / nav_start) ** (TRADING_DAYS / days) - 1


def volatility(returns: list[float]) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def sharpe(returns: list[float], rf: float = 0.02) -> float:
    vol = volatility(returns)
    if vol == 0:
        return 0.0
    ann = (sum(returns) / len(returns)) * TRADING_DAYS if returns else 0
    return (ann - rf) / vol


def sortino(returns: list[float], rf: float = 0.02) -> float:
    if not returns:
        return 0.0
    ann = (sum(returns) / len(returns)) * TRADING_DAYS
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    dd_vol = math.sqrt(sum(r ** 2 for r in downside) / len(downside)) * math.sqrt(TRADING_DAYS)
    return (ann - rf) / dd_vol if dd_vol else 0.0


def alpha_beta(rets: list[float], bench: list[float], rf: float = 0.02) -> tuple[float, float]:
    """对基准回归，返回 (年化Alpha, Beta)"""
    n = min(len(rets), len(bench))
    if n < 10:
        return 0.0, 1.0
    r, b = rets[:n], bench[:n]
    mb = sum(b) / n
    mr = sum(r) / n
    cov = sum((b[i] - mb) * (r[i] - mr) for i in range(n)) / (n - 1)
    var = sum((x - mb) ** 2 for x in b) / (n - 1)
    beta = cov / var if var else 1.0
    alpha_daily = mr - (rf / TRADING_DAYS) - beta * (mb - rf / TRADING_DAYS)
    return alpha_daily * TRADING_DAYS, beta


def trade_stats(trades: list[dict]) -> dict:
    """按「买卖配对」计算胜率与盈亏比（FIFO 近似：按股票累计成本）。"""
    by_code: dict[str, list[dict]] = {}
    for t in trades:
        by_code.setdefault(t["code"], []).append(t)

    closed = []   # 已实现盈亏
    for code, ts in by_code.items():
        pos = 0.0
        cost = 0.0
        for t in sorted(ts, key=lambda x: (x["date"], x.get("seq", 0))):
            qty = t["shares"]
            if t["side"] == "buy":
                cost += t["amount"] + t["fee"]
                pos += qty
            else:
                if pos > 0:
                    sold = min(qty, pos)
                    avg = cost / pos
                    pnl = (t["amount"] - t["fee"]) - avg * sold
                    closed.append(pnl)
                    cost -= avg * sold
                    pos -= sold
        if pos > 0:   # 未平仓按成本计（不计盈亏）
            pass

    wins = [p for p in closed if p > 0]
    losses = [p for p in closed if p <= 0]
    total = len(closed)
    win_rate = len(wins) / total * 100 if total else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else 0.0
    return {
        "closed_trades": total,
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 0),
        "avg_loss": round(avg_loss, 0),
        "profit_factor": round(profit_factor, 2),
        "trade_count": len(trades),
    }


def metrics(dates: list[str], navs: list[float], bench_navs: list[float],
            trades: list[dict], initial: float, positions_daily: list[int] | None = None) -> dict:
    if not navs:
        return {}
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    bench_rets = [bench_navs[i] / bench_navs[i - 1] - 1 for i in range(1, len(bench_navs))] \
        if bench_navs else []

    days = len(navs)
    total_ret = navs[-1] / navs[0] - 1
    ann_ret = annualized_return(navs[0], navs[-1], days)
    vol = volatility(rets)
    mdd, pi, di = max_drawdown(navs)
    mdd_bench, _, _ = max_drawdown(bench_navs) if bench_navs else (0, 0, 0)
    alpha, beta = alpha_beta(rets, bench_rets) if bench_rets else (0.0, 1.0)
    bench_total = bench_navs[-1] / bench_navs[0] - 1 if bench_navs else 0

    # 年化换手率 = 日均成交额 / 平均资产 × 252（单边计算，买卖各计一次）
    total_turnover = sum(t["amount"] for t in trades)
    avg_nav = (sum(navs) / len(navs)) * initial if navs else initial
    turnover_ann = total_turnover / avg_nav / days * TRADING_DAYS * 100 if avg_nav > 0 else 0

    return {
        "total_return": round(total_ret * 100, 2),
        "annual_return": round(ann_ret * 100, 2),
        "bench_total_return": round(bench_total * 100, 2),
        "excess_return": round((total_ret - bench_total) * 100, 2),
        "annual_volatility": round(vol * 100, 2),
        "sharpe": round(sharpe(rets), 2),
        "sortino": round(sortino(rets), 2),
        "max_drawdown": round(mdd * 100, 2),
        "max_drawdown_bench": round(mdd_bench * 100, 2),
        "calmar": round(ann_ret / mdd, 2) if mdd > 0 else 0.0,
        "alpha": round(alpha * 100, 2),
        "beta": round(beta, 2),
        "turnover_ann": round(turnover_ann, 1),
        "days": days,
        "start": dates[0] if dates else "",
        "end": dates[-1] if dates else "",
        "final_nav": round(initial * navs[-1], 2),
        **trade_stats(trades),
    }


def monthly_returns(dates: list[str], navs: list[float]) -> list[dict]:
    """按自然月聚合收益，供热力图。"""
    out = []
    cur = None
    start_nav = navs[0] if navs else 1
    for i, d in enumerate(dates):
        ym = d[:7]
        if cur is None:
            cur = ym
        elif ym != cur:
            out.append({"month": cur, "ret": round((navs[i - 1] / start_nav - 1) * 100, 2)})
            cur = ym
            start_nav = navs[i - 1]
    if cur:
        out.append({"month": cur, "ret": round((navs[-1] / start_nav - 1) * 100, 2)})
    return out
