"""
模拟盘（纸上交易）：真实资金管理逻辑，但不接券商接口。

设计：
- 每日收盘后（或手动触发）运行一次信号 → 生成目标组合 → 次日开盘撮合
- 账户状态、成交记录、每日净值全部落盘，可跨重启恢复
- 与回测共用同一套成本模型与风控规则，保证「回测 → 模拟」一致性
- 每日跑一次，就能得到一条与回测可比的实盘化净值曲线
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import market, universe
from .config import BacktestConfig, CostModel, RiskConfig
from .strategies import build_strategy

PAPER_DIR = Path(__file__).resolve().parent.parent / "data" / "paper"
PAPER_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = PAPER_DIR / "state.json"

_lock = threading.RLock()


DEFAULT_STATE = {
    "created": None,
    "strategy": "momentum",
    "params": {},
    "universe": "csi300",
    "initial_cash": 1_000_000.0,
    "cash": 1_000_000.0,
    "positions": {},        # {code: {shares, cost, avg_price, peak_price, name}}
    "orders": [],           # 待执行订单
    "trades": [],           # 已成交
    "daily": [],            # 每日净值 [{date, equity, nav, cash, positions}]
    "last_run": None,
    "signals": [],          # 最近一次信号
    "status": "未启动",
}


def load_state() -> dict:
    with _lock:
        if STATE_FILE.exists():
            try:
                s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                for k, v in DEFAULT_STATE.items():
                    s.setdefault(k, v)
                return s
            except Exception:
                pass
        return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    with _lock:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


class PaperAccount:
    """模拟盘账户：负责下单、撮合、记账、净值记录。"""

    def __init__(self, state: dict | None = None, cost: CostModel | None = None):
        self.state = state or load_state()
        self.cost = cost or CostModel()

    # ---------------- 初始化 / 重置 ----------------
    @classmethod
    def create(cls, strategy: str, params: dict, universe_name: str,
               initial_cash: float = 1_000_000.0) -> "PaperAccount":
        state = dict(DEFAULT_STATE)
        state.update({
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "strategy": strategy, "params": params, "universe": universe_name,
            "initial_cash": initial_cash, "cash": initial_cash,
            "positions": {}, "orders": [], "trades": [], "daily": [],
            "status": "运行中",
        })
        save_state(state)
        return cls(state)

    # ---------------- 组合计算 ----------------
    def get_universe_codes(self) -> list[str]:
        if self.state["universe"] == "watchlist":
            from .engine import engine
            return list(engine.watchlist)
        return [x["code"] for x in universe.build_universe(self.state["universe"])]

    def positions_value(self) -> tuple[float, list[dict]]:
        """按最新价估值，返回 (总市值, 明细)"""
        codes = list(self.state["positions"].keys())
        qs = market.fetch_quotes(codes) if codes else {}
        total = 0.0
        rows = []
        for code, p in self.state["positions"].items():
            q = qs.get(code)
            price = q.price if q else p["avg_price"]
            value = price * p["shares"]
            total += value
            rows.append({
                "code": code, "name": p.get("name", code),
                "shares": p["shares"], "avg_price": round(p["avg_price"], 3),
                "price": round(price, 3), "value": round(value, 0),
                "pnl": round(value - p["avg_price"] * p["shares"], 0),
                "pnl_pct": round((price / p["avg_price"] - 1) * 100, 2) if p["avg_price"] else 0,
                "weight": 0.0,
            })
        rows.sort(key=lambda x: x["value"], reverse=True)
        return total, rows

    def equity(self) -> float:
        mv, _ = self.positions_value()
        return self.state["cash"] + mv

    def summary(self) -> dict:
        mv, rows = self.positions_value()
        eq = self.state["cash"] + mv
        init = self.state["initial_cash"]
        total_ret = (eq / init - 1) * 100 if init else 0
        for r in rows:
            r["weight"] = round(r["value"] / eq * 100, 2) if eq else 0
        daily = self.state["daily"]
        # 最大回撤
        mdd = 0.0
        peak = init
        for d in daily:
            peak = max(peak, d["equity"])
            mdd = max(mdd, (peak - d["equity"]) / peak if peak else 0)
        return {
            "created": self.state["created"],
            "strategy": self.state["strategy"],
            "params": self.state["params"],
            "universe": self.state["universe"],
            "status": self.state["status"],
            "last_run": self.state["last_run"],
            "initial_cash": round(init, 0),
            "cash": round(self.state["cash"], 0),
            "market_value": round(mv, 0),
            "equity": round(eq, 0),
            "total_return": round(total_ret, 2),
            "total_pnl": round(eq - init, 0),
            "max_drawdown": round(mdd * 100, 2),
            "positions": rows,
            "position_count": len(rows),
            "trade_count": len(self.state["trades"]),
            "pending_orders": len(self.state["orders"]),
            "daily": daily[-250:],
            "trades": self.state["trades"][-80:][::-1],
            "signals": self.state.get("signals", []),
        }

    # ---------------- 生成信号 ----------------
    def generate_target(self, top_n: int = 8) -> list[tuple[str, float]]:
        """用策略给股票池打分，返回 [(code, score)]（按分数降序，取前 top_n）"""
        codes = self.get_universe_codes()
        # 确保本地有数据
        panel = universe.load_panel(codes)
        missing = [c for c in codes if market.normalize(c) not in panel]
        if missing:
            universe.download_history(missing[:400], workers=10)
            panel = universe.load_panel(codes)

        strat = build_strategy(self.state["strategy"], self.state["params"])
        if hasattr(strat, "bind"):
            for bars in panel.values():
                strat.bind(bars)

        scored = []
        for code, bars in panel.items():
            try:
                s = strat.score(bars, len(bars) - 1)
            except Exception:
                s = None
            if s is not None:
                scored.append((code, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    # ---------------- 下单 ----------------
    def place_orders(self, target: list[tuple[str, float]], sizing: str = "equal",
                     max_pos_pct: float = 0.20) -> list[dict]:
        """根据目标组合生成次日订单（不立即成交）。"""
        eq = self.equity()
        target_codes = {c for c, _ in target}
        orders = []

        # 卖出：不在目标中
        for code, p in self.state["positions"].items():
            if code not in target_codes:
                orders.append({"code": code, "side": "sell", "shares": p["shares"],
                               "reason": "调出", "score": 0})

        # 买入/调整
        if target:
            weights = [1 / len(target)] * len(target)
            if sizing == "score":
                tot = sum(max(s, 0) for _, s in target) or 1
                weights = [max(s, 0) / tot for _, s in target]
            weights = [min(w, max_pos_pct) for w in weights]
            tot_w = sum(weights)
            if tot_w > 0.95:
                weights = [w / tot_w * 0.95 for w in weights]
            for (code, score), w in zip(target, weights):
                target_value = eq * w
                p = self.state["positions"].get(code)
                qs = market.fetch_quotes([code])
                price = qs[code].price if code in qs else 0
                if price <= 0:
                    continue
                cur_value = price * p["shares"] if p else 0
                delta = target_value - cur_value
                shares = int(abs(delta) / price / 100) * 100
                if shares < 100:
                    continue
                orders.append({
                    "code": code, "side": "buy" if delta > 0 else "sell",
                    "shares": shares, "reason": "调仓", "score": round(score, 2),
                })
        return orders

    # ---------------- 撮合 ----------------
    def execute_orders(self, orders: list[dict]) -> list[dict]:
        """按最新价撮合（模拟盘简化：以现价成交，含滑点与费用）。"""
        if not orders:
            return []
        codes = list({o["code"] for o in orders})
        qs = market.fetch_quotes(codes)
        filled = []
        for o in orders:
            q = qs.get(o["code"])
            if not q or q.price <= 0:
                continue
            price = q.price
            if o["side"] == "buy":
                exec_price = price * (1 + self.cost.slippage)
                budget = min(o["shares"] * exec_price, self.state["cash"] * 0.995)
                shares = int(budget / exec_price / 100) * 100
                if shares < 100:
                    continue
                amount = shares * exec_price
                fee = self.cost.buy_cost(amount)
                if amount + fee > self.state["cash"]:
                    continue
                self.state["cash"] -= amount + fee
                p = self.state["positions"].get(o["code"])
                if p:
                    total_cost = p["avg_price"] * p["shares"] + amount + fee
                    p["shares"] += shares
                    p["avg_price"] = total_cost / p["shares"]
                else:
                    self.state["positions"][o["code"]] = {
                        "shares": shares, "avg_price": (amount + fee) / shares,
                        "peak_price": exec_price, "name": q.name,
                    }
                self._record(o, exec_price, shares, amount, fee, q.name)
                filled.append(o)
            else:
                p = self.state["positions"].get(o["code"])
                if not p:
                    continue
                shares = min(o["shares"], p["shares"])
                exec_price = price * (1 - self.cost.slippage)
                amount = shares * exec_price
                fee = self.cost.sell_cost(amount)
                self.state["cash"] += amount - fee
                p["shares"] -= shares
                if p["shares"] <= 0:
                    self.state["positions"].pop(o["code"], None)
                self._record(o, exec_price, shares, amount, fee, q.name)
                filled.append(o)
        save_state(self.state)
        return filled

    def _record(self, o, price, shares, amount, fee, name):
        self.state["trades"].append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "code": o["code"], "name": name, "side": o["side"],
            "price": round(price, 3), "shares": shares,
            "amount": round(amount, 2), "fee": round(fee, 2),
            "reason": o.get("reason", ""),
        })

    # ---------------- 每日推进 ----------------
    def run_daily(self, force: bool = False) -> dict:
        """执行一次日度流程：止损检查 → 生成目标 → 下单撮合 → 记净值。"""
        today = datetime.now().strftime("%Y-%m-%d")
        st = self.state
        if not force and st.get("last_run") == today:
            return {"ok": False, "msg": "今日已执行，如需重跑请使用强制模式"}

        log = []

        # 1. 止损检查（个股）
        risk = RiskConfig()
        for code, p in list(st["positions"].items()):
            qs = market.fetch_quotes([code])
            q = qs.get(code)
            if not q:
                continue
            if q.price > p.get("peak_price", 0):
                p["peak_price"] = q.price
            if p["avg_price"] and q.price / p["avg_price"] - 1 <= -risk.stop_loss:
                log.append(f"{p.get('name', code)} 触发止损 {risk.stop_loss*100:.0f}%")
                self.execute_orders([{"code": code, "side": "sell", "shares": p["shares"],
                                      "reason": "止损"}])
            elif p.get("peak_price") and q.price / p["peak_price"] - 1 <= -risk.trailing_stop:
                log.append(f"{p.get('name', code)} 触发移动止损")
                self.execute_orders([{"code": code, "side": "sell", "shares": p["shares"],
                                      "reason": "移动止损"}])

        # 2. 生成目标并下单
        target = self.generate_target()
        st["signals"] = [{"code": c, "score": round(s, 2),
                          "name": self.state["positions"].get(c, {}).get("name", "")}
                         for c, s in target]
        orders = self.place_orders(target)
        filled = self.execute_orders(orders)

        # 3. 记录净值
        eq = self.equity()
        init = st["initial_cash"]
        st["daily"] = [d for d in st["daily"] if d["date"] != today]
        st["daily"].append({
            "date": today, "equity": round(eq, 0),
            "nav": round(eq / init, 6),
            "cash": round(st["cash"], 0),
            "positions": len(st["positions"]),
        })
        st["daily"].sort(key=lambda x: x["date"])
        st["last_run"] = today
        st["status"] = "运行中"
        save_state(st)

        return {
            "ok": True, "date": today, "target": len(target),
            "orders": len(orders), "filled": len(filled),
            "equity": round(eq, 0), "log": log,
        }


def get_account() -> PaperAccount:
    return PaperAccount()
