#!/usr/bin/env python3
"""
Kalshi paper trader: logs the picks the backtested strategies would make, at
live prices, and scores them once the markets settle. No orders are placed and
no API key is needed (public market data only).

Each run:
1. Scans open markets in every series a paper strategy uses
2. For games starting within the next LOOKAHEAD_MIN minutes (the backtest
   priced markets 15 minutes before the start), applies each strategy's rule
   to the current quote and logs a pick: price, fee, open interest, and how
   many contracts were actually available at that price
3. Settles earlier picks whose markets have resolved

Picks are one contract each, like the backtest. A market can count for more
than one strategy (a quiet-market over is also a plain over).

Runs every 15 minutes on GitHub Actions (.github/workflows/paper_trade.yml),
which commits paper/picks.csv.

    python scripts/paper_trader.py            # scan + settle + summary
    python scripts/paper_trader.py --report   # summary only
"""

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
PICKS = Path(__file__).parent.parent / "paper" / "picks.csv"
TAKER_COEFF = 0.07
MAX_SPREAD = 0.10          # same filter as the backtest
LOOKAHEAD_MIN = 45         # log markets whose game starts within this many minutes
START_OFFSET = timedelta(hours=3)   # occurrence_datetime is the start + 3h

NFL_OVERS = {"KXNFLREC", "KXNFLRECYDS", "KXNFLPASSYDS"}
NBA_PROPS = {"KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT"}
# Lowest third of pregame open interest per series in the 2025-27 backtest
QUIET_OI = {"KXNFLREC": 323, "KXNFLRECYDS": 389, "KXNFLPASSYDS": 524}

# The Bet and Watch strategies from the strategy board (build_dashboard_data.py)
# id: (series, (low, high) midpoint range, side, quiet-only)
STRATEGIES = {
    "nfl_overs": (NFL_OVERS, (.03, .30), "yes", False),
    "nfl_overs_quiet": (NFL_OVERS, (.03, .30), "yes", True),
    "nfl_rec_no": ({"KXNFLREC"}, (.70, .94), "no", False),
    "nfl_tt_no": ({"KXNFLTEAMTOTAL"}, (.85, .94), "no", False),
    "nfl_td_no": ({"KXNFLTD"}, (.40, .60), "no", False),
    "nfl_spread_no": ({"KXNFLSPREAD"}, (.60, .80), "no", False),
    "nba_overs": (NBA_PROPS, (.03, .30), "yes", False),
    "nhl_goal_overs": ({"KXNHLGOAL"}, (.03, .30), "yes", False),
    "nhl_pts_overs": ({"KXNHLPTS", "KXNHLAST"}, (.03, .30), "yes", False),
    "ncaaf_tt_overs": ({"KXNCAAFTEAMTOTAL"}, (.03, .30), "yes", False),
    "ncaaf_tt_no": ({"KXNCAAFTEAMTOTAL"}, (.80, .94), "no", False),
}

FIELDS = ["logged_at", "strategy", "ticker", "title", "game_start", "lead_min", "side",
          "yes_bid", "yes_ask", "price", "fee", "cost", "ask_size", "open_interest",
          "result", "won", "profit", "settled_at"]


