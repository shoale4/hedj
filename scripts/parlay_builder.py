#!/usr/bin/env python3
"""
Weekly NFL parlay card: a few 3-4 leg Kalshi parlays built from researched,
fairly priced legs, instead of thousands of single bets.

1. Legs: every open Kalshi NFL player prop (yards, receptions, passing TDs,
   anytime TD) for games in the window, priced against de-vigged sportsbook
   ladders from kalshi_edge_scanner.py. A leg qualifies when Kalshi's ask is
   no worse than LEG_MAX_GAP under the books' fair price.
2. Research per leg from nflverse (download with player_research.py
   --download): last-8 hit rate at the line, last-3 form, snap share, what the
   defense allows to the position, team implied total, injury report.
   Backtests found these only weakly predictive (the books already price most
   of it), so they rank legs and explain picks; they do not override the price.
3. Parlays: search 3-4 leg combos, with joint fair odds adjusted by the
   same-game pair lifts measured in the backtest (QB yards + his receiver hit
   together 1.36x as often as priced independently; RB + teammate receiver
   and two same-team TD scorers hit together less often and are avoided).
   Live Kalshi combo quotes run ~1.45x the legs' prices across games and
   ~2.2x within one game (Oct 29 RFQs), so expected value is fair odds vs that
   estimated combo price.
4. Output: the card with a max price per parlay (only buy if Kalshi quotes
   this or less), appended to paper/parlays.csv, one row per leg.

    python scripts/parlay_builder.py                     # next 7 days, live odds pull
    python scripts/parlay_builder.py --use-cache --dry-run
    python scripts/parlay_builder.py --settle            # score open parlays
"""

import argparse
import csv
import itertools
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))
import kalshi_edge_scanner as scanner  # noqa: E402
from player_research import NFLVERSE, STATS, TEAM_ALIASES, load_box_scores, norm_name, opponent_allowed, ratio_allowed  # noqa: E402

ROOT = Path(__file__).parent.parent
PARLAYS = ROOT / "paper" / "parlays.csv"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
START_OFFSET = timedelta(hours=3)   # occurrence_datetime is the start + 3h

# Kalshi series -> (scanner stat key, nflverse stat series for research)
LEG_SERIES = {
    "KXNFLPASSYDS": ("pass_yds", "KXNFLPASSYDS"),
    "KXNFLRECYDS": ("reception_yds", "KXNFLRECYDS"),
    "KXNFLREC": ("receptions", "KXNFLREC"),
    "KXNFLRSHYDS": ("rush_yds", "KXNFLRSHYDS"),
    "KXNFLPASSTDS": ("pass_tds", "KXNFLPASSTDS"),
    "KXNFLTD": ("anytime_td", "KXNFLANYTD"),
}
LEG_FAIR = (0.40, 0.85)     # legs that are likely enough to stack
LEG_MAX_GAP = 0.02          # Kalshi ask may be at most this far above book fair
PARLAY_FAIR = (0.05, 0.25)  # about +300 to +1900
BIG_SWING_FAIR = (0.02, 0.06)   # one long shot a week: about +1600 to +5000
# Kalshi combo quotes vs the product of the legs' asks. Live RFQ quotes on
# 2026-10-29 (1-2 makers answering): ~1.5x across games, ~2.2x within one
# game. Historical first trades suggested 1.10x/1.21x; trust the live quotes.
MARKUP = 1.45
SAME_GAME_MARKUP = 2.2
TARGET_EDGE = 1.05          # max price leaves 5% edge vs fair
LIFT_SHRINK = 0.5           # count half of each measured same-game lift

# Same-game pair lifts: how much more often both hit than priced independently
# (dashboard/correlations.csv from the 2025-27 backtest)
RECEIVING = {"KXNFLRECYDS", "KXNFLREC"}


def pair_lift(a: dict, b: dict) -> float:
    if a["game"] != b["game"]:
        return 1.0
    same = a["team"] == b["team"]
    kinds = {a["series"], b["series"]}
    if "KXNFLPASSYDS" in kinds and kinds & RECEIVING:
        return 1.356 if same else 1.113
    if "KXNFLPASSTDS" in kinds and "KXNFLTD" in kinds and same:
        return 1.291
    if kinds <= RECEIVING:
        return 1.017 if same else 1.051
    if "KXNFLRSHYDS" in kinds and kinds & RECEIVING and same:
        return 0.921
    if kinds == {"KXNFLTD"} and same:
        return 0.868
    return 1.0


