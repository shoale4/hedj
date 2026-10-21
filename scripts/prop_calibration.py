#!/usr/bin/env python3
"""
Kalshi Prop Calibration Backtest

Tests whether Kalshi sports markets are mispriced at the extremes
(favorite-longshot bias): do markets trading at 5c pregame actually hit 5%
of the time? If longshot YES is overpriced, buying NO on longshots is +EV.

For every settled market in the chosen series:
1. Take the pregame bid/ask from hourly candlesticks (~15 min before kickoff)
2. Compare to how the market actually settled
3. Bucket by price and compute hit rate + EV of buying YES / NO after fees

Public endpoints only, no auth needed. Responses are cached to disk so
re-runs are instant.

Usage:
    python scripts/prop_calibration.py                   # NFL props, all seasons
    python scripts/prop_calibration.py --series KXNFLANYTD KXNFLRECYDS
    python scripts/prop_calibration.py --min-volume 2000
"""

import argparse
import hashlib
import json
import math
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CACHE_DIR = Path(__file__).parent.parent / "data" / "prediction_markets" / "cache"

DEFAULT_SERIES = [
    # Player props
    "KXNFLANYTD", "KXNFL2TD", "KXNFLFIRSTTD",
    "KXNFLRECYDS", "KXNFLRSHYDS", "KXNFLPASSYDS",
    "KXNFLREC", "KXNFLPASSTDS",
    # Game props
    "KXNFLDSTTD", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLTEAMTOTAL", "KXNFLGAME",
]

# occurrence_datetime is the expected event end (NFL: ~kickoff + 3h).
# Snapshot this far before it so in-game info never leaks in. Longest
# matching prefix wins; override with --lead-hours.
LEAD_HOURS = {
    "KXNFL": 3.25,
    "KXNCAAF": 3.25,
    "KXNBA": 3.25,
    "KXNHL": 3.25,
    # Daily highs expire 19:00Z the day after; 29h before is ~10am ET on the day
    "KXHIGH": 29,
}
DEFAULT_LEAD_HOURS = 3.25


def lead_for(series: str, override: Optional[float]) -> timedelta:
    if override is not None:
        return timedelta(hours=override)
    match = max((p for p in LEAD_HOURS if series.startswith(p)), key=len, default=None)
    return timedelta(hours=LEAD_HOURS[match] if match else DEFAULT_LEAD_HOURS)
MAX_SPREAD = 0.10        # skip snapshots with bid/ask wider than 10c
TAKER_COEFF = 0.07       # Kalshi taker fee: 0.07 * p * (1-p) per contract
BUCKETS = [0, 0.03, 0.06, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50,
           0.60, 0.70, 0.80, 0.85, 0.90, 0.94, 0.97, 1.0]

# ---------------------------------------------------------------------------
# HTTP with disk cache + simple rate limiting
# ---------------------------------------------------------------------------

MAX_REQ_PER_SEC = 8
_rate_lock = threading.Lock()
_next_slot = [0.0]


def _throttle():
    with _rate_lock:
        now = time.monotonic()
        wait = _next_slot[0] - now
        _next_slot[0] = max(now, _next_slot[0]) + 1.0 / MAX_REQ_PER_SEC
    if wait > 0:
        time.sleep(wait)


def _get(path: str, cache: bool = True) -> dict:
    url = f"{KALSHI_BASE}{path}"
    key = hashlib.sha1(url.encode()).hexdigest()
    cache_file = CACHE_DIR / f"{key}.json"
    if cache and cache_file.exists():
        return json.loads(cache_file.read_text())

    for attempt in range(8):
        _throttle()
        try:
            req = Request(url, headers={"User-Agent": "hedj-calibration/1.0"})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            break
        except HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt)
                continue
            if e.code == 404:
                return {}
            raise
    else:
        raise RuntimeError(f"rate limited: {url}")

    if cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data))
    return data


def fetch_settled_markets(series: str) -> List[dict]:
    """All settled markets in a series: historical archive + recent live."""
    markets: List[dict] = []
    for base in ("/historical/markets", "/markets"):
        cursor = ""
        while True:
            path = f"{base}?series_ticker={series}&limit=1000"
            if cursor:
                path += f"&cursor={cursor}"
            data = _get(path)
            batch = data.get("markets", [])
            markets.extend(
                {**m, "_historical": base.startswith("/historical")}
                for m in batch if m.get("result") in ("yes", "no")
            )
            cursor = data.get("cursor") or ""
            if not cursor or not batch:
                break
    # Historical and live can overlap around the cutoff
    return list({m["ticker"]: m for m in markets}.values())

# ---------------------------------------------------------------------------
# Pregame snapshot
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    ticker: str
    series: str
    title: str
    yes_bid: float
    yes_ask: float
    result: int   # 1 = YES settled, 0 = NO
    volume: float
    game_date: str

    @property
    def mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _candle_quote(candle: dict, side: str) -> Optional[float]:
    # Live candles use "close_dollars", historical use "close"
    q = candle.get(side) or {}
    return _f(q.get("close_dollars", q.get("close")))