def taker_fee(p: float) -> float:
    return TAKER_COEFF * p * (1 - p)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def get(path: str) -> dict:
    url = f"{KALSHI_BASE}{path}"
    for attempt in range(6):
        try:
            req = Request(url, headers={"User-Agent": "hedj-paper/1.0"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt)
                continue
            if e.code == 404:
                return {}
            raise
    raise RuntimeError(f"rate limited: {url}")


def open_markets(series: str) -> list:
    markets, cursor = [], ""
    while True:
        data = get(f"/markets?series_ticker={series}&status=open&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        markets += data.get("markets", [])
        cursor = data.get("cursor") or ""
        if not cursor or not data.get("markets"):
            return markets


def game_start(m: dict):
    occ = m.get("occurrence_datetime") or m.get("expected_expiration_time")
    return datetime.fromisoformat(occ.replace("Z", "+00:00")) - START_OFFSET if occ else None


def best_ask_size(ticker: str, side: str) -> float:
    """Contracts available at the best price to buy `side`. Buying YES fills
    against the highest NO bid, and buying NO against the highest YES bid."""
    book = get(f"/markets/{ticker}/orderbook").get("orderbook_fp", {})
    levels = book.get("no_dollars" if side == "yes" else "yes_dollars") or []
    if not levels:
        return 0.0
    price, size = max(levels, key=lambda lv: _f(lv[0]))
    return _f(size)


def load_picks() -> list:
    if not PICKS.exists():
        return []
    with open(PICKS) as f:
        return list(csv.DictReader(f))


def save_picks(rows: list):
    PICKS.parent.mkdir(parents=True, exist_ok=True)
    with open(PICKS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def scan(picks: list, now: datetime) -> int:
    seen = {(p["ticker"], p["strategy"]) for p in picks}
    by_series = defaultdict(list)
    for sid, (series, *_rest) in STRATEGIES.items():
        for s in series:
            by_series[s].append(sid)

    added = 0
    for series, sids in sorted(by_series.items()):
        for m in open_markets(series):
            start = game_start(m)
            if not start or not timedelta(0) < start - now <= timedelta(minutes=LOOKAHEAD_MIN):
                continue
            bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            if bid <= 0 or ask >= 1 or ask < bid or ask - bid > MAX_SPREAD:
                continue
            mid, oi = (bid + ask) / 2, _f(m.get("open_interest_fp"))
            for sid in sids:
                _, (lo, hi), side, quiet_only = STRATEGIES[sid]
                if (m["ticker"], sid) in seen or not lo <= mid < hi:
                    continue
                if quiet_only and oi >= QUIET_OI.get(series, 0):
                    continue
                price = ask if side == "yes" else 1 - bid
                fee = taker_fee(price)
                picks.append({
                    "logged_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "strategy": sid, "ticker": m["ticker"],
                    "title": m.get("title", ""), "game_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "lead_min": round((start - now).total_seconds() / 60), "side": side.upper(),
                    "yes_bid": f"{bid:.2f}", "yes_ask": f"{ask:.2f}", "price": f"{price:.2f}",
                    "fee": f"{fee:.4f}", "cost": f"{price + fee:.4f}",
                    "ask_size": f"{best_ask_size(m['ticker'], side):.0f}", "open_interest": f"{oi:.0f}",
                    "result": "", "won": "", "profit": "", "settled_at": "",
                })
                seen.add((m["ticker"], sid))
                added += 1
    return added


def settle(picks: list, now: datetime) -> int:
    settled, cache = 0, {}
    for p in picks:
        if p["result"]:
            continue
        if datetime.fromisoformat(p["game_start"].replace("Z", "+00:00")) > now - timedelta(hours=2):
            continue
        if p["ticker"] not in cache:
            cache[p["ticker"]] = get(f"/markets/{p['ticker']}").get("market", {})
        m = cache[p["ticker"]]
        result = m.get("result")
        if result in ("yes", "no"):
            won = int((result == "yes") == (p["side"] == "YES"))
            p.update(result=result, won=str(won), profit=f"{won - float(p['cost']):.4f}",
                     settled_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            settled += 1
        elif m.get("status") in ("finalized", "settled") or result:
            p.update(result="void", won="", profit="0", settled_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            settled += 1
    return settled


def report(picks: list):
    print(f"\n{'strategy':18s} {'picks':>6s} {'settled':>8s} {'won':>5s} {'cost':>9s} {'profit':>9s} {'ROI':>7s}  {'median size':>11s}")
    for sid in STRATEGIES:
        rows = [p for p in picks if p["strategy"] == sid]
        if not rows:
            continue
        done = [p for p in rows if p["result"] in ("yes", "no")]
        cost = sum(float(p["cost"]) for p in done)
        profit = sum(float(p["profit"]) for p in done)
        sizes = sorted(float(p["ask_size"]) for p in rows)
        roi = f"{profit / cost * 100:+6.1f}%" if cost else "     —"
        print(f"{sid:18s} {len(rows):6d} {len(done):8d} {sum(int(p['won']) for p in done):5d} "
              f"${cost:8.2f} ${profit:+8.2f} {roi}  {sizes[len(sizes) // 2]:11.0f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="print the summary only")
    args = ap.parse_args()

    picks = load_picks()
    if not args.report:
        now = datetime.now(timezone.utc)
        added, settled = scan(picks, now), settle(picks, now)
        save_picks(picks)
        print(f"{now:%Y-%m-%d %H:%M}Z: {added} new picks, {settled} settled, {len(picks)} total")
    report(picks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
