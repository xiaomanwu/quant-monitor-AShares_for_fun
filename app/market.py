"""
行情数据源：多源降级抓取（腾讯 / 新浪），带内存缓存与磁盘 K 线缓存。
所有函数在网络异常时返回空结构而不是抛异常，保证监控线程不中断。
"""
from __future__ import annotations

import json
import re
import time
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

_lock = threading.Lock()


def _get(url: str, referer: str | None = None, timeout: float = 6.0, encoding: str | None = None) -> str:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    if encoding:
        r.encoding = encoding
    return r.text


# ----------------------------------------------------------------------------
# 代码规范化：用户可输入 600519 / sh600519 / 000001 / 300750
# ----------------------------------------------------------------------------
def normalize(code: str) -> str:
    code = code.strip().lower()
    if re.match(r"^(sh|sz|bj)\d{6}$", code):
        return code
    code = re.sub(r"\D", "", code)
    if len(code) != 6:
        return code
    if code.startswith(("60", "68", "51", "58", "56", "11", "90")):
        return "sh" + code
    if code.startswith(("00", "30", "12", "15", "16", "18", "39")):
        return "sz" + code
    if code.startswith(("8", "4", "92")):
        return "bj" + code
    return "sh" + code


def bare(code: str) -> str:
    return re.sub(r"^\D+", "", normalize(code))


# ----------------------------------------------------------------------------
# 实时快照
# ----------------------------------------------------------------------------
@dataclass
class Quote:
    code: str            # 带前缀，如 sh600519
    name: str
    price: float         # 最新价
    prev_close: float
    open: float
    high: float
    low: float
    change: float        # 涨跌额
    pct: float           # 涨跌幅 %
    volume: float        # 成交量（手）
    amount: float        # 成交额（元）
    turnover: float      # 换手率 %
    volume_ratio: float  # 量比
    amplitude: float     # 振幅 %
    pe: float
    pb: float
    market_cap: float    # 总市值（亿元）
    float_cap: float     # 流通市值（亿元）
    high_limit: float    # 涨停价
    low_limit: float     # 跌停价
    time: str
    bids: list = field(default_factory=list)   # [(价格, 手数) x5]
    asks: list = field(default_factory=list)
    # 由监控层补充
    turnover_rate_amount: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _f(v, default=0.0) -> float:
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return default


def parse_tencent(text: str) -> dict[str, Quote]:
    """解析 qt.gtimg.cn 返回（GBK 解码后），字段以 ~ 分隔。"""
    out: dict[str, Quote] = {}
    for line in text.splitlines():
        m = re.match(r'v_(\w+)="([^"]*)"', line.strip())
        if not m:
            continue
        symbol, body = m.group(1), m.group(2)
        f = body.split("~")
        if len(f) < 50 or not f[1]:
            continue
        bids, asks = [], []
        for i in range(5):
            bids.append([_f(f[9 + i * 2]), _f(f[10 + i * 2])])
            asks.append([_f(f[19 + i * 2]), _f(f[20 + i * 2])])
        q = Quote(
            code=symbol,
            name=f[1],
            price=_f(f[3]),
            prev_close=_f(f[4]),
            open=_f(f[5]),
            high=_f(f[33]),
            low=_f(f[34]),
            change=_f(f[31]),
            pct=_f(f[32]),
            volume=_f(f[36]),
            amount=_f(f[37]) * 10000,      # 万元 -> 元
            turnover=_f(f[38]),
            volume_ratio=_f(f[49]),
            amplitude=_f(f[43]),
            pe=_f(f[39]),
            pb=_f(f[46]),
            market_cap=_f(f[45]),   # 总市值（亿元）
            float_cap=_f(f[44]),    # 流通市值（亿元）
            high_limit=_f(f[47]),
            low_limit=_f(f[48]),
            time=f[30],
            bids=bids,
            asks=asks,
        )
        out[symbol] = q
    return out


