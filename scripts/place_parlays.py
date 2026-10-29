#!/usr/bin/env python3
"""
Place the open parlays from paper/parlays.csv on Kalshi, only at or under each
parlay's max price. Real money: run it yourself.

For each open, unplaced parlay whose games haven't started:
1. Checks every leg is still open, and creates the combo market from the legs
   (POST /multivariate_event_collections/{collection})
2. Requests quotes for the stake (POST /communications/rfqs, sized by
   target_cost_dollars so the debit including fees can't exceed the stake)
3. Waits for market makers' quotes. Buying YES on a combo means accepting a
   maker's NO quote (FIX docs: BUY accepts the maker's NO quote), so our price
   is 1 - no_bid
4. Accepts the best quote only if price + taker fee <= the max price, and only
   after you confirm (or --yes); otherwise deletes the request
5. Checks the position landed on YES and records price, contracts and the
   combo ticker in parlays.csv

    source ~/.kalshi/env && python scripts/place_parlays.py --dry-run   # quotes only, buys nothing
    source ~/.kalshi/env && python scripts/place_parlays.py             # asks before each buy
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_client import KalshiClient  # noqa: E402
from parlay_builder import get, load_rows, save_rows  # noqa: E402

TAKER_COEFF = 0.07
QUOTE_WAIT_S = 20          # how long to collect quotes
MAX_SPEND = 25.0           # hard cap per run, whatever the card says


def taker_fee(p: float) -> float:
    return TAKER_COEFF * p * (1 - p)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def find_collection(event_tickers: set) -> str:
    """An open combo collection that includes every leg's event"""
    cursor = ""
    while True:
        data = get("/multivariate_event_collections?status=open&limit=200" + (f"&cursor={cursor}" if cursor else ""))
        for c in data.get("multivariate_contracts", []):
            if event_tickers <= set(c.get("associated_event_tickers", [])):
                return c["collection_ticker"]
        cursor = data.get("cursor") or ""
        if not cursor:
            raise RuntimeError(f"no open combo collection covers {sorted(event_tickers)}")


def best_quote(client: KalshiClient, rfq_id: str):
    """Best (highest NO bid = cheapest YES for us) open quote after QUOTE_WAIT_S"""
    deadline, best = time.time() + QUOTE_WAIT_S, None
    while time.time() < deadline:
        time.sleep(2)
        quotes = client.request("GET", f"/communications/quotes?rfq_id={rfq_id}&rfq_user_filter=self&limit=100").get("quotes", [])
        live = [q for q in quotes if q.get("status") == "open" and _f(q.get("no_bid_dollars")) > 0]
        if live:
            best = max(live, key=lambda q: _f(q["no_bid_dollars"]))
    return best


def yes_position(client: KalshiClient, ticker: str) -> float:
    for p in client.request("GET", f"/portfolio/positions?ticker={ticker}&limit=10").get("market_positions", []):
        if p.get("ticker") == ticker:
            return _f(p.get("position_fp", p.get("position")))
    return 0.0


def place(client: KalshiClient, pid: str, legs: list, dry_run: bool, auto_yes: bool) -> dict:
    stake, max_price = float(legs[0]["stake"]), float(legs[0]["max_price"])
    markets = [get(f"/markets/{l['ticker']}").get("market", {}) for l in legs]
    for l, m in zip(legs, markets):
        if m.get("status") not in ("open", "active"):
            print(f"  skip: {l['title']} is {m.get('status')}")
            return {}
    collection = find_collection({m["event_ticker"] for m in markets})
    combo = client.request("POST", f"/multivariate_event_collections/{collection}", {
        "selected_markets": [{"market_ticker": m["ticker"], "event_ticker": m["event_ticker"], "side": "yes"}
                             for m in markets],
    })["market_ticker"]
    rfq_id = client.request("POST", "/communications/rfqs", {
        "market_ticker": combo, "target_cost_dollars": f"{stake:.2f}",
        "rest_remainder": False, "obscure_creator_id": True,
    })["id"]
    print(f"  combo {combo}: waiting {QUOTE_WAIT_S}s for quotes...")
    try:
        quote = best_quote(client, rfq_id)
        if not quote:
            print("  no quotes: skip")
            return {}
        price = round(1 - _f(quote["no_bid_dollars"]), 4)
        cost = price + taker_fee(price)
        print(f"  best quote: YES at {price * 100:.1f}¢ (+fee {cost * 100:.1f}¢), max {max_price * 100:.1f}¢,"
              f" pays {1 / price:.1f}x")
        if cost > max_price:
            print("  over the max price: skip")
            return {}
        if dry_run:
            print("  dry run: not buying")
            return {}
        if not auto_yes and input(f"  Buy ${stake:.0f} at {price * 100:.1f}¢? [y/N] ").strip().lower() != "y":
            print("  skipped")
            return {}
        client.request("PUT", f"/communications/rfqs/{rfq_id}/quotes/{quote['id']}/accept", {"accepted_side": "no"})
        time.sleep(6)   # combo quotes confirm within 3s and execute 1s later
        held = yes_position(client, combo)
        if held < 0:
            print(f"  !!! position is {held} (NO side). Check the Kalshi app now")
        elif held == 0:
            print("  not filled (maker didn't confirm)")
            return {}
        else:
            print(f"  bought {held:g} YES contracts")
        return {"combo_ticker": combo, "paid_price": f"{price:.4f}", "contracts": f"{held:g}",
                "placed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    finally:
        try:
            client.request("DELETE", f"/communications/rfqs/{rfq_id}")
        except RuntimeError:
            pass   # already executed or closed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="get quotes, buy nothing")
    ap.add_argument("--yes", action="store_true", help="don't ask before each buy")
    ap.add_argument("--max-spend", type=float, default=MAX_SPEND)
    args = ap.parse_args()

    client = KalshiClient()
    balance = client.balance_dollars()
    print(f"Kalshi balance ${balance:.2f}")
    rows, now, spent = load_rows(), datetime.now(timezone.utc), 0.0
    parlays = {}
    for r in rows:
        parlays.setdefault(r["parlay_id"], []).append(r)
    for pid, legs in parlays.items():
        if legs[0]["status"] != "open" or legs[0].get("placed_at"):
            continue
        if min(datetime.fromisoformat(l["game_start"].replace("Z", "+00:00")) for l in legs) <= now:
            continue
        stake = float(legs[0]["stake"])
        print(f"\nParlay {pid} ({len(legs)} legs, ${stake:.0f}):")
        for l in legs:
            print(f"  - {l['title']}")
        if spent + stake > args.max_spend or (not args.dry_run and stake > balance - spent):
            print("  over the spend cap or balance: skip")
            continue
        try:
            result = place(client, pid, legs, args.dry_run, args.yes)
        except RuntimeError as e:
            print(f"  failed: {e}")
            continue
        if result:
            spent += stake
            for l in legs:
                l.update(result)
            save_rows(rows)   # record each buy as soon as it happens
    print(f"\nSpent ${spent:.2f}")


if __name__ == "__main__":
    main()
