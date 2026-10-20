"""动态补齐全市场 A 股近一年日线数据。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

from app import market
from app.market import _breadth_page

DATA_DIR = Path(__file__).resolve().parent
DAILY_A_DIR = DATA_DIR / "daily_a"
MANIFEST_PATH = DAILY_A_DIR / "manifest.json"
PAGE_COUNT = 60
PAGE_SIZE = 100
INITIAL_BARS = 260
RECENT_BARS = 30
LOOKBACK_DAYS = 365
RECENT_REFRESH_DAYS = 5


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        json.dump(value, f, ensure_ascii=False)
        temp_path = Path(f.name)
    temp_path.replace(path)


def fetch_all_a_symbols() -> list[dict]:
    """通过新浪 A 股行情分页接口获取当日可用的证券列表。"""
    with ThreadPoolExecutor(max_workers=10) as executor:
        pages = list(executor.map(lambda page: _breadth_page(page, PAGE_SIZE),
                                  range(1, PAGE_COUNT + 1)))

    if not pages or not pages[0]:
        raise RuntimeError("无法获取 A 股列表，请检查行情源连接后重试")

    symbols: dict[str, dict] = {}
    for rows in pages:
        for row in rows:
            code = str(row.get("symbol", "")).lower()
            if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", code):
                continue
            symbols[code] = {"code": code, "name": str(row.get("name", ""))}
    if not symbols:
        raise RuntimeError("行情源未返回有效的 A 股代码")
    return sorted(symbols.values(), key=lambda item: item["code"])


def _read_bars(code: str) -> list[dict]:
    path = DAILY_A_DIR / f"{code}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [bar for bar in value if isinstance(bar, dict) and bar.get("time")]


def _merge_recent_bars(previous: list[dict], incoming: list[dict], today: date) -> list[dict]:
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    merged = {
        str(bar["time"]): bar
        for bar in (*previous, *incoming)
        if bar.get("time") and cutoff <= str(bar["time"])[:10] <= today.isoformat()
    }
    return [merged[key] for key in sorted(merged)]


def _download_one(item: dict, today: date) -> tuple[str, bool, str, str | None]:
    code = item["code"]
    previous = _read_bars(code)
    last_date = previous[-1].get("time", "")[:10] if previous else ""
    try:
        last_day = date.fromisoformat(last_date)
    except ValueError:
        last_day = None

    length = (RECENT_BARS if last_day and
              timedelta(0) <= today - last_day <= timedelta(days=RECENT_REFRESH_DAYS)
              else INITIAL_BARS)
    bars = []
    for attempt in range(3):
        bars = market.fetch_kline(code, scale=240, length=length, use_cache=False)
        if bars:
            break
        if attempt < 2:
            time.sleep(0.3)
    if not bars:
        return code, False, item["name"], "行情源未返回日线"

    merged = _merge_recent_bars(previous, bars, today)
    if not merged:
        return code, False, item["name"], "日线数据不在近一年日期范围内"
    _write_json(DAILY_A_DIR / f"{code}.json", merged)
    return code, True, item["name"], None


def refresh_daily_a(workers: int = 8) -> dict:
    """补齐全市场 A 股日线，保留最近 365 个自然日；当日成功后不重复请求。"""
    today = date.today()
    existing = (json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
                if MANIFEST_PATH.exists() else {})
    if not isinstance(existing, dict):
        raise RuntimeError(f"数据清单格式错误：{MANIFEST_PATH}")
    symbols = fetch_all_a_symbols()
    same_day = existing.get("end_date") == today.isoformat()
    existing_codes = existing.get("codes", {}) if same_day else {}
    if not isinstance(existing_codes, dict):
        existing_codes = {}
    if same_day:
        failed_codes = set(existing.get("failed_codes", []))
        pending = [
            item for item in symbols
            if item["code"] in failed_codes
            or item["code"] not in existing_codes
            or not (DAILY_A_DIR / f'{item["code"]}.json').exists()
        ]
        if not pending and not failed_codes:
            return existing
        symbols_to_update = pending
    else:
        symbols_to_update = symbols

    ok = len(symbols) - len(symbols_to_update)
    failures: list[str] = []
    failure_reasons: dict[str, str] = {}
    names: dict[str, str] = dict(existing_codes)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as executor:
        futures = {
            executor.submit(_download_one, item, today): item for item in symbols_to_update
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                code, succeeded, name, reason = future.result()
            except Exception as exc:
                code, succeeded, name, reason = (
                    item["code"], False, item["name"], f"{type(exc).__name__}: {exc}"
                )
            names[code] = name or code
            if succeeded:
                ok += 1
            else:
                failures.append(code)
                failure_reasons[code] = reason or "未知错误"

    updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    start_date = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    result = {
        "updated_at": updated_at,
        "start_date": start_date,
        "end_date": today.isoformat(),
        "total": len(symbols),
        "ok": ok,
        "failed": len(failures),
        "failed_codes": sorted(failures),
        "failure_reasons": failure_reasons,
        "codes": names,
    }
    _write_json(MANIFEST_PATH, result)
    return result


def get_daily_bars(code: str) -> list[dict]:
    """读取独立动态日线目录中的单只股票数据。"""
    return _read_bars(market.normalize(code))


def main() -> int:
    parser = argparse.ArgumentParser(description="获取/更新近一年全市场 A 股日线")
    parser.add_argument("--workers", type=int, default=8, help="并发请求数（默认 8，最大 16）")
    args = parser.parse_args()
    try:
        result = refresh_daily_a(workers=args.workers)
    except Exception as exc:
        print(f"数据更新失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({key: value for key, value in result.items() if key != "codes"},
                     ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
