"""
监控引擎：持有自选股、定时抓取行情、运行信号规则、推送 WebSocket。
单例 MonitorEngine，供 FastAPI 路由访问。
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

from . import market, indicators

WATCH_FILE = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"
DEFAULT_WATCHLIST = [
    "sh600519", "sz000001", "sz300750", "sh601318", "sz000858",
    "sh600036", "sz002594", "sh688981", "sh601899", "sz002230",
]


def is_trading_time(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def session_label(now: datetime | None = None) -> str:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "休市"
    hm = now.hour * 100 + now.minute
    if hm < 915:
        return "盘前"
    if 915 <= hm < 930:
        return "集合竞价"
    if 930 <= hm <= 1130:
        return "午前交易"
    if 1130 < hm < 1300:
        return "午间休市"
    if 1300 <= hm <= 1500:
        return "午后交易"
    return "已收盘"


class MonitorEngine:
    def __init__(self) -> None:
        self.watchlist: list[str] = []
        self.quotes: dict[str, dict] = {}
        self.signal_feed: deque[dict] = deque(maxlen=300)
        self._seen: set[str] = set()
        self.indexes: list[dict] = []
        self.breadth: dict = {}
        self.sectors: list[dict] = []
        self.gainers: list[dict] = []
        self.losers: list[dict] = []
        self.last_update: str | None = None
        self.tick_count = 0
        self.status = "初始化"
        self._lock = threading.RLock()
        self._subscribers: list = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._slow_counter = 0
        self.interval = 5
        self.load_watchlist()

    # ---------------- 自选股持久化 ----------------
    def load_watchlist(self) -> None:
        if WATCH_FILE.exists():
            try:
                data = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
                self.watchlist = [market.normalize(c) for c in data if c]
            except Exception:
                self.watchlist = list(DEFAULT_WATCHLIST)
        else:
            self.watchlist = list(DEFAULT_WATCHLIST)
        self.save_watchlist()

    def save_watchlist(self) -> None:
        try:
            WATCH_FILE.write_text(
                json.dumps(self.watchlist, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def add(self, code: str) -> dict:
        c = market.normalize(code)
        if not c.isdigit() and len(market.bare(c)) != 6:
            return {"ok": False, "msg": f"无法识别的代码：{code}"}
        with self._lock:
            if c in self.watchlist:
                return {"ok": True, "msg": "已在自选股中"}
            self.watchlist.append(c)
            self.save_watchlist()
        # 立即取一次数据
        self._refresh_one(c)
        return {"ok": True, "msg": f"已添加 {c}"}

    def remove(self, code: str) -> dict:
        c = market.normalize(code)
        with self._lock:
            if c in self.watchlist:
                self.watchlist.remove(c)
                self.quotes.pop(c, None)
                self.save_watchlist()
        return {"ok": True}

    # ---------------- 单只刷新 ----------------
    def _refresh_one(self, code: str) -> None:
        qs = market.fetch_quotes([code])
        q = qs.get(code)
        if not q:
            return
        bars = market.fetch_kline(code, 240, 260)
        d = q.to_dict()
        sigs = indicators.evaluate_signals(bars, d)
        d["signals"] = sigs
        d["score"] = indicators.score_signals(sigs)
        d["spark"] = [b["close"] for b in bars[-30:]]
        with self._lock:
            self.quotes[code] = d

    # ---------------- 主循环 ----------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self.status = "数据异常，重试中"
                traceback.print_exc()
            time.sleep(self.interval)

    def tick(self) -> None:
        with self._lock:
            codes = list(self.watchlist)
        if not codes:
            self.status = "自选股为空"
            return

        qs = market.fetch_quotes(codes)
        if not qs:
            self.status = "行情获取失败"
            return

        # 批量取K线（带缓存，5分钟内不重复请求）
        new_quotes = {}
        for code in codes:
            q = qs.get(code)
            if not q:
                continue
            bars = market.fetch_kline(code, 240, 260)
            d = q.to_dict()
            sigs = indicators.evaluate_signals(bars, d)
            # 标记信号：同一只股票的同一信号，价格变化不大则去重
            d["signals"] = sigs
            d["score"] = indicators.score_signals(sigs)
            d["spark"] = [b["close"] for b in bars[-30:]]
            if bars:
                prev = bars[-2]["close"] if len(bars) > 1 else q.prev_close
                d["day_pct_vs_ma20"] = (
                    (q.price - sum(b["close"] for b in bars[-20:]) / min(20, len(bars)))
                    / (sum(b["close"] for b in bars[-20:]) / min(20, len(bars))) * 100
                    if bars else 0)
                d["prev_bar_close"] = prev
            new_quotes[code] = d
            self._collect_signals(d, sigs)

        with self._lock:
            self.quotes = new_quotes
        self.tick_count += 1
        self.last_update = datetime.now().strftime("%H:%M:%S")
        self.status = "运行中" if is_trading_time() else f"运行中（{session_label()}）"

        # 低频刷新：大盘 / 涨跌分布 / 板块 / 榜单（每 6 个 tick ≈ 30 秒）
        self._slow_counter += 1
        if self._slow_counter % 6 == 1:
            threading.Thread(target=self._slow_refresh, daemon=True).start()

        self.broadcast({"type": "tick", "payload": self.snapshot(core=False)})

    def _slow_refresh(self) -> None:
        try:
            idx = market.fetch_indexes()
            if idx:
                self.indexes = idx
            sectors = market.fetch_sectors()
            if sectors:
                self.sectors = sectors[:12]
            gain = market.fetch_rank("hs_a", "changepercent", 0, 20)
            lose = market.fetch_rank("hs_a", "changepercent", 1, 20)
            if gain:
                self.gainers = gain
            if lose:
                self.losers = lose
            breadth = market.fetch_market_breadth()
            if breadth.get("total"):
                self.breadth = breadth
        except Exception:
            pass
        self.broadcast({"type": "overview", "payload": self.overview()})

    def _collect_signals(self, quote: dict, sigs: list[dict]) -> None:
        now = datetime.now()
        for s in sigs:
            if s["direction"] == "info" and s["key"] not in ("limit_up", "limit_down", "plunge", "surge"):
                continue
            day = now.strftime("%Y-%m-%d")
            dedup = f"{day}|{quote['code']}|{s['key']}"
            if dedup in self._seen:
                continue
            self._seen.add(dedup)
            item = {
                "id": dedup,
                "time": now.strftime("%H:%M:%S"),
                "date": day,
                "code": quote["code"],
                "stock_name": quote["name"],
                "title": s["title"],
                "direction": s["direction"],
                "strength": s["strength"],
                "desc": s["desc"],
                "price": quote["price"],
                "pct": quote["pct"],
            }
            self.signal_feed.appendleft(item)

    # ---------------- 快照 ----------------
    def snapshot(self, core: bool = True) -> dict:
        with self._lock:
            quotes = list(self.quotes.values())
            feed = list(self.signal_feed)[:60]
            wl = list(self.watchlist)
        return {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_update": self.last_update,
            "status": self.status,
            "session": session_label(),
            "trading": is_trading_time(),
            "tick": self.tick_count,
            "watchlist": wl,
            "quotes": quotes,
            "signals": feed,
            "overview": self.overview() if core else None,
        }

    def overview(self) -> dict:
        return {
            "indexes": self.indexes,
            "breadth": self.breadth,
            "sectors": self.sectors,
            "gainers": self.gainers,
            "losers": self.losers,
        }

    # ---------------- WebSocket ----------------
    def register(self, send) -> None:
        with self._lock:
            self._subscribers.append(send)

    def unregister(self, send) -> None:
        with self._lock:
            if send in self._subscribers:
                self._subscribers.remove(send)

    def broadcast(self, msg: dict) -> None:
        with self._lock:
            subs = list(self._subscribers)
        dead = []
        for s in subs:
            try:
                s(msg)
            except Exception:
                dead.append(s)
        for d in dead:
            self.unregister(d)


engine = MonitorEngine()
