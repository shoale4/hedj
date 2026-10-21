#!/usr/bin/env python3
"""
Kalshi Prop Edge Scanner

Compares Kalshi player-prop ladders ("Rome Odunze: 60+ receiving yards") to
de-vigged sportsbook prices and flags Kalshi contracts priced below fair.

Fair value per (player, stat, line):
1. Clean two-sided quotes (Pinnacle, FanDuel, DraftKings, exchanges...) are
   de-vigged directly. Junk exchange quotes (overround > MAX_OVERROUND) are dropped.
2. One-sided alt ladders (books only post Overs) are de-vigged by shifting the
   book's whole ladder in logit space so its main-line point matches the
   consensus fair price at that point.
3. Per line, take the weighted median across books, then force the ladder to
   be monotonic (P(60+) >= P(70+)).

Kalshi lines map exactly: "60+" == Over 59.5 (Kalshi floor_strike).

Usage:
    python scripts/kalshi_edge_scanner.py                     # NFL, live pull
    python scripts/kalshi_edge_scanner.py --sport nba
    python scripts/kalshi_edge_scanner.py --use-cache         # reuse last odds pull (0 credits)
    python scripts/kalshi_edge_scanner.py --events 3          # only next 3 games (cheap test)
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data" / "prediction_markets"
ODDS_CACHE = DATA_DIR / "odds_cache"

ODDS_BASE = "https://api.the-odds-api.com/v4"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Up to 10 bookmakers bill as one region, so this list costs the same as one.
BOOKMAKERS = [
    "pinnacle", "fanduel", "draftkings", "williamhill_us", "betmgm",
    "fanatics", "novig", "prophetx", "underdog", "betrivers",
]

# Sharper books count more toward the consensus
BOOK_WEIGHTS = {
    "pinnacle": 3.0, "novig": 2.0, "prophetx": 2.0, "fanduel": 2.0,
    "draftkings": 1.5, "williamhill_us": 1.0, "betmgm": 1.0,
    "fanatics": 1.0, "betrivers": 1.0, "underdog": 1.0,
}

MAX_OVERROUND = 1.10        # drop two-sided quotes with more vig than this

# Exchange/DFS one-sided quotes are often stale resting asks; only trust
# sportsbook alt ladders for shape.
ONE_SIDED_EXCLUDE = {"novig", "prophetx", "underdog"}
MIN_EDGE = 0.02             # 2c after fees
TAKER_COEFF = 0.07

# stat -> (Kalshi series, Odds API market keys)
SPORTS = {
    "nfl": {
        "odds_sport": "americanfootball_nfl",
        "stats": {
            "reception_yds": ("KXNFLRECYDS", ["player_reception_yds", "player_reception_yds_alternate"]),
            "rush_yds": ("KXNFLRSHYDS", ["player_rush_yds", "player_rush_yds_alternate"]),
            "pass_yds": ("KXNFLPASSYDS", ["player_pass_yds", "player_pass_yds_alternate"]),
            "receptions": ("KXNFLREC", ["player_receptions", "player_receptions_alternate"]),
            "pass_tds": ("KXNFLPASSTDS", ["player_pass_tds", "player_pass_tds_alternate"]),
            "anytime_td": ("KXNFLTD", ["player_anytime_td"]),  # KXNFLANYTD pre-2026
        },
    },
    "nba": {
        "odds_sport": "basketball_nba",
        "stats": {
            "points": ("KXNBAPTS", ["player_points", "player_points_alternate"]),
            "rebounds": ("KXNBAREB", ["player_rebounds", "player_rebounds_alternate"]),
            "assists": ("KXNBAAST", ["player_assists", "player_assists_alternate"]),
            "threes": ("KXNBA3PT", ["player_threes", "player_threes_alternate"]),
        },
    },
}

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get_json(url: str, retries: int = 5) -> Tuple[dict, dict]:
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "hedj-edge-scanner/1.0"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode()), dict(resp.headers)
        except HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def load_api_key() -> str:
    key = os.environ.get("ODDS_API_KEY", "")
    if key:
        return key
    cfg = ROOT / "config.json"
    if cfg.exists():
        return json.loads(cfg.read_text())["provider"]["api_key"]
    raise SystemExit("Set ODDS_API_KEY or create config.json")

# ---------------------------------------------------------------------------
# Odds math
# ---------------------------------------------------------------------------

def american_to_prob(price: float) -> float:
    if price < 0:
        return -price / (-price + 100)
    return 100 / (price + 100)


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def inv_logit(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def weighted_median(pairs: List[Tuple[float, float]]) -> float:
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def taker_fee(p: float) -> float:
    return TAKER_COEFF * p * (1 - p)


def norm_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z ]", "", name.lower())
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    return " ".join(name.split())

# ---------------------------------------------------------------------------
# Sportsbook side
# ---------------------------------------------------------------------------

def fetch_events(api_key: str, odds_sport: str, hours_ahead: int) -> List[dict]:
    """Event list is free (0 credits)."""
    data, _ = _get_json(f"{ODDS_BASE}/sports/{odds_sport}/events?apiKey={api_key}")
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    events = []
    for e in data:
        start = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        if now < start <= cutoff:
            events.append(e)
    return sorted(events, key=lambda e: e["commence_time"])


def fetch_event_props(api_key: str, odds_sport: str, event_id: str,
                      markets: List[str], use_cache: bool) -> dict:
    cache_file = ODDS_CACHE / f"{event_id}.json"
    if use_cache:
        if cache_file.exists():
            return json.loads(cache_file.read_text())
        print(f"  no cache for {event_id}, skipping (use-cache mode)")
        return {}

    url = (f"{ODDS_BASE}/sports/{odds_sport}/events/{event_id}/odds?apiKey={api_key}"
           f"&bookmakers={','.join(BOOKMAKERS)}&markets={','.join(markets)}&oddsFormat=american")
    data, headers = _get_json(url)
    print(f"  credits: last={headers.get('x-requests-last')} "
          f"remaining={headers.get('x-requests-remaining')}")
    ODDS_CACHE.mkdir(parents=True, exist_ok=True)
    data["_fetched_at"] = datetime.now(timezone.utc).isoformat()
    cache_file.write_text(json.dumps(data))
    return data


def build_fair_ladders(event_data: dict, market_keys: List[str]) -> Dict[str, Dict[float, float]]:
    """Returns {normalized player: {line: fair P(over)}} for one stat."""
    # book -> player -> line -> {"Over": p, "Under": p}
    quotes: Dict[str, Dict[str, Dict[float, Dict[str, float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(dict)))
    for book in event_data.get("bookmakers", []):
        for market in book.get("markets", []):
            if market["key"] not in market_keys:
                continue
            for o in market["outcomes"]:
                player = norm_name(o.get("description", ""))
                side = o["name"]
                if side == "Yes":      # anytime TD style markets
                    side = "Over"
                elif side == "No":
                    side = "Under"
                line = o.get("point") or 0.5
                if not player or side not in ("Over", "Under") or line is None:
                    continue
                # Same line can appear in main + alternate; keep the better price
                p = american_to_prob(o["price"])
                prev = quotes[book["key"]][player][line].get(side)
                quotes[book["key"]][player][line][side] = min(p, prev) if prev else p

    # Pass 1: clean two-sided quotes -> direct de-vig
    two_sided: Dict[str, Dict[float, List[Tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
    for book, players in quotes.items():
        w = BOOK_WEIGHTS.get(book, 1.0)
        for player, lines in players.items():
            for line, sides in lines.items():
                if "Over" in sides and "Under" in sides:
                    total = sides["Over"] + sides["Under"]
                    if 1.0 <= total <= MAX_OVERROUND:
                        two_sided[player][line].append((sides["Over"] / total, w))

    anchors: Dict[str, Dict[float, float]] = {
        player: {line: weighted_median(v) for line, v in lines.items()}
        for player, lines in two_sided.items()
    }

    # Pass 2: one-sided Over ladders, shifted so they agree with the anchors
    estimates: Dict[str, Dict[float, List[Tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
    for player, lines in two_sided.items():
        for line, v in lines.items():
            estimates[player][line].extend(v)

    for book, players in quotes.items():
        if book in ONE_SIDED_EXCLUDE:
            continue
        w = BOOK_WEIGHTS.get(book, 1.0) * 0.5   # shape-only info counts less
        for player, lines in players.items():
            if player not in anchors:
                continue
            overs = {line: s["Over"] for line, s in lines.items() if "Over" in s and "Under" not in s}
            if not overs:
                continue
            # Shift = this book's raw Over vs consensus fair at the anchor lines it shares
            shared = [line for line in lines if line in anchors[player] and "Over" in lines[line]]
            if not shared:
                continue
            shift = sum(logit(lines[l]["Over"]) - logit(anchors[player][l]) for l in shared) / len(shared)
            for line, p_raw in overs.items():
                estimates[player][line].append((inv_logit(logit(p_raw) - shift), w))

    ladders: Dict[str, Dict[float, float]] = {}
    for player, lines in estimates.items():
        pts = sorted((line, weighted_median(v)) for line, v in lines.items() if v)
        # Enforce monotonic non-increasing P(over) as the line rises
        fixed: List[Tuple[float, float]] = []
        for line, p in pts:
            if fixed and p > fixed[-1][1]:
                # pool-adjacent-violators lite: average with previous
                prev_line, prev_p = fixed.pop()
                avg = (prev_p + p) / 2
                fixed.append((prev_line, avg))
                fixed.append((line, avg))
            else:
                fixed.append((line, p))
        ladders[player] = dict(fixed)
    return ladders


def fair_at(ladder: Dict[float, float], line: float) -> Optional[float]:
    """Exact line, or logit-interpolate between neighbors. Never extrapolate."""
    if line in ladder:
        return ladder[line]
    lines = sorted(ladder)
    for lo, hi in zip(lines, lines[1:]):
        if lo < line < hi:
            t = (line - lo) / (hi - lo)
            return inv_logit(logit(ladder[lo]) * (1 - t) + logit(ladder[hi]) * t)
    return None

# ---------------------------------------------------------------------------
# Kalshi side
# ---------------------------------------------------------------------------

def fetch_kalshi_open(series: str) -> List[dict]:
    markets, cursor = [], ""
    while True:
        url = f"{KALSHI_BASE}/markets?series_ticker={series}&status=open&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        data, _ = _get_json(url)
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor") or ""
        if not cursor or not data.get("markets"):
            return markets


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

@dataclass
class Edge:
    ticker: str
    title: str
    game: str
    side: str            # "YES" or "NO"
    price: float         # what we'd pay (taker)
    fair: float          # fair prob of the side we buy
    edge: float          # fair - price - fee
    size_available: float
    n_books: int
    kalshi_url: str


def kalshi_line(m: dict) -> Optional[float]:
    if m.get("floor_strike") is not None:
        return float(m["floor_strike"])
    match = re.search(r"(\d+)\+", m.get("yes_sub_title") or m.get("title", ""))
    return float(match.group(1)) - 0.5 if match else None


def scan(sport: str, hours_ahead: int, max_events: Optional[int], use_cache: bool,
         min_edge: float) -> List[Edge]:
    cfg = SPORTS[sport]
    api_key = load_api_key()
    all_market_keys = [k for _, keys in cfg["stats"].values() for k in keys]

    events = fetch_events(api_key, cfg["odds_sport"], hours_ahead)
    if max_events:
        events = events[:max_events]
    print(f"{len(events)} {sport.upper()} games in next {hours_ahead}h "
          f"(~{len(events) * len(all_market_keys)} credits max)")

    # player -> stat -> ladder, plus game label + book count
    ladders: Dict[str, Dict[str, Dict[float, float]]] = defaultdict(dict)
    player_game: Dict[str, str] = {}
    player_books: Dict[str, int] = {}
    for e in events:
        game = f"{e['away_team']} @ {e['home_team']}"
        print(f"{game}  {e['commence_time']}")
        data = fetch_event_props(api_key, cfg["odds_sport"], e["id"], all_market_keys, use_cache)
        n_books = len(data.get("bookmakers", []))
        for stat, (_, keys) in cfg["stats"].items():
            for player, ladder in build_fair_ladders(data, keys).items():
                ladders[player][stat] = ladder
                player_game[player] = game
                player_books[player] = n_books

    edges: List[Edge] = []
    for stat, (series, _) in cfg["stats"].items():
        try:
            kmarkets = fetch_kalshi_open(series)
        except Exception as ex:
            print(f"Kalshi {series} failed: {ex}")
            continue
        matched = 0
        for m in kmarkets:
            player = norm_name((m.get("title") or "").split(":")[0])
            line = kalshi_line(m)
            ladder = ladders.get(player, {}).get(stat)
            if not ladder or line is None:
                continue
            fair = fair_at(ladder, line)
            if fair is None:
                continue
            matched += 1
            yes_ask, yes_bid = _f(m.get("yes_ask_dollars")), _f(m.get("yes_bid_dollars"))
            url = f"https://kalshi.com/markets/{series.lower()}/{m['event_ticker'].lower()}"
            if 0 < yes_ask < 1:
                e_yes = fair - yes_ask - taker_fee(yes_ask)
                if e_yes >= min_edge:
                    edges.append(Edge(m["ticker"], m["title"], player_game[player], "YES",
                                      yes_ask, fair, e_yes, _f(m.get("yes_ask_size_fp")),
                                      player_books[player], url))
            if 0 < yes_bid < 1:
                no_ask = 1 - yes_bid
                e_no = (1 - fair) - no_ask - taker_fee(no_ask)
                if e_no >= min_edge:
                    edges.append(Edge(m["ticker"], m["title"], player_game[player], "NO",
                                      no_ask, 1 - fair, e_no, _f(m.get("yes_bid_size_fp")),
                                      player_books[player], url))
        print(f"Kalshi {series}: {len(kmarkets)} open, {matched} matched to book prices")

    return sorted(edges, key=lambda x: -x.edge)

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_edges(edges: List[Edge]):
    if not edges:
        print("\nNo edges above threshold")
        return
    print(f"\n{'Side':<5}{'Price':>7}{'Fair':>7}{'Edge':>7}{'Size':>8}  Market")
    print("-" * 90)
    for e in edges:
        print(f"{e.side:<5}{e.price*100:>6.0f}c{e.fair*100:>6.1f}%{e.edge*100:>+6.1f}c"
              f"{e.size_available:>8.0f}  {e.title}  ({e.game})")


def save_edges(edges: List[Edge], sport: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"edges_{sport}_{datetime.now(timezone.utc):%Y%m%dT%H%M}.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(Edge.__dataclass_fields__))
        w.writeheader()
        for e in edges:
            w.writerow(asdict(e))
    return out


def main():
    parser = argparse.ArgumentParser(description="Kalshi prop edge scanner")
    parser.add_argument("--sport", choices=list(SPORTS), default="nfl")
    parser.add_argument("--hours-ahead", type=int, default=96)
    parser.add_argument("--events", type=int, default=None, help="Only the next N games")
    parser.add_argument("--use-cache", action="store_true", help="Reuse cached odds (0 credits)")
    parser.add_argument("--min-edge", type=float, default=MIN_EDGE * 100, help="Cents (default 2)")
    args = parser.parse_args()

    edges = scan(args.sport, args.hours_ahead, args.events, args.use_cache, args.min_edge / 100)
    print_edges(edges)
    if edges:
        print(f"\nSaved {save_edges(edges, args.sport)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
