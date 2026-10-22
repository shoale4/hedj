#!/usr/bin/env python3
"""
Kalshi NFL combo pricing: are same-game combos priced as if the legs were
unrelated, and do correlated combos (a QB's passing yards with his own
receiver's yards) pay off more often than they cost?

For a random sample of settled same-game NFL combos (KXMVENFLSINGLEGAME):
1. Take the first trade before kickoff: what the buyer paid
2. Price each leg from its own last hourly quote before that trade; the
   product is the combo's price if the legs were independent
3. Compare paid vs independent price vs how often the combo actually hit,
   overall and for combos whose legs are positively correlated

Public endpoints only; shares the cache and rate limiter in prop_calibration.

    python scripts/combo_pricing.py --sample 4000
"""

import argparse
import json
import random
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

from prop_calibration import CACHE_DIR, _candle_quote, _f, _get

SERIES = "KXMVENFLSINGLEGAME"
START_OFFSET = timedelta(hours=3)   # leg markets expire ~3h after kickoff
MAX_LEGS = 6
STAT_SERIES = {"KXNFLPASSYDS": "QB yds", "KXNFLRECYDS": "rec yds", "KXNFLREC": "receptions",
               "KXNFLRSHYDS": "rush yds", "KXNFLTD": "TD", "KXNFLANYTD": "TD", "KXNFLPASSTDS": "pass TDs"}


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def settled_combos() -> list:
    out, cursor = [], ""
    while True:
        data = _get(f"/historical/markets?series_ticker={SERIES}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        out += [m for m in data.get("markets", []) if m.get("result") in ("yes", "no")
                and 2 <= len(m.get("mve_selected_legs") or []) <= MAX_LEGS]
        cursor = data.get("cursor") or ""
        if not cursor or not data.get("markets"):
            return out


def leg_market(ticker: str) -> dict:
    m = _get(f"/historical/markets/{ticker}").get("market")
    return m or _get(f"/markets/{ticker}").get("market", {})


def leg_price(m: dict, at: datetime) -> Optional[float]:
    """Leg's YES midpoint from its last hourly candle before `at`."""
    series = m["event_ticker"].split("-")[0]
    hist = bool(_get(f"/historical/markets/{m['ticker']}").get("market"))
    path = (f"/historical/markets/{m['ticker']}/candlesticks" if hist
            else f"/series/{series}/markets/{m['ticker']}/candlesticks")
    end = int(at.timestamp())
    # Round the window to the hour so nearby combos share cached candles
    end_h = end - end % 3600
    data = _get(f"{path}?start_ts={end_h - 24 * 3600}&end_ts={end_h}&period_interval=60")
    for c in reversed([c for c in data.get("candlesticks", []) if c.get("end_period_ts", 0) <= end]):
        bid, ask = _candle_quote(c, "yes_bid"), _candle_quote(c, "yes_ask")
        if bid and ask and 0 < bid <= ask < 1 and ask - bid <= 0.10:
            return (bid + ask) / 2
    return None


def team_of(leg_ticker: str) -> str:
    parts = leg_ticker.split("-")
    return parts[2][:3] if len(parts) > 2 else ""


def classify(legs: list) -> str:
    """'qb+wr' when the combo holds a QB passing-yards over and a same-team receiver's yards over."""
    kinds = [(STAT_SERIES.get(l["event_ticker"].split("-")[0]), team_of(l["market_ticker"]), l["side"]) for l in legs]
    qb_teams = {t for k, t, s in kinds if k == "QB yds" and s == "yes"}
    if any(k in ("rec yds", "receptions") and s == "yes" and t[:2] in {q[:2] for q in qb_teams} for k, t, s in kinds):
        return "qb+receiver stack"
    if all(k for k, _, _ in kinds):
        return "player props, other"
    return "includes game lines"


def evaluate(combo: dict) -> Optional[dict]:
    legs = combo["mve_selected_legs"]
    markets = [leg_market(l["market_ticker"]) for l in legs]
    if not all(markets):
        return None
    occ = markets[0].get("occurrence_datetime") or markets[0].get("expected_expiration_time")
    if not occ:
        return None
    kickoff = ts(occ) - START_OFFSET
    # Settled combos' trades live in the historical archive
    trades = _get(f"/historical/trades?ticker={combo['ticker']}&limit=1000").get("trades", [])
    pre = sorted((t for t in trades if ts(t["created_time"]) < kickoff), key=lambda t: t["created_time"])
    if not pre:
        return None
    first = pre[0]
    paid = _f(first.get("yes_price_dollars"))
    if not paid or not 0 < paid < 1:
        return None
    at = ts(first["created_time"])
    probs = []
    for l, m in zip(legs, markets):
        p = leg_price(m, at)
        if p is None:
            return None
        probs.append(p if l["side"] == "yes" else 1 - p)
    indep = 1.0
    for p in probs:
        indep *= p
    return {"ticker": combo["ticker"], "legs": len(legs), "kind": classify(legs), "paid": paid,
            "independent": indep, "won": int(combo["result"] == "yes"),
            "contracts": sum(_f(t.get("count_fp")) for t in pre), "hours_before": (kickoff - at).total_seconds() / 3600}


def report(rows: list):
    def line(label, rs):
        if not rs:
            return
        n = len(rs)
        paid = sum(r["paid"] for r in rs) / n
        indep = sum(r["independent"] for r in rs) / n
        hit = sum(r["won"] for r in rs) / n
        roi = (sum(r["won"] for r in rs) - sum(r["paid"] for r in rs)) / sum(r["paid"] for r in rs) * 100
        print(f"  {label:26s} n={n:5d}  paid {paid*100:5.1f}c  independent {indep*100:5.1f}c  "
              f"hit {hit*100:5.1f}%  paid/indep {paid/indep:4.2f}x  ROI buying YES {roi:+6.1f}%")
    print(f"\n{len(rows):,} combos with a pregame trade and priced legs")
    line("all", rows)
    by = defaultdict(list)
    for r in rows:
        by[r["kind"]].append(r)
    for k in sorted(by):
        line(k, by[k])
    for n in sorted({r["legs"] for r in rows}):
        line(f"{n} legs", [r for r in rows if r["legs"] == n])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=4000)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    combos = settled_combos()
    print(f"{len(combos):,} settled same-game NFL combos with 2-{MAX_LEGS} legs", flush=True)
    random.Random(0).shuffle(combos)
    combos = combos[:args.sample]

    def safe(c):
        try:
            return evaluate(c)
        except Exception as e:
            print(f"  skip {c['ticker']}: {e}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = [r for r in pool.map(safe, combos) if r]
    (CACHE_DIR.parent / "combo_pricing.json").write_text(json.dumps(rows))
    report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