def fetch_quotes(codes: Iterable[str], batch: int = 90) -> dict[str, Quote]:
    """批量抓取实时快照，自动分批（URL 长度限制）。"""
    codes = [normalize(c) for c in codes if c]
    result: dict[str, Quote] = {}
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        try:
            text = _get("https://qt.gtimg.cn/q=" + ",".join(chunk), encoding="gbk")
            result.update(parse_tencent(text))
        except Exception:
            continue
    return result


# ----------------------------------------------------------------------------
# K 线
# ----------------------------------------------------------------------------
_kl_cache: dict[tuple[str, str], tuple[float, list]] = {}
KLINE_TTL = 300  # 5 分钟


def fetch_kline(code: str, scale: int = 240, length: int = 260, use_cache: bool = True) -> list[dict]:
    """
    scale: 240=日线 60=60分钟 30=30分钟 5=5分钟
    返回 [{time, open, high, low, close, volume, amount}, ...]
    """
    sym = normalize(code)
    key = (sym, f"{scale}_{length}")
    now = time.time()
    if use_cache:
        with _lock:
            hit = _kl_cache.get(key)
            if hit and now - hit[0] < KLINE_TTL:
                return hit[1]

    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
        f"?symbol={sym}&scale={scale}&ma=no&datalen={length}"
    )
    bars: list[dict] = []
    try:
        raw = _get(url, referer="https://finance.sina.com.cn", timeout=8)
        data = json.loads(raw) if raw.strip().startswith("[") else []
        for b in data or []:
            bars.append({
                "time": b.get("day"),
                "open": _f(b.get("open")),
                "high": _f(b.get("high")),
                "low": _f(b.get("low")),
                "close": _f(b.get("close")),
                "volume": _f(b.get("volume")),
                "amount": _f(b.get("amount")),
            })
    except Exception:
        bars = []

    if bars:
        with _lock:
            _kl_cache[key] = (now, bars)
    return bars


# ----------------------------------------------------------------------------
# 全市场排行（新浪榜单，用于异动榜 / 涨跌分布）
# ----------------------------------------------------------------------------
def fetch_rank(node: str = "hs_a", sort: str = "changepercent", asc: int = 0, num: int = 40) -> list[dict]:
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"Market_Center.getHQNodeData?page=1&num={num}&sort={sort}&asc={asc}"
        f"&node={node}&symbol=&_s_r_a=page"
    )
    try:
        raw = _get(url, referer="https://finance.sina.com.cn", timeout=8)
        data = json.loads(raw) if raw.strip().startswith("[") else []
    except Exception:
        return []
    rows = []
    for d in data or []:
        rows.append({
            "code": d.get("symbol", ""),
            "name": d.get("name", ""),
            "price": _f(d.get("trade")),
            "pct": _f(d.get("changepercent")),
            "amount": _f(d.get("amount")),
            "turnover": _f(d.get("turnoverratio")),
            "mktcap": _f(d.get("mktcap")) / 10000,  # 万元 -> 亿元
        })
    return rows


def _breadth_page(page: int, per_page: int) -> list[dict]:
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"Market_Center.getHQNodeData?page={page}&num={per_page}&sort=symbol&asc=1&node=hs_a"
    )
    try:
        raw = _get(url, referer="https://finance.sina.com.cn", timeout=10)
        return json.loads(raw) if raw.strip().startswith("[") else []
    except Exception:
        return []


