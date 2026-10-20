"""
事件驱动回测引擎（日频）。

真实性与可复现性要点：
1. 无未来函数：t 日收盘后用 bars[:t+1] 计算信号，t+1 日开盘成交
2. T+1：当日买入的股票当日不可卖出（本引擎仅在调仓日执行，天然满足）
3. 涨跌停不可成交：开盘价触及涨停（买入）/ 跌停（卖出）则跳过
4. 停牌：当日无成交量或价格异常则跳过
5. 成本：佣金（含最低5元）+ 过户费 + 印花税（卖出）+ 滑点
6. 整手：买入按 100 股向下取整
7. 组合回撤熔断、个股止损、移动止损

流程：预计算所有股票在每日的策略分数（向量化缓存）→ 逐交易日推进 → 调仓日生成目标组合
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import performance
from .config import BacktestConfig, CostModel, RiskConfig
from .strategies import Strategy


@dataclass
class Position:
    code: str
    shares: int = 0
    cost: float = 0.0        # 总成本（含费用）
    avg_price: float = 0.0
    peak_price: float = 0.0  # 持仓期间最高价（移动止损用）


@dataclass
class BTResult:
    dates: list[str] = field(default_factory=list)
    navs: list[float] = field(default_factory=list)          # 归一化净值（起点=1）
    bench_navs: list[float] = field(default_factory=list)
    drawdowns: list[float] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    daily_positions: list[int] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    monthly: list[dict] = field(default_factory=list)
    holdings: list[dict] = field(default_factory=list)       # 期末持仓
    equity_curve: list[float] = field(default_factory=list)  # 绝对资产
    benchmark_name: str = ""

    def to_dict(self) -> dict:
        return {
            "dates": self.dates,
            "navs": [round(x, 4) for x in self.navs],
            "bench_navs": [round(x, 4) for x in self.bench_navs],
            "drawdowns": [round(x, 2) for x in self.drawdowns],
            "trades": self.trades,
            "daily_positions": self.daily_positions,
            "metrics": self.metrics,
            "monthly": self.monthly,
            "holdings": self.holdings,
            "equity_curve": [round(x, 2) for x in self.equity_curve],
            "benchmark_name": self.benchmark_name,
        }


class BacktestEngine:
    def __init__(self, strategy: Strategy, panel: dict[str, list[dict]],
                 names: dict[str, str], cfg: BacktestConfig):
        self.strategy = strategy
        self.panel = panel                      # {code: bars(升序)}
        self.names = names
        self.cfg = cfg
        self.cost: CostModel = cfg.cost
        self.risk: RiskConfig = cfg.risk

        self.cash = cfg.initial_cash
        self.positions: dict[str, Position] = {}
        self.trades: list[dict] = []
        self._seq = 0

        # 按日期对齐：交易日历取自数据最全的股票（通常是基准或上市最早的）
        self.calendar = self._build_calendar()
        self.index_of: dict[str, dict[str, int]] = {}
        for code, bars in panel.items():
            self.index_of[code] = {b["time"]: i for i, b in enumerate(bars)}

        self._score_cache: dict[str, list[float | None]] = {}
        self._atr_cache: dict[str, list[float | None]] = {}

    # ------------------------------------------------------------------
    # 准备
    # ------------------------------------------------------------------
    def _build_calendar(self) -> list[str]:
        # 以所有股票日期并集为准，再裁剪到配置区间
        dates: set[str] = set()
        for bars in self.panel.values():
            for b in bars:
                dates.add(b["time"])
        cal = sorted(d for d in dates if self.cfg.start <= d <= self.cfg.end)
        return cal

    def _prepare_scores(self) -> None:
        """预计算：每只股票在每个交易日的策略分数。这是最耗时的步骤，只做一次。"""
        # 策略若支持预计算（如信号评分），先绑定整个序列，避免逐日重复计算指标
        if hasattr(self.strategy, "bind"):
            for bars in self.panel.values():
                try:
                    self.strategy.bind(bars)
                except Exception:
                    pass
        for code, bars in self.panel.items():
            idx = self.index_of[code]
            scores: list[float | None] = [None] * len(self.calendar)
            # 只计算回测区间内（但要保证有足够历史）
            for t, d in enumerate(self.calendar):
                i = idx.get(d)
                if i is None or i < 65:
                    continue
                try:
                    scores[t] = self.strategy.score(bars, i)
                except Exception:
                    scores[t] = None
            self._score_cache[code] = scores

    def bar_at(self, code: str, date: str) -> dict | None:
        i = self.index_of[code].get(date)
        if i is None:
            return None
        return self.panel[code][i]

    # ------------------------------------------------------------------
    # 账户
    # ------------------------------------------------------------------
    def equity(self, date: str, use_open: bool = False) -> float:
        total = self.cash
        for code, pos in self.positions.items():
            b = self.bar_at(code, date)
            if not b:
                total += pos.avg_price * pos.shares   # 停牌按成本估值
                continue
            price = b["open"] if use_open else b["close"]
            total += price * pos.shares
        return total

    def _record(self, date, code, side, price, shares, amount, fee, reason):
        self._seq += 1
        self.trades.append({
            "date": date, "code": code, "name": self.names.get(code, code),
            "side": side, "price": round(price, 3), "shares": shares,
            "amount": round(amount, 2), "fee": round(fee, 2),
            "reason": reason, "seq": self._seq,
        })

    def _can_buy(self, bar: dict) -> bool:
        # 开盘价触及涨停视为买不进（近似：开盘涨幅 >= 9.7%；创业板/科创板 19.7% 由 high_limit 判断）
        if bar.get("volume", 0) <= 0:
            return False
        if bar["open"] >= bar["high"] and bar["open"] == bar["low"]:
            return False   # 一字板
        return True

    def _can_sell(self, bar: dict) -> bool:
        if bar.get("volume", 0) <= 0:
            return False
        if bar["open"] <= bar["low"] and bar["open"] == bar["high"]:
            return False   # 一字跌停
        return True

    def _limit_pct(self, code: str) -> float:
        bare = code[2:] if len(code) > 6 else code
        if bare.startswith(("300", "301", "688")):
            return 0.197
        if bare.startswith(("8", "4", "92")):
            return 0.297
        return 0.097

    def buy(self, date: str, code: str, price: float, target_amount: float, reason: str) -> bool:
        if target_amount <= 0 or code in self.positions:
            return False
        bar = self.bar_at(code, date)
        if not bar or not self._can_buy(bar):
            return False
        # 涨停判断
        prev_bar_idx = self.index_of[code].get(date)
        if prev_bar_idx is not None and prev_bar_idx > 0:
            prev_close = self.panel[code][prev_bar_idx - 1]["close"]
            if prev_close > 0 and (bar["open"] / prev_close - 1) >= self._limit_pct(code) - 0.002:
                return False
        # 滑点
        exec_price = price * (1 + self.cost.slippage)
        # 冲击成本约束：不超过当日成交额的一定比例（数据源无成交额时按「量×价」估算）
        day_amount = bar.get("amount") or (bar.get("volume", 0) * price)
        max_amount = day_amount * self.cost.impact_ratio if day_amount else target_amount
        budget = min(target_amount, max_amount, self.cash * (1 - self.risk.cash_buffer))
        shares = int(budget / exec_price / 100) * 100
        if shares < 100:
            return False
        amount = shares * exec_price
        fee = self.cost.buy_cost(amount)
        if amount + fee > self.cash:
            shares -= 100
            if shares < 100:
                return False
            amount = shares * exec_price
            fee = self.cost.buy_cost(amount)
        self.cash -= amount + fee
        self.positions[code] = Position(code=code, shares=shares, cost=amount + fee,
                                        avg_price=(amount + fee) / shares, peak_price=exec_price)
        self._record(date, code, "buy", exec_price, shares, amount, fee, reason)
        return True

    def sell(self, date: str, code: str, price: float, reason: str, shares: int | None = None) -> bool:
        pos = self.positions.get(code)
        if not pos or pos.shares <= 0:
            return False
        bar = self.bar_at(code, date)
        if not bar or not self._can_sell(bar):
            return False
        idx = self.index_of[code].get(date)
        if idx is not None and idx > 0:
            prev_close = self.panel[code][idx - 1]["close"]
            if prev_close > 0 and (bar["open"] / prev_close - 1) <= -(self._limit_pct(code) - 0.002):
                return False
        qty = pos.shares if shares is None else min(shares, pos.shares)
        exec_price = price * (1 - self.cost.slippage)
        amount = qty * exec_price
        fee = self.cost.sell_cost(amount)
        self.cash += amount - fee
        self._record(date, code, "sell", exec_price, qty, amount, fee, reason)
        pos.shares -= qty
        pos.cost -= pos.avg_price * qty
        if pos.shares <= 0:
            self.positions.pop(code, None)
        return True

    # ------------------------------------------------------------------
    # 目标组合
    # ------------------------------------------------------------------
    def target_weights(self, t: int) -> list[tuple[str, float, float]]:
        """返回 [(code, score, weight)]，weight 之和 <= 1"""
        day_scores = []
        for code, scores in self._score_cache.items():
            s = scores[t] if t < len(scores) else None
            if s is None:
                continue
            day_scores.append((code, s))
        if not day_scores:
            return []
        day_scores.sort(key=lambda x: x[1], reverse=True)
        n = min(self.cfg.top_n, self.risk.max_holdings)
        picks = day_scores[:n]
        w = self.cfg.weighting

        if w == "equal":
            weights = [1 / len(picks)] * len(picks)
        elif w == "score":
            total = sum(max(s, 0) for _, s in picks) or 1
            weights = [max(s, 0) / total for _, s in picks]
        elif w == "vol_inverse":
            invs = []
            for code, _ in picks:
                v = self._vol_60(code, t)
                invs.append(1 / v if v and v > 0 else 1)
            tot = sum(invs) or 1
            weights = [x / tot for x in invs]
        elif w == "risk_parity":
            invs = []
            for code, _ in picks:
                v = self._vol_60(code, t)
                invs.append(1 / (v * v) if v and v > 0 else 1)
            tot = sum(invs) or 1
            weights = [x / tot for x in invs]
        else:
            weights = [1 / len(picks)] * len(picks)

        # 单票上限
        cap = self.risk.max_position_pct
        weights = [min(x, cap) for x in weights]
        # 重新归一，但总和不超过 (1 - cash_buffer)
        tot = sum(weights)
        max_total = 1 - self.risk.cash_buffer
        if tot > max_total and tot > 0:
            weights = [x / tot * max_total for x in weights]

        return [(picks[i][0], picks[i][1], weights[i]) for i in range(len(picks))]

    def _vol_60(self, code: str, t: int) -> float | None:
        date = self.calendar[t]
        i = self.index_of[code].get(date)
        if i is None or i < 61:
            return None
        bars = self.panel[code]
        rets = [bars[k]["close"] / bars[k - 1]["close"] - 1 for k in range(i - 59, i + 1)]
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) * math.sqrt(252)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self, progress=None) -> BTResult:
        self._prepare_scores()
        cal = self.calendar
        res = BTResult(benchmark_name=self.cfg.benchmark)
        if len(cal) < 30:
            raise ValueError("回测区间内交易日过少（<30），请检查股票池数据或区间设置")

        peak_equity = self.cfg.initial_cash
        halted = False
        halt_until = 0
        pending: list[tuple[str, float, float]] | None = None   # 次日执行的目标组合
        pending_reason = ""

        for t, date in enumerate(cal):
            # ---------- 1. 开盘：执行昨日生成的目标组合 ----------
            if pending is not None:
                self._execute_target(date, pending, pending_reason)
                pending = None

            # ---------- 2. 盘中/收盘：止损检查（按收盘价，次日开盘卖出） ----------
            to_sell = []
            for code, pos in list(self.positions.items()):
                bar = self.bar_at(code, date)
                if not bar:
                    continue
                close = bar["close"]
                pos.peak_price = max(pos.peak_price, bar["high"])
                # 个股止损
                if pos.avg_price > 0 and close / pos.avg_price - 1 <= -self.risk.stop_loss:
                    to_sell.append((code, "止损", close))
                # 移动止损
                elif pos.peak_price > 0 and close / pos.peak_price - 1 <= -self.risk.trailing_stop:
                    to_sell.append((code, "移动止损", close))
            # 次日开盘卖出（T+1 合规）
            if to_sell:
                for code, reason, _ in to_sell:
                    nb = self._next_bar(code, date)
                    if nb:
                        self.sell(nb[0], code, nb[1]["open"], reason)

            # ---------- 3. 收盘：计算净值与风控 ----------
            eq = self.equity(date)
            peak_equity = max(peak_equity, eq)
            dd = (peak_equity - eq) / peak_equity if peak_equity else 0

            if dd >= self.risk.max_drawdown_stop and not halted:
                halted = True
                halt_until = t + 20   # 暂停 20 个交易日后自动恢复
                # 熔断：清掉超出比例的仓位
                self._reduce_positions(date, self.risk.drawdown_reduce_ratio, "回撤熔断")
            elif halted and t >= halt_until:
                halted = False

            res.dates.append(date)
            res.equity_curve.append(eq)
            res.navs.append(eq / self.cfg.initial_cash)
            res.daily_positions.append(len(self.positions))
            bn = self._bench_nav(date)
            res.bench_navs.append(bn)

            # ---------- 4. 调仓日：生成次日目标 ----------
            if t % self.cfg.rebalance_days == 0 and t < len(cal) - 2:
                if halted:
                    # 熔断期间：保留现有持仓，不新增也不清仓（等回撤修复）
                    pending = None
                else:
                    pending = self.target_weights(t)
                    pending_reason = f"调仓(第{t // self.cfg.rebalance_days + 1}次)"

            if progress and t % 50 == 0:
                progress(t, len(cal))

        # 收尾：期末持仓
        last = cal[-1]
        res.holdings = []
        for code, pos in self.positions.items():
            b = self.bar_at(code, last)
            px = b["close"] if b else pos.avg_price
            res.holdings.append({
                "code": code, "name": self.names.get(code, code),
                "shares": pos.shares, "avg_price": round(pos.avg_price, 3),
                "price": round(px, 3), "value": round(px * pos.shares, 0),
                "pnl_pct": round((px / pos.avg_price - 1) * 100, 2) if pos.avg_price else 0,
            })
        res.holdings.sort(key=lambda x: x["value"], reverse=True)

        res.drawdowns = performance.drawdown_series(res.navs)
        res.trades = self.trades
        res.metrics = performance.metrics(res.dates, res.navs, res.bench_navs,
                                          res.trades, self.cfg.initial_cash)
        res.metrics["final_cash"] = round(self.cash, 0)
        res.monthly = performance.monthly_returns(res.dates, res.navs)
        return res

    def _next_bar(self, code: str, date: str) -> tuple[str, dict] | None:
        i = self.index_of[code].get(date)
        if i is None or i + 1 >= len(self.panel[code]):
            return None
        nb = self.panel[code][i + 1]
        if nb["time"] > self.cfg.end:
            return None
        return nb["time"], nb

    def _execute_target(self, date: str, target: list[tuple[str, float, float]], reason: str) -> None:
        target_codes = {c for c, _, _ in target}
        # 先卖不在目标中的
        for code in list(self.positions.keys()):
            if code not in target_codes:
                bar = self.bar_at(code, date)
                if bar:
                    self.sell(date, code, bar["open"], reason + "-调出")
        # 再买入/加仓
        eq = self.equity(date, use_open=True)
        for code, score, weight in target:
            bar = self.bar_at(code, date)
            if not bar:
                continue
            target_value = eq * weight
            pos = self.positions.get(code)
            cur_value = bar["open"] * pos.shares if pos else 0
            delta = target_value - cur_value
            if delta > eq * 0.01:      # 差异 >1% 才调仓，避免频繁交易
                self.buy(date, code, bar["open"], delta, reason)
            elif delta < -eq * 0.01 and pos:
                qty = int(abs(delta) / bar["open"] / 100) * 100
                if qty >= 100:
                    self.sell(date, code, bar["open"], reason + "-减仓", shares=qty)

    def _reduce_positions(self, date: str, keep_ratio: float, reason: str) -> None:
        """按比例减仓（回撤熔断）。"""
        for code, pos in list(self.positions.items()):
            bar = self.bar_at(code, date)
            if not bar:
                continue
            qty = int(pos.shares * (1 - keep_ratio) / 100) * 100
            if qty >= 100:
                self.sell(date, code, bar["close"], reason, shares=qty)

    def _bench_nav(self, date: str) -> float:
        bench = self.panel.get(self.cfg.benchmark)
        if not bench:
            return 1.0
        idx = self.index_of[self.cfg.benchmark]
        i = idx.get(date)
        # 找最近一个交易日
        if i is None:
            if not hasattr(self, "_bench_base"):
                self._bench_base = None
            return self._last_bench if hasattr(self, "_last_bench") else 1.0
        if not hasattr(self, "_bench_base") or self._bench_base is None:
            self._bench_base = bench[i]["close"]
        self._last_bench = bench[i]["close"] / self._bench_base
        return self._last_bench


def run_backtest(strategy_key: str, params: dict, universe_codes: list[str],
                 names: dict[str, str], cfg: BacktestConfig,
                 progress=None) -> BTResult:
    from . import universe as uni
    from .strategies import build_strategy

    panel = uni.load_panel(universe_codes)
    # 基准数据必须存在
    if cfg.benchmark not in panel:
        b = uni.get_history(cfg.benchmark, 1000)
        if b:
            panel[cfg.benchmark] = b
    strat = build_strategy(strategy_key, params)
    engine = BacktestEngine(strat, panel, names, cfg)
    return engine.run(progress)
