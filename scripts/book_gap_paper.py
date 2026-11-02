"""
Paper test: do sportsbooks price women's college basketball better than Kalshi?

The calibration backtest (women_lines_snapshots.json, 2025-26 season) found no
reliable edge in Kalshi's women's college moneylines on their own. This checks
the other source of edge, the one behind the parlay card: buy on Kalshi when
the de-vigged sportsbook price says Kalshi is cheap.

Each run (called every 10 min by paper_trade.yml):
1. Free Odds API events call. If a game tips off in the next LOOKAHEAD_MIN
   minutes and isn't logged yet, one moneyline odds call (1 credit) for those
   games only.
2. Matches each game to its Kalshi KXNCAAWBGAME markets and logs both teams:
   book fair, Kalshi bid/ask/size, and a paper pick when a side clears
   MIN_EDGE after fees.
3. Settles logged rows from Kalshi results.

    python scripts/book_gap_paper.py            # log + settle
    python scripts/book_gap_paper.py --report   # results only
"""
import argparse
import csv
import json
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))
import kalshi_edge_scanner as scanner  # noqa: E402

ROOT = Path(__file__).parent.parent
LOG = ROOT / "paper" / "wncaab_gaps.csv"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_SPORT = "basketball_wncaab"
SERIES = "KXNCAAWBGAME"
LOOKAHEAD_MIN = 30
MIN_EDGE = 0.02          # book fair must beat Kalshi's price + fee by 2 points
BOOKMAKERS = ["pinnacle", "fanduel", "draftkings", "williamhill_us", "betmgm",
              "fanatics", "betrivers", "bovada", "betonlineag", "lowvig"]
FIELDS = ["logged_at", "game_start", "game", "team", "ticker", "book_fair", "n_books",
          "yes_bid", "yes_ask", "ask_size", "open_interest", "pick", "price", "fee", "edge",
          "result", "profit", "settled_at"]