def is_stack(a: dict, b: dict) -> bool:
    return (a["game"] == b["game"] and a["team"] == b["team"]
            and {a["series"], b["series"]} & {"KXNFLPASSYDS"} and {a["series"], b["series"]} & RECEIVING)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def get(path: str) -> dict:
    req = Request(f"{KALSHI_BASE}{path}", headers={"User-Agent": "hedj-parlays/1.0"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

class Research:
    def __init__(self):
        _, by_player = load_box_scores()
        self.hist, self.league = opponent_allowed(by_player)
        # Most recent row per player name this season, plus their game log
        self.players = {}
        for rows in by_player.values():
            rows = [r for r in rows if r["season_type"] == "REG"]
            if rows:
                self.players[norm_name(rows[-1]["player_display_name"])] = rows
        self.snaps = defaultdict(list)
        for r in csv.DictReader(open(NFLVERSE / "snap_counts_2026.csv")):
            self.snaps[norm_name(r["player"])].append((int(r["week"]), _f(r["offense_pct"])))
        self.injuries = {}
        for r in csv.DictReader(open(NFLVERSE / "injuries_2026.csv")):
            key = norm_name(r["full_name"])
            if int(r["week"]) >= self.injuries.get(key, (0, ""))[0]:
                self.injuries[key] = (int(r["week"]), r["report_status"])
        self.games = [g for g in csv.DictReader(open(NFLVERSE / "games.csv")) if g["season"] == "2026"]

    def game_env(self, team: str, day: str):
        for g in self.games:
            if g["gameday"] == day and team in (g["home_team"], g["away_team"]):
                total, spread = _f(g["total_line"]), _f(g["spread_line"])   # spread > 0: home favored
                home = team == g["home_team"]
                implied = total / 2 + (spread / 2 if home else -spread / 2)
                return {"week": int(g["week"]), "opp": g["away_team"] if home else g["home_team"],
                        "implied": implied, "roof": g["roof"]}
        return None

    def leg(self, name: str, series: str, line: float, day: str) -> dict:
        rows = self.players.get(name)
        if not rows:
            return {}
        last = rows[-1]
        stat = STATS[LEG_SERIES[series][1]]
        recent = [stat(r) for r in rows if r["season"] == "2026"][-8:] or [stat(r) for r in rows][-8:]
        env = self.game_env(last["team"], day) or {}
        out = {
            "team": last["team"], "pos": last["position"],
            "hit_l8": sum(v > line for v in recent) / len(recent),
            "avg_l8": statistics.mean(recent), "avg_l3": statistics.mean(recent[-3:]),
            "snap_l3": statistics.mean([p for _, p in sorted(self.snaps.get(name, []))[-3:]] or [0]),
            "implied": env.get("implied"), "roof": env.get("roof"),
            "injury": self.injuries.get(name, (0, ""))[1],
        }
        if env:
            out["opp"] = env["opp"]
            out["opp_ratio"] = ratio_allowed(self.hist, self.league, "2026", env["opp"], LEG_SERIES[series][1],
                                             last["position_group"], env["week"])
        return out


def research_points(r: dict, line: float) -> tuple:
    """Small tiebreak score (points of probability) and the reasons behind it"""
    pts, why = 0.0, []
    if r["hit_l8"] >= 0.625:
        pts += 1.5
        why.append(f"cleared {r['hit_l8']:.0%} of last 8")
    elif r["hit_l8"] <= 0.25:
        pts -= 1.5
        why.append(f"cleared only {r['hit_l8']:.0%} of last 8")
    if r["avg_l3"] > line * 1.15:
        pts += 1
        why.append(f"last 3 avg {r['avg_l3']:.1f}")
    ratio = r.get("opp_ratio")
    if ratio and ratio >= 1.10:
        pts += 1
        why.append(f"{r['opp']} allows {ratio:.0%} of avg")
    elif ratio and ratio <= 0.90:
        pts -= 1
        why.append(f"{r['opp']} allows {ratio:.0%} of avg")
    if r.get("implied") and r["implied"] >= 25:
        pts += 0.5
        why.append(f"team total {r['implied']:.1f}")
    if r["injury"] == "Questionable":
        pts -= 2
        why.append("questionable")
    return pts, why

# ---------------------------------------------------------------------------
# Legs + parlays
# ---------------------------------------------------------------------------

def load_legs(days: int, use_cache: bool, research: Research) -> list:
    cfg = scanner.SPORTS["nfl"]
    events = scanner.fetch_events(scanner.load_api_key(), cfg["odds_sport"], days * 24)
    keys = sorted({k for _, ks in cfg["stats"].values() for k in ks})
    ladders = defaultdict(dict)
    api_key = scanner.load_api_key()
    for e in events:
        data = scanner.fetch_event_props(api_key, cfg["odds_sport"], e["id"], keys, use_cache)
        for stat, (_, ks) in cfg["stats"].items():
            for player, ladder in scanner.build_fair_ladders(data, ks).items():
                ladders[player][stat] = ladder
    print(f"{len(events)} games, book ladders for {len(ladders)} players", file=sys.stderr)

    now, legs = datetime.now(timezone.utc), []
    for series, (stat, _) in LEG_SERIES.items():
        for m in scanner.fetch_kalshi_open(series):
            occ = m.get("occurrence_datetime") or m.get("expected_expiration_time")
            if not occ:
                continue
            start = datetime.fromisoformat(occ.replace("Z", "+00:00")) - START_OFFSET
            if not now + timedelta(minutes=30) < start <= now + timedelta(days=days):
                continue
            name = norm_name(m.get("title", "").split(":")[0])
            # Anytime TD is "1+ touchdowns" (line 0.5); books don't price the 2+/3+ markets
            line = scanner.kalshi_line(m) or 0.5 if series == "KXNFLTD" else scanner.kalshi_line(m)
            ladder = ladders.get(name, {}).get(stat)
            fair = scanner.fair_at(ladder, line) if ladder and line is not None else None
            ask = _f(m.get("yes_ask_dollars"))
            if fair is None or not 0 < ask < 1 or not LEG_FAIR[0] <= fair <= LEG_FAIR[1] or ask > fair + LEG_MAX_GAP:
                continue
            r = research.leg(name, series, line, (start - timedelta(hours=6)).date().isoformat())
            if not r or r["injury"] in ("Out", "Doubtful"):
                continue
            pts, why = research_points(r, line)
            legs.append({
                "ticker": m["ticker"], "title": m.get("title", ""), "series": series, "player": name,
                "team": r["team"], "game": m["event_ticker"].split("-")[1], "start": start,
                "line": line, "ask": ask, "fair": fair, "score": (fair - ask) * 100 + pts,
                "why": "; ".join([f"books {fair:.0%} vs Kalshi {ask:.0%}"] + why),
            })
    # One rung per player and stat: the best-scoring line
    best = {}
    for leg in legs:
        k = (leg["player"], leg["series"])
        if k not in best or leg["score"] > best[k]["score"]:
            best[k] = leg
    return sorted(best.values(), key=lambda x: -x["score"])


def price_parlay(legs: tuple) -> dict:
    fair = 1.0
    for leg in legs:
        fair *= leg["fair"]
    # Pair lifts don't multiply across a 3-4 leg cluster, and they were measured
    # against Kalshi prices that may have been stale: per game, use only the
    # strongest pair and count half of it
    for game in {l["game"] for l in legs}:
        same = [l for l in legs if l["game"] == game]
        lifts = [pair_lift(a, b) for a, b in itertools.combinations(same, 2)]
        if lifts:
            lift = max(lifts) if min(lifts) >= 1 else min(lifts)
            fair *= 1 + (lift - 1) * LIFT_SHRINK
    ask = 1.0
    for leg in legs:
        ask *= leg["ask"]
    stack = any(is_stack(a, b) for a, b in itertools.combinations(legs, 2))
    same_game = len({l["game"] for l in legs}) < len(legs)
    est = min(ask * (SAME_GAME_MARKUP if same_game else MARKUP), 0.99)
    return {"fair": fair, "est_price": est, "ev": fair / est - 1, "max_price": fair / TARGET_EDGE, "stack": stack}


def candidates(pool: list, sizes: tuple, fair_range: tuple) -> list:
    out = []
    for k in sizes:
        for combo in itertools.combinations(pool, k):
            if any(pair_lift(a, b) < 1 for a, b in itertools.combinations(combo, 2)):
                continue
            if len({l["player"] for l in combo}) < k:
                continue
            p = price_parlay(combo)
            if fair_range[0] <= p["fair"] <= fair_range[1] and p["ev"] > 0:
                out.append((p["ev"] + sum(l["score"] for l in combo) / 100, combo, p))
    return sorted(out, key=lambda c: -c[0])


def build_card(legs: list, n_parlays: int, top: int, big_swing: bool) -> list:
    """n_parlays regular parlays (no shared legs), then optionally one 4-5 leg long shot"""
    card, used = [], set()
    for _, combo, p in candidates(legs[:top], (3, 4), PARLAY_FAIR):
        if any(l["ticker"] in used for l in combo):
            continue
        card.append((combo, {**p, "big": False}))
        used |= {l["ticker"] for l in combo}
        if len(card) == n_parlays:
            break
    if big_swing:
        # Fewer legs to search: 5-leg combos of the top 35 would be 300K+
        for _, combo, p in candidates(legs[:22], (4, 5), BIG_SWING_FAIR):
            card.append((combo, {**p, "big": True}))
            break
    return card


def print_card(card: list, stake: float):
    for i, (combo, p) in enumerate(card, 1):
        pays = 1 / p["max_price"] - 1
        print(f"\n{'BIG SWING' if p['big'] else 'Parlay'} {i}: {len(combo)} legs, fair {p['fair']:.1%} (+{(1 / p['fair'] - 1) * 100:.0f}),"
              f" est. Kalshi price {p['est_price'] * 100:.1f}¢ -> EV {p['ev']:+.0%}"
              f"{' [QB stack]' if p['stack'] else ''}")
        print(f"  BUY ONLY AT {p['max_price'] * 100:.1f}¢ OR LESS (pays {pays:.1f}x)  stake ${stake:.0f}")
        for l in combo:
            print(f"  - {l['title']}  [{l['start']:%a %-I:%M%p} UTC]\n      {l['why']}")

# ---------------------------------------------------------------------------
# Log + settle
# ---------------------------------------------------------------------------

FIELDS = ["built_at", "parlay_id", "kind", "status", "legs", "parlay_fair", "est_price", "max_price", "stake",
          "ticker", "title", "game_start", "line", "leg_ask", "leg_fair", "why", "leg_result", "settled_at",
          # Filled in by place_parlays.py when the parlay is actually bought
          "combo_ticker", "paid_price", "contracts", "placed_at"]


def load_rows() -> list:
    return list(csv.DictReader(open(PARLAYS))) if PARLAYS.exists() else []


def save_rows(rows: list):
    PARLAYS.parent.mkdir(parents=True, exist_ok=True)
    with open(PARLAYS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n", restval="")
        w.writeheader()
        w.writerows(rows)


def log_card(card: list, stake: float):
    rows, now = load_rows(), datetime.now(timezone.utc)
    for i, (combo, p) in enumerate(card, 1):
        pid = f"{now:%Y%m%d}-{i}"
        for l in combo:
            rows.append({
                "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "parlay_id": pid,
                "kind": "big swing" if p["big"] else "regular", "status": "open",
                "legs": len(combo), "parlay_fair": f"{p['fair']:.4f}", "est_price": f"{p['est_price']:.4f}",
                "max_price": f"{p['max_price']:.4f}", "stake": f"{stake:.2f}", "ticker": l["ticker"],
                "title": l["title"], "game_start": l["start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "line": l["line"], "leg_ask": f"{l['ask']:.2f}", "leg_fair": f"{l['fair']:.4f}",
                "why": l["why"], "leg_result": "", "settled_at": "",
            })
    save_rows(rows)
    print(f"\nLogged {len(card)} parlays to {PARLAYS.relative_to(ROOT)}")


def settle() -> int:
    rows, now, done = load_rows(), datetime.now(timezone.utc), 0
    for r in rows:
        if r["leg_result"]:
            continue
        m = get(f"/markets/{r['ticker']}").get("market", {})
        if m.get("result") in ("yes", "no"):
            r["leg_result"], r["settled_at"] = m["result"], now.strftime("%Y-%m-%dT%H:%M:%SZ")
            done += 1
    by_parlay = defaultdict(list)
    for r in rows:
        by_parlay[r["parlay_id"]].append(r)
    for legs in by_parlay.values():
        results = [r["leg_result"] for r in legs]
        status = "lost" if "no" in results else "won" if all(x == "yes" for x in results) else "open"
        for r in legs:
            r["status"] = status
    save_rows(rows)
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="games starting within this many days")
    ap.add_argument("--parlays", type=int, default=3, help="regular parlays (plus one big swing)")
    ap.add_argument("--no-big-swing", action="store_true")
    ap.add_argument("--budget", type=float, default=20)
    ap.add_argument("--top", type=int, default=35, help="best legs to combine")
    ap.add_argument("--use-cache", action="store_true", help="reuse the last odds pull (0 credits)")
    ap.add_argument("--dry-run", action="store_true", help="print the card without logging it")
    ap.add_argument("--settle", action="store_true")
    args = ap.parse_args()

    if args.settle:
        print(f"{settle()} legs settled")
        return
    legs = load_legs(args.days, args.use_cache, Research())
    print(f"{len(legs)} qualifying legs", file=sys.stderr)
    card = build_card(legs, args.parlays, args.top, not args.no_big_swing)
    if not card:
        print("No parlays meet the rules this week: pass")
        return
    # Fixed slice of the weekly budget; a thin week spends less instead of betting more per parlay
    stake = max(1, round(args.budget / (args.parlays + (0 if args.no_big_swing else 1))))
    print_card(card, stake)
    if not args.dry_run:
        log_card(card, stake)


if __name__ == "__main__":
    main()
