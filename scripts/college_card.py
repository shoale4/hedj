"""
Saturday college football card: cheap team-total overs on Kalshi.

Paper trading (ncaaf_tt_overs in paper_trader.py, Oct 19 - Nov 14 2026) bought
every KXNCAAFTEAMTOTAL over at 3-30c and returned +10% over ~150 team-games,
positive in 3 of 4 weeks and in every price bucket. Kalshi underprices the
over, same pattern as the NFL long shots.

The card: one rung per game (closest to TARGET_ASK within ASK_RANGE), games
starting between 20 minutes and LOOKAHEAD hours out, N picks at STAKE each.
Logged to paper/parlays.csv as single-leg "long shot" rows with ids like
20261114c-1, so place_parlays.py buys them under their own per-card cap and
parlay_builder.py --settle settles them.

    python scripts/college_card.py             # build and log today's card
    python scripts/college_card.py --dry-run   # print only
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import kalshi_edge_scanner as scanner  # noqa: E402
import parlay_builder as pb  # noqa: E402

SERIES = "KXNCAAFTEAMTOTAL"
ASK_RANGE = (0.10, 0.30)
TARGET_ASK = 0.15
MIN_ASK_SIZE = 20          # contracts at the ask, enough for the stake
LOOKAHEAD = timedelta(hours=14)
MIN_LEAD = timedelta(minutes=20)
PAPER_ROI = 0.10           # paper return, used as the fair estimate
SLIPPAGE = 0.01            # buy up to 1c over the build-time ask


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def open_markets() -> list:
    out, cursor = [], ""
    while True:
        d = pb.get(f"/markets?series_ticker={SERIES}&status=open&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        out += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            return out


def pick(n: int) -> list:
    now = datetime.now(timezone.utc)
    best = {}   # event (game) -> market
    for m in open_markets():
        if not m.get("occurrence_datetime"):
            continue
        start = datetime.fromisoformat(m["occurrence_datetime"].replace("Z", "+00:00")) - pb.START_OFFSET
        ask = _f(m.get("yes_ask_dollars"))
        if not (now + MIN_LEAD <= start <= now + LOOKAHEAD) or not ASK_RANGE[0] <= ask <= ASK_RANGE[1]:
            continue
        if _f(m.get("yes_ask_size_fp")) < MIN_ASK_SIZE:
            continue
        m = {**m, "_start": start, "_ask": ask}
        key = (abs(ask - TARGET_ASK), -_f(m.get("open_interest_fp")))
        cur = best.get(m["event_ticker"])
        if cur is None or key < (abs(cur["_ask"] - TARGET_ASK), -_f(cur.get("open_interest_fp"))):
            best[m["event_ticker"]] = m
    ranked = sorted(best.values(), key=lambda m: (abs(m["_ask"] - TARGET_ASK), -_f(m.get("open_interest_fp"))))
    return ranked[:n]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--picks", type=int, default=4)
    ap.add_argument("--stake", type=float, default=2.5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    prefix = f"{now:%Y%m%d}c-"
    rows = pb.load_rows()
    if not args.dry_run and any(r["parlay_id"].startswith(prefix) for r in rows):
        print("Today's college card is already built")
        return
    picks = pick(args.picks)
    if not picks:
        print("No college team-total overs in range: pass")
        return
    for i, m in enumerate(picks, 1):
        ask = m["_ask"]
        cost = ask + scanner.taker_fee(ask)
        fair = cost * (1 + PAPER_ROI)
        print(f"{i}. {m['title']}  ask {ask * 100:.0f}c, pays {1 / cost:.1f}x  [{m['_start']:%a %-I:%M%p} UTC]")
        rows.append({
            "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "parlay_id": f"{prefix}{i}", "kind": "long shot",
            "status": "open", "legs": 1, "parlay_fair": f"{fair:.4f}", "est_price": f"{cost:.4f}",
            "max_price": f"{ask + SLIPPAGE + scanner.taker_fee(ask + SLIPPAGE):.4f}", "stake": f"{args.stake:.2f}",
            "ticker": m["ticker"], "title": m["title"], "game_start": m["_start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "line": m.get("floor_strike", ""), "leg_ask": f"{ask:.2f}", "leg_fair": f"{fair:.4f}",
            "why": f"college team-total over at {ask * 100:.0f}c (paper +{PAPER_ROI:.0%})",
            "leg_result": "", "settled_at": "",
        })
    if not args.dry_run:
        pb.save_rows(rows)
        print(f"Logged {len(picks)} bets to paper/parlays.csv")


if __name__ == "__main__":
    main()