def pregame_snapshot(market: dict, series: str, lead: timedelta) -> Optional[Snapshot]:
    # Historical markets lack occurrence_datetime; expected_expiration_time matches it
    occ = market.get("occurrence_datetime") or market.get("expected_expiration_time")
    if not occ:
        return None
    kickoff_approx = datetime.fromisoformat(occ.replace("Z", "+00:00")) - lead
    snap_ts = int(kickoff_approx.timestamp())
    start_ts = snap_ts - 6 * 3600

    ticker = market["ticker"]
    if market["_historical"]:
        path = f"/historical/markets/{ticker}/candlesticks"
    else:
        path = f"/series/{series}/markets/{ticker}/candlesticks"
    try:
        data = _get(f"{path}?start_ts={start_ts}&end_ts={snap_ts}&period_interval=60")
    except Exception as e:
        print(f"  skip {ticker}: {e}", file=sys.stderr)
        return None
    candles = [c for c in data.get("candlesticks", []) if c.get("end_period_ts", 0) <= snap_ts]

    # Most recent candle with a real two-sided quote
    for c in reversed(candles):
        bid, ask = _candle_quote(c, "yes_bid"), _candle_quote(c, "yes_ask")
        if bid is None or ask is None or bid <= 0 or ask >= 1 or ask < bid:
            continue
        if ask - bid > MAX_SPREAD:
            return None
        return Snapshot(
            ticker=ticker,
            series=series,
            title=market.get("title", ""),
            yes_bid=bid,
            yes_ask=ask,
            result=1 if market["result"] == "yes" else 0,
            volume=_f(market.get("volume_fp", market.get("volume"))) or 0,
            game_date=kickoff_approx.date().isoformat(),
        )
    return None

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def taker_fee(p: float) -> float:
    return TAKER_COEFF * p * (1 - p)


def bucket_label(lo: float, hi: float) -> str:
    return f"{lo*100:>3.0f}-{hi*100:<3.0f}c"


def report(snaps: List[Snapshot], title: str):
    print(f"\n{'='*92}\n{title}  (n={len(snaps):,})\n{'='*92}")
    print(f"{'Mid':<10}{'n':>7}{'Implied':>9}{'Actual':>9}{'±1σ':>7}"
          f"{'Diff':>8}{'YES EV':>10}{'NO EV':>10}{'NO ROI':>9}")
    print("-" * 92)

    tot_no_ev = tot_no_cost = 0.0
    for lo, hi in zip(BUCKETS, BUCKETS[1:]):
        b = [s for s in snaps if lo <= s.mid < hi]
        if len(b) < 20:
            continue
        n = len(b)
        implied = sum(s.mid for s in b) / n
        actual = sum(s.result for s in b) / n
        se = math.sqrt(max(actual * (1 - actual), 1e-9) / n)
        # Taker EV per contract: buy YES at ask, or buy NO at (1 - yes_bid)
        yes_ev = sum(s.result - s.yes_ask - taker_fee(s.yes_ask) for s in b) / n
        no_prices = [1 - s.yes_bid for s in b]
        no_ev = sum((1 - s.result) - p - taker_fee(p) for s, p in zip(b, no_prices)) / n
        no_roi = no_ev / (sum(no_prices) / n)
        if hi <= 0.15:
            tot_no_ev += no_ev * n
            tot_no_cost += sum(no_prices)
        flag = "  <--" if no_roi > 0 and abs(actual - implied) > se else ""
        print(f"{bucket_label(lo, hi):<10}{n:>7,}{implied*100:>8.1f}%{actual*100:>8.1f}%"
              f"{se*100:>6.1f}%{(actual-implied)*100:>+7.1f}%"
              f"{yes_ev*100:>+9.2f}c{no_ev*100:>+9.2f}c{no_roi*100:>+8.2f}%{flag}")

    if tot_no_cost:
        print(f"\nBuy NO on every <15c longshot (taker): ROI {tot_no_ev / tot_no_cost * 100:+.2f}%")


def main():
    parser = argparse.ArgumentParser(description="Kalshi prop calibration backtest")
    parser.add_argument("--series", nargs="+", default=DEFAULT_SERIES)
    parser.add_argument("--min-volume", type=float, default=500,
                        help="Skip markets with lifetime volume below this (contracts)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--lead-hours", type=float, default=None,
                        help="Snapshot this many hours before expected expiration "
                             "(default: per-sport preset)")
    parser.add_argument("--out", default="calibration_snapshots.json",
                        help="Snapshot output filename under data/prediction_markets/")
    parser.add_argument("--include-preseason", action="store_true",
                        help="Include NFL August games (excluded by default)")
    args = parser.parse_args()

    all_snaps: List[Snapshot] = []
    for series in args.series:
        markets = fetch_settled_markets(series)
        markets = [m for m in markets
                   if (_f(m.get("volume_fp", m.get("volume"))) or 0) >= args.min_volume]
        if not args.include_preseason and series.startswith("KXNFL"):
            markets = [m for m in markets if "AUG" not in m["event_ticker"].split("-")[1]]
        print(f"{series}: {len(markets):,} settled markets with volume >= {args.min_volume:.0f}",
              flush=True)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            lead = lead_for(series, args.lead_hours)
            snaps = list(pool.map(lambda m: pregame_snapshot(m, series, lead), markets))
        snaps = [s for s in snaps if s]
        print(f"  {len(snaps):,} with a usable pregame quote", flush=True)
        all_snaps.extend(snaps)

    if not all_snaps:
        print("No data")
        return 1

    out = CACHE_DIR.parent / args.out
    out.write_text(json.dumps([s.__dict__ for s in all_snaps]))
    print(f"\nSaved {len(all_snaps):,} snapshots to {out}")

    report(all_snaps, "ALL SERIES")
    by_series: Dict[str, List[Snapshot]] = defaultdict(list)
    for s in all_snaps:
        by_series[s.series].append(s)
    for series, snaps in sorted(by_series.items(), key=lambda kv: -len(kv[1])):
        if len(snaps) >= 200:
            report(snaps, series)
    return 0


if __name__ == "__main__":
    sys.exit(main())