def get(url: str):
    req = Request(url, headers={"User-Agent": "hedj-gap-paper/1.0"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()), dict(r.headers)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def norm(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", scanner.norm_name(name).lower()).split()
    return " ".join("state" if w == "st" else w for w in words)


# Odds API school names that Kalshi spells differently
ALIASES = {"nc state": "north carolina state"}


def names_match(kalshi: str, odds: str) -> int:
    """Kalshi uses the school ('Arizona St.'), the Odds API school + mascot
    ('Arizona State Sun Devils'). Whole-word prefix match; returns the matched
    length so the longest Kalshi name wins (Arizona St. over Arizona)"""
    k, o = norm(kalshi), norm(odds)
    for short, full in ALIASES.items():
        if o.startswith(short + " "):
            o = full + o[len(short):]
    return len(k) if k and (o == k or o.startswith(k + " ")) else 0


def load() -> list:
    return list(csv.DictReader(open(LOG))) if LOG.exists() else []


def save(rows: list):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n", restval="")
        w.writeheader()
        w.writerows(rows)


def kalshi_open() -> list:
    out, cursor = [], ""
    while True:
        d, _ = get(f"{KALSHI_BASE}/markets?series_ticker={SERIES}&status=open&limit=1000"
                   + (f"&cursor={cursor}" if cursor else ""))
        out += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            return out


def fair_by_team(event: dict) -> dict:
    """Weighted median of de-vigged two-way moneylines -> {team: (fair, n_books)}"""
    quotes = []
    for b in event.get("bookmakers", []):
        for m in b.get("markets", []):
            o = m.get("outcomes", [])
            if m.get("key") != "h2h" or len(o) != 2:
                continue
            p = [scanner.american_to_prob(x["price"]) for x in o]
            if sum(p) > scanner.MAX_OVERROUND:
                continue
            quotes.append((b["key"], {o[i]["name"]: p[i] / sum(p) for i in range(2)}))
    if not quotes:
        return {}
    out = {}
    for team in quotes[0][1]:
        pairs = [(q[team], scanner.BOOK_WEIGHTS.get(book, 1.0)) for book, q in quotes if team in q]
        out[team] = (scanner.weighted_median(pairs), len(pairs))
    return out


def scan(rows: list) -> int:
    key = scanner.load_api_key()
    now = datetime.now(timezone.utc)
    events, _ = get(f"{scanner.ODDS_BASE}/sports/{ODDS_SPORT}/events?apiKey={key}")
    logged = {r["game"] for r in rows}
    due = [e for e in events
           if now < datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) <= now + timedelta(minutes=LOOKAHEAD_MIN)
           and e["id"] not in logged]
    if not due:
        return 0
    odds, headers = get(f"{scanner.ODDS_BASE}/sports/{ODDS_SPORT}/odds?apiKey={key}&markets=h2h&oddsFormat=american"
                        f"&bookmakers={','.join(BOOKMAKERS)}&eventIds={','.join(e['id'] for e in due)}")
    print(f"odds credits: last={headers.get('x-requests-last')} remaining={headers.get('x-requests-remaining')}")
    markets = kalshi_open()
    added = 0
    for e in odds:
        start = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        fair = fair_by_team(e)
        if len(fair) != 2:
            continue
        # Kalshi markets for this game: same day (+-1) and both teams match
        cands = [m for m in markets
                 if m.get("occurrence_datetime")
                 and abs(datetime.fromisoformat(m["occurrence_datetime"].replace("Z", "+00:00")) - start) < timedelta(hours=8)]
        by_event = {}
        for m in cands:
            by_event.setdefault(m["event_ticker"], []).append(m)
        match = None
        for legs in by_event.values():
            pairs = {}
            for team in fair:
                best = max(legs, key=lambda m: names_match(m.get("yes_sub_title", ""), team))
                if names_match(best.get("yes_sub_title", ""), team):
                    pairs[team] = best
            if len(pairs) == 2 and pairs[list(fair)[0]] is not pairs[list(fair)[1]]:
                match = pairs
                break
        if not match:
            print(f"  no Kalshi match: {e['away_team']} @ {e['home_team']}")
            continue
        for team, m in match.items():
            p, n = fair[team]
            bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            pick = price = edge = None
            if 0 < ask < 1 and p - ask - scanner.taker_fee(ask) >= MIN_EDGE:
                pick, price, edge = "YES", ask, p - ask - scanner.taker_fee(ask)
            elif 0 < bid < 1 and (1 - p) - (1 - bid) - scanner.taker_fee(1 - bid) >= MIN_EDGE:
                pick, price, edge = "NO", 1 - bid, (1 - p) - (1 - bid) - scanner.taker_fee(1 - bid)
            rows.append({
                "logged_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "game_start": e["commence_time"],
                "game": e["id"], "team": team, "ticker": m["ticker"], "book_fair": f"{p:.4f}", "n_books": n,
                "yes_bid": f"{bid:.2f}", "yes_ask": f"{ask:.2f}", "ask_size": f"{_f(m.get('yes_ask_size_fp')):.0f}",
                "open_interest": f"{_f(m.get('open_interest_fp')):.0f}", "pick": pick or "",
                "price": f"{price:.2f}" if price else "", "fee": f"{scanner.taker_fee(price):.4f}" if price else "",
                "edge": f"{edge:.4f}" if edge else "",
            })
            added += 1
    return added


def settle(rows: list) -> int:
    now, done = datetime.now(timezone.utc), 0
    for r in rows:
        if r["result"] or datetime.fromisoformat(r["game_start"].replace("Z", "+00:00")) > now - timedelta(hours=2):
            continue
        m, _ = get(f"{KALSHI_BASE}/markets/{r['ticker']}")
        res = m.get("market", {}).get("result")
        if res not in ("yes", "no"):
            continue
        r["result"], r["settled_at"] = res, now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if r["pick"]:
            cost = float(r["price"]) + float(r["fee"])
            r["profit"] = f"{(1 if res == r['pick'].lower() else 0) - cost:.4f}"
        done += 1
    return done


def report(rows: list):
    done = [r for r in rows if r["result"]]
    picks = [r for r in done if r["pick"]]
    print(f"Women's college hoops, books vs Kalshi: {len(rows)} sides logged, {len(done)} settled")
    if done:
        gap = statistics.mean(float(r["book_fair"]) - (float(r["yes_bid"]) + float(r["yes_ask"])) / 2 for r in done)
        print(f"  avg book fair minus Kalshi mid: {gap * 100:+.1f} pts")
    if picks:
        cost = sum(float(r["price"]) + float(r["fee"]) for r in picks)
        pnl = sum(float(r["profit"]) for r in picks)
        wins = sum(r["result"] == r["pick"].lower() for r in picks)
        print(f"  picks: {len(picks)} ({wins} won), ROI {pnl / cost * 100:+.1f}% on ${cost:.2f} at 1 contract each")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    rows = load()
    if not args.report:
        added = scan(rows)
        settled = settle(rows)
        save(rows)
        print(f"logged {added}, settled {settled}")
    report(rows)


if __name__ == "__main__":
    main()
