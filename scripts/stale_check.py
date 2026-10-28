#!/usr/bin/env python3
"""
Stale-quote check: was the backtest's pregame price real?

Candlesticks are only written when something changes, so the "most recent
candle with a quote" at the snapshot can be hours old on a quiet market, and
can predate lineup and injury news. For a sample of every paper strategy's
backtest picks this reports:

- quote age: minutes between the candle the backtest used and the start
- the strategy's ROI at the backtest quote vs at the last real trade before
  the start (taker fee included). If the edge only exists at stale quotes, it
  was never tradeable

    python scripts/stale_check.py --sample 400
"""

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from maker_backtest import DATA, SEASON_CUTOFF, picks_from_snapshots, trades_between, ts  # noqa: E402
from paper_trader import START_OFFSET  # noqa: E402
from prop_calibration import _candle_quote, _f, _get, fetch_settled_markets, taker_fee  # noqa: E402

SNAP_LEAD = 3.25 * 3600   # backtest snapshot = occurrence - 3h15m = start - 15 min


def check(pick: dict, market: dict):
    occ = ts(market.get("occurrence_datetime") or market["expected_expiration_time"])
    start, snap_ts = occ - START_OFFSET.total_seconds(), int(occ - SNAP_LEAD)
    base = "/historical/markets" if market["_historical"] else f"/series/{pick['series']}/markets"
    # Same request prop_calibration made, so it comes from the cache
    data = _get(f"{base}/{market['ticker']}/candlesticks?start_ts={snap_ts - 6 * 3600}&end_ts={snap_ts}&period_interval=60")
    used = None
    for c in reversed([c for c in data.get("candlesticks", []) if c.get("end_period_ts", 0) <= snap_ts]):
        bid, ask = _candle_quote(c, "yes_bid"), _candle_quote(c, "yes_ask")
        if bid and ask and 0 < bid <= ask < 1:
            used = c
            break
    if not used:
        return None
    trades = [t for t in trades_between(market, int(start - 6 * 3600), int(start)) if ts(t["created_time"]) < start]
    last = max(trades, key=lambda t: t["created_time"]) if trades else None

    side = pick["side"]
    won = pick["result"] == (1 if side == "yes" else 0)
    quote = pick["yes_ask"] if side == "yes" else 1 - pick["yes_bid"]
    traded = _f(last.get(f"{side}_price_dollars")) if last else None
    return {
        "strategy": pick["strategy"], "season": "2026-27" if pick["game_date"] >= SEASON_CUTOFF else "2025-26",
        "age_min": (start - used["end_period_ts"]) / 60, "won": won,
        "quote": quote, "traded": traded,
        "last_trade_age_min": (start - ts(last["created_time"])) / 60 if last else None,
    }


def roi(rows, key):
    rows = [r for r in rows if r[key] is not None and 0 < r[key] < 1]
    cost = sum(r[key] + taker_fee(r[key]) for r in rows)
    return (sum(r["won"] for r in rows) - cost) / cost if cost else 0.0, len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=400, help="picks per strategy and season")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    random.seed(11)
    groups = defaultdict(list)
    for p in picks_from_snapshots():
        groups[(p["strategy"], p["game_date"] >= SEASON_CUTOFF)].append(p)
    picks = [p for g in groups.values() for p in random.sample(g, min(args.sample, len(g)))]
    markets = {}
    for series in sorted({p["series"] for p in picks}):
        markets.update({m["ticker"]: m for m in fetch_settled_markets(series)})
    print(f"{len(picks)} picks", file=sys.stderr)

    def run(p):
        try:
            return check(p, markets[p["ticker"]]) if p["ticker"] in markets else None
        except Exception as e:
            print(f"  skip {p['ticker']}: {e}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(args.workers) as ex:
        results = [r for r in ex.map(run, picks) if r]
    (DATA / "stale_check.json").write_text(json.dumps(results))

    by = defaultdict(list)
    for r in results:
        by[(r["strategy"], r["season"])].append(r)
    print(f"{'strategy':<16}{'season':<9}{'n':>5}{'median age':>12}{'>60 min old':>12}"
          f"{'ROI @quote':>12}{'ROI @last trade':>17}{'n traded':>10}{'ROI fresh only':>16}")
    for (sid, season), rows in sorted(by.items()):
        ages = [r["age_min"] for r in rows]
        fresh = [r for r in rows if r["age_min"] <= 60]
        q, _ = roi(rows, "quote")
        t, nt = roi(rows, "traded")
        f, nf = roi(fresh, "quote")
        print(f"{sid:<16}{season:<9}{len(rows):>5}{statistics.median(ages):>10.0f}m"
              f"{sum(a > 60 for a in ages) / len(ages):>12.0%}{q:>+12.1%}{t:>+17.1%}{nt:>10}{f:>+12.1%} ({nf})")


if __name__ == "__main__":
    main()
