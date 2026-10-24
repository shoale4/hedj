#!/usr/bin/env python3
"""
Maker-order backtest: would posting a limit order beat paying the ask?

For every pregame snapshot that a paper strategy (paper_trader.STRATEGIES)
would have bought, simulate a resting limit order instead of a taker fill:

- Price: one cent better than the best bid on our side (joins the bid when the
  spread is only a cent), so we are first in the queue
- Placed at the snapshot (LEAD minutes before the start), cancelled at the start
- Fills if any later pregame trade had a taker selling into our price
  (taker_side opposite to ours, at or through our price). --strict requires the
  trade to go through our price, which is certain to fill even with a queue
- Maker fee 0.0175*p*(1-p) (Kalshi's maker rate on fee-charging series)

Unfilled orders are a missed bet, not a loss, so the fair comparison is profit
per opportunity: taker profit over all picks vs maker profit over filled
picks, both divided by the number of picks. Adverse selection shows up as
filled orders winning less often than unfilled ones.

    python scripts/maker_backtest.py                    # 15 min before the start
    python scripts/maker_backtest.py --lead-min 120     # rest it for two hours
    python scripts/maker_backtest.py --sample 400 --strict
"""

import argparse
import json
import random
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from paper_trader import QUIET_OI, START_OFFSET, STRATEGIES  # noqa: E402
from prop_calibration import _f, _get, fetch_settled_markets, pregame_snapshot, taker_fee  # noqa: E402

DATA = Path(__file__).parent.parent / "data" / "prediction_markets"
SNAPSHOT_FILES = ["nfl_v2", "nfl_td2026", "nba", "ncaaf", "nhl"]
MAKER_COEFF = 0.0175
SEASON_CUTOFF = "2026-08-01"   # 2025-26 before, 2026-27 after


def maker_fee(p: float) -> float:
    return MAKER_COEFF * p * (1 - p)


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def trades_between(market: dict, lo: int, hi: int) -> list:
    base = "/historical/trades" if market["_historical"] else "/markets/trades"
    out, cursor = [], ""
    while True:
        path = f"{base}?ticker={market['ticker']}&min_ts={lo}&max_ts={hi}&limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        data = _get(path)
        out += data.get("trades", [])
        cursor = data.get("cursor") or ""
        if not cursor or not data.get("trades"):
            return out


def simulate(pick: dict, market: dict, lead: timedelta, strict: bool):
    start = ts(market.get("occurrence_datetime") or market["expected_expiration_time"]) - START_OFFSET.total_seconds()
    placed = start - lead.total_seconds()
    if lead == timedelta(minutes=15):
        bid, ask = pick["yes_bid"], pick["yes_ask"]
    else:
        # Re-quote at the earlier placement time (snapshot lead is measured from start + 3h)
        snap = pregame_snapshot(market, pick["series"], lead + START_OFFSET)
        if not snap:
            return None
        bid, ask = snap.yes_bid, snap.yes_ask
    side = pick["side"]
    if side == "yes":
        taker_price = ask
        limit = round(bid + 0.01, 2) if bid + 0.01 < ask - 1e-9 else bid
    else:
        taker_price = round(1 - bid, 2)
        limit = round(1 - ask + 0.01, 2) if 1 - ask + 0.01 < 1 - bid - 1e-9 else round(1 - ask, 2)

    filled = False
    for t in trades_between(market, int(placed), int(start)):
        if ts(t["created_time"]) >= start:
            continue
        price = _f(t.get(f"{side}_price_dollars"))
        if price is None or t.get("taker_side") == side:
            continue   # a taker buying our side never hits our bid
        if price < limit - 1e-9 or (not strict and price <= limit + 1e-9):
            filled = True
            break

    won = pick["result"] == (1 if side == "yes" else 0)
    taker_profit = won - taker_price - taker_fee(taker_price)
    maker_profit = won - limit - maker_fee(limit) if filled else 0.0
    return {
        "strategy": pick["strategy"], "season": "2026-27" if pick["game_date"] >= SEASON_CUTOFF else "2025-26",
        "game": pick["ticker"].split("-")[1], "won": won, "filled": filled,
        "taker_cost": taker_price + taker_fee(taker_price), "taker_profit": taker_profit,
        "maker_cost": (limit + maker_fee(limit)) if filled else 0.0, "maker_profit": maker_profit,
        "saved": taker_price - limit,
    }