def fetch_market_breadth() -> dict:
    """涨跌家数统计：并发抓取全部 A 股涨跌幅（约 56 页 × 100 只）。"""
    from concurrent.futures import ThreadPoolExecutor

    total = 0
    up = down = flat = 0
    limit_up = limit_down = 0
    buckets = {"<-7": 0, "-7~-5": 0, "-5~-3": 0, "-3~0": 0, "0~3": 0, "3~5": 0, "5~7": 0, ">7": 0}
    per_page = 100
    with ThreadPoolExecutor(max_workers=10) as ex:
        pages = list(ex.map(lambda p: _breadth_page(p, per_page), range(1, 60)))
    for data in pages:
        if not data:
            continue
        for d in data:
            pct = _f(d.get("changepercent"))
            price = _f(d.get("trade"))
            if price <= 0:
                continue
            total += 1
            if pct > 0:
                up += 1
            elif pct < 0:
                down += 1
            else:
                flat += 1
            if pct >= 9.8:
                limit_up += 1
            elif pct <= -9.8:
                limit_down += 1
            if pct < -7:
                buckets["<-7"] += 1
            elif pct < -5:
                buckets["-7~-5"] += 1
            elif pct < -3:
                buckets["-5~-3"] += 1
            elif pct < 0:
                buckets["-3~0"] += 1
            elif pct <= 3:
                buckets["0~3"] += 1
            elif pct <= 5:
                buckets["3~5"] += 1
            elif pct <= 7:
                buckets["5~7"] += 1
            else:
                buckets[">7"] += 1
        if len(data) < per_page:
            break
    return {
        "total": total, "up": up, "down": down, "flat": flat,
        "limit_up": limit_up, "limit_down": limit_down, "buckets": buckets,
    }


# ----------------------------------------------------------------------------
# 板块行情（新浪行业板块）
# ----------------------------------------------------------------------------
def fetch_sectors() -> list[dict]:
    try:
        text = _get("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
                    referer="https://finance.sina.com.cn", encoding="gbk", timeout=8)
    except Exception:
        return []
    m = re.search(r"=\s*(\{.*\})", text, re.S)
    if not m:
        return []
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return []
    rows = []
    for _, v in obj.items():
        p = v.split(",")
        if len(p) < 13:
            continue
        rows.append({
            "id": p[0], "name": p[1], "count": int(_f(p[2])),
            "avg_price": _f(p[3]), "pct": _f(p[5]),
            "volume": _f(p[6]), "amount": _f(p[7]),
            "lead_code": p[9], "lead_pct": _f(p[11]), "lead_name": p[12],
        })
    rows.sort(key=lambda x: x["pct"], reverse=True)
    return rows


# ----------------------------------------------------------------------------
# 大盘指数
# ----------------------------------------------------------------------------
INDEX_CODES = ["sh000001", "sz399001", "sz399006", "sh000688", "sh000300"]


def fetch_indexes() -> list[dict]:
    qs = fetch_quotes(INDEX_CODES)
    out = []
    for c in INDEX_CODES:
        q = qs.get(c)
        if not q:
            continue
        out.append({
            "code": c, "name": q.name, "price": q.price,
            "change": q.change, "pct": q.pct,
            "amount": q.amount, "time": q.time,
        })
    return out


# ----------------------------------------------------------------------------
# 分时（当日逐分钟累计）—— 用 1 分钟 K 线近似
# ----------------------------------------------------------------------------
def fetch_intraday(code: str) -> list[dict]:
    bars = fetch_kline(code, scale=5, length=60)
    today = time.strftime("%Y-%m-%d")
    return [b for b in bars if str(b["time"]).startswith(today)]


def search_symbols(keyword: str, limit: int = 20) -> list[dict]:
    """用新浪建议接口做代码/名称联想。"""
    kw = keyword.strip()
    if not kw:
        return []
    url = f"https://suggest3.sinajs.cn/suggest/type=11,12,15,21,22,23,24,25,26,31&key={kw}"
    try:
        text = _get(url, referer="https://finance.sina.com.cn", encoding="gbk", timeout=6)
    except Exception:
        return []
    m = re.search(r'="(.*)"', text, re.S)
    if not m or not m.group(1):
        return []
    out = []
    for item in m.group(1).split(";"):
        p = item.split(",")
        if len(p) < 5:
            continue
        symbol = p[3].lower() if p[3] else ""
        if not re.match(r"^(sh|sz|bj)\d{6}$", symbol):
            continue
        out.append({"code": symbol, "name": p[4], "type": p[1]})
        if len(out) >= limit:
            break
    return out