def picks_from_snapshots():
    picks = []
    for name in SNAPSHOT_FILES:
        f = DATA / f"{name}_snapshots.json"
        if not f.exists():
            continue
        for s in json.loads(f.read_text()):
            mid = (s["yes_bid"] + s["yes_ask"]) / 2
            for sid, (series, (lo, hi), side, quiet_only) in STRATEGIES.items():
                if s["series"] not in series or not lo <= mid < hi:
                    continue
                if quiet_only and s.get("pregame_oi", 0) >= QUIET_OI.get(s["series"], 0):
                    continue
                picks.append({**s, "strategy": sid, "side": side})
    return picks


def cluster_ci(rows, key_profit, key_cost, n_boot=1000):
    """95% range of ROI, resampling whole games (ladder rungs move together)"""
    games = defaultdict(list)
    for r in rows:
        games[r["game"]].append(r)
    keys = list(games)
    vals = []
    for _ in range(n_boot):
        sample = [r for k in random.choices(keys, k=len(keys)) for r in games[k]]
        cost = sum(r[key_cost] for r in sample)
        if cost:
            vals.append(sum(r[key_profit] for r in sample) / cost)
    vals.sort()
    return (vals[int(len(vals) * .025)], vals[int(len(vals) * .975)]) if vals else (0, 0)


def report(results):
    groups = defaultdict(list)
    for r in results:
        groups[(r["strategy"], r["season"])].append(r)
        groups[(r["strategy"], "all")].append(r)
    print(f"{'strategy':<16}{'season':<9}{'picks':>6}{'fill':>6}{'saved':>7}"
          f"{'taker ROI':>11}{'maker ROI':>11}{'  maker 95% range':>18}"
          f"{'taker $/pick':>13}{'maker $/pick':>13}{'win filled':>11}{'win unfilled':>13}")
    for (sid, season), rows in sorted(groups.items()):
        n = len(rows)
        filled = [r for r in rows if r["filled"]]
        unfilled = [r for r in rows if not r["filled"]]
        t_cost = sum(r["taker_cost"] for r in rows)
        m_cost = sum(r["maker_cost"] for r in filled)
        t_roi = sum(r["taker_profit"] for r in rows) / t_cost if t_cost else 0
        m_roi = sum(r["maker_profit"] for r in filled) / m_cost if m_cost else 0
        lo, hi = cluster_ci(filled, "maker_profit", "maker_cost") if season == "all" and filled else (None, None)
        rng = f"{lo:+.0%} to {hi:+.0%}" if lo is not None else ""
        saved = sum(r["saved"] for r in rows) / n
        wf = sum(r["won"] for r in filled) / len(filled) if filled else 0
        wu = sum(r["won"] for r in unfilled) / len(unfilled) if unfilled else 0
        print(f"{sid:<16}{season:<9}{n:>6}{len(filled) / n:>6.0%}{saved * 100:>6.1f}¢"
              f"{t_roi:>+11.1%}{m_roi:>+11.1%}{rng:>18}"
              f"{sum(r['taker_profit'] for r in rows) / n * 100:>12.2f}¢"
              f"{sum(r['maker_profit'] for r in filled) / n * 100:>12.2f}¢"
              f"{wf:>11.1%}{wu:>13.1%}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lead-min", type=int, default=15, help="minutes before the start the order is placed")
    ap.add_argument("--sample", type=int, default=500, help="max picks per strategy and season (0 = all)")
    ap.add_argument("--strict", action="store_true", help="only count fills when a trade went through our price")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="maker_results.json")
    args = ap.parse_args()

    random.seed(7)
    picks = picks_from_snapshots()
    by_group = defaultdict(list)
    for p in picks:
        by_group[(p["strategy"], p["game_date"] >= SEASON_CUTOFF)].append(p)
    if args.sample:
        picks = [p for g in by_group.values() for p in random.sample(g, min(args.sample, len(g)))]
    print(f"{len(picks)} picks across {len(by_group)} strategy-seasons", file=sys.stderr)

    markets = {}
    for series in sorted({p["series"] for p in picks}):
        markets.update({m["ticker"]: m for m in fetch_settled_markets(series)})

    lead = timedelta(minutes=args.lead_min)
    done = [0]

    def run(p):
        m = markets.get(p["ticker"])
        try:
            r = simulate(p, m, lead, args.strict) if m else None
        except Exception as e:
            print(f"  skip {p['ticker']}: {e}", file=sys.stderr)
            r = None
        done[0] += 1
        if done[0] % 500 == 0:
            print(f"  {done[0]}/{len(picks)}", file=sys.stderr)
        return r

    with ThreadPoolExecutor(args.workers) as ex:
        results = [r for r in ex.map(run, picks) if r]
    (DATA / args.out).write_text(json.dumps(results))
    print(f"\nMaker at bid+1¢, placed {args.lead_min} min before the start"
          f"{', strict fills' if args.strict else ''} ({len(results)} picks)\n")
    report(results)


if __name__ == "__main__":
    main()
