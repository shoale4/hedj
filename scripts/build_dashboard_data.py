#!/usr/bin/env python3
"""
Build the strategy dashboard's data files from the backtest snapshots.

Writes to data/prediction_markets/dashboard/ (and the same rows as JSON to
dashboard/data/board/ for hedj.app):
    strategies.csv     one row per strategy x month: bets, cost, profit
    correlations.csv   same-game leg pairs: how often both hit vs. priced
    factors.csv        player factors that held up in both seasons

Each strategy is a plain rule (market types, price range, side) applied to
every settled market at its pregame quote, with taker fees. The rules were
picked by looking at this same data, so the 2026-27 column is the closest
thing to an out-of-sample check until paper trading starts.

    python scripts/build_dashboard_data.py
"""

import csv
import itertools
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).parent.parent / "data" / "prediction_markets"
OUT = DATA / "dashboard"
SITE = Path(__file__).parent.parent / "dashboard" / "data" / "board"   # committed, read by hedj.app
TAKER_COEFF = 0.07

NFL_OVERS = {"KXNFLREC", "KXNFLRECYDS", "KXNFLPASSYDS"}
NBA_PROPS = {"KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT"}

# id, league, name, rule shown on the page, snapshot files, series, price range (mid), side, extra filter
STRATEGIES = [
    ("nfl_overs", "NFL", "Cheap player overs", "YES on receptions, receiving and passing yards priced 3–30¢",
     ["nfl_v2"], NFL_OVERS, (.03, .30), "yes", None),
    ("nfl_overs_quiet", "NFL", "Cheap overs, quiet markets", "Same, only markets in the lowest third of pregame open interest",
     ["nfl_v2"], NFL_OVERS, (.03, .30), "yes", "quiet"),
    ("nfl_rush_overs", "NFL", "Cheap rushing overs", "YES on rushing yards priced 3–30¢",
     ["nfl_v2"], {"KXNFLRSHYDS"}, (.03, .30), "yes", None),
    ("nfl_rec_no", "NFL", "Fade low reception lines", "NO on receptions priced 70–94¢ (lines like 2+ or 3+ catches)",
     ["nfl_v2"], {"KXNFLREC"}, (.70, .94), "no", None),
    ("nfl_tt_no", "NFL", "Fade low team-total lines", "NO on team totals priced 85–94¢",
     ["nfl_v2"], {"KXNFLTEAMTOTAL"}, (.85, .94), "no", None),
    ("nfl_td_no", "NFL", "Fade coin-flip TD scorers", "NO on anytime TD priced 40–60¢",
     ["nfl_v2", "nfl_td2026"], {"KXNFLANYTD", "KXNFLTD"}, (.40, .60), "no", None),
    ("nfl_spread_no", "NFL", "Fade spread favorites", "NO on spreads priced 60–80¢",
     ["nfl_v2"], {"KXNFLSPREAD"}, (.60, .80), "no", None),
    ("nfl_longshot_no", "NFL", "NO on long shots (parlay legs)", "NO on every NFL market priced under 15¢",
     ["nfl_v2", "nfl_td2026"], None, (0, .15), "no", None),
    ("nba_overs", "NBA", "Cheap player overs", "YES on points, rebounds, assists and threes priced 3–30¢",
     ["nba"], NBA_PROPS, (.03, .30), "yes", None),
    ("nba_lines", "NBA", "Game lines, favorites", "YES on moneyline, spread and total priced 60–85¢",
     ["nba"], {"KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL"}, (.60, .85), "yes", None),
    ("ncaaf_tt_overs", "College football", "Cheap team-total overs", "YES on team totals priced 3–30¢",
     ["ncaaf"], {"KXNCAAFTEAMTOTAL"}, (.03, .30), "yes", None),
    ("ncaaf_tt_no", "College football", "Fade low team-total lines", "NO on team totals priced 80–94¢",
     ["ncaaf"], {"KXNCAAFTEAMTOTAL"}, (.80, .94), "no", None),
    ("nhl_goal_overs", "NHL", "Anytime goal long shots", "YES on player goals priced 3–30¢",
     ["nhl"], {"KXNHLGOAL"}, (.03, .30), "yes", None),
    ("nhl_pts_overs", "NHL", "Cheap points and assists overs", "YES on player points and assists priced 3–30¢",
     ["nhl"], {"KXNHLPTS", "KXNHLAST"}, (.03, .30), "yes", None),
    ("nhl_lines", "NHL", "Game lines, favorites", "YES on moneyline, puck line and total priced 60–85¢",
     ["nhl"], {"KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL"}, (.60, .85), "yes", None),
    ("weather_longshot_no", "Weather", "NO on long-shot temperature brackets", "NO on daily-high brackets priced under 10¢ (7 cities)",
     ["weather"], None, (0, .10), "no", None),
    ("ncaaf_spread_dogs", "College football", "Spread underdogs", "YES on spreads priced 15–40¢",
     ["ncaaf"], {"KXNCAAFSPREAD"}, (.15, .40), "yes", None),
]

# Same-game pairs for parlays: (label, series a, series b, same team?)
PAIRS = [
    ("QB passing yards + his receiver's yards", "KXNFLPASSYDS", "KXNFLRECYDS", True),
    ("QB passing TDs + his teammate scores a TD", "KXNFLPASSTDS", "KXNFLANYTD", True),
    ("QB passing yards + opposing receiver's yards", "KXNFLPASSYDS", "KXNFLRECYDS", False),
    ("Two receivers, opposite teams", "KXNFLRECYDS", "KXNFLRECYDS", False),
    ("Two receivers, same team", "KXNFLRECYDS", "KXNFLRECYDS", True),
    ("QB passing yards + his RB's rushing yards", "KXNFLPASSYDS", "KXNFLRSHYDS", True),
    ("RB rushing yards + teammate receiver's yards", "KXNFLRSHYDS", "KXNFLRECYDS", True),
    ("Two TD scorers, same team", "KXNFLANYTD", "KXNFLANYTD", True),
]

# Player factors from player_research.py: (factor, what it means, column, high-is-better?)
FACTORS = [
    ("Quiet market", "Overs in markets with little pregame open interest hit more than priced", "hype", False),
    ("High implied team total", "Overs for players on teams Vegas expects to score a lot hit more than priced", "team_total", True),
    ("Home team", "Home players' overs hit slightly more than priced", "home", True),
    ("Cold streak", "Players coming off quiet games hit more than priced, hot streaks less (market chases form)", "trend", False),
]


# First regular-season day; earlier games are preseason and left out
SEASON_START = {
    "KXNBA": ("2025-10-21", "2026-10-20"),
    "KXNHL": ("2025-10-07", "2026-10-05"),
}


def preseason(s: dict) -> bool:
    for prefix, starts in SEASON_START.items():
        if s["series"].startswith(prefix):
            start = starts[1] if s["game_date"] >= "2026-08-01" else starts[0]
            return s["game_date"][5:7] in ("08", "09", "10") and s["game_date"] < start
    return False


def fee(p: float) -> float:
    return TAKER_COEFF * p * (1 - p)


def season_of(date: str) -> str:
    return "2026-27" if date >= "2026-08-01" else "2025-26"


def load(name: str) -> list:
    path = DATA / f"{name}_snapshots.json"
    return json.load(open(path)) if path.exists() else []


def quiet_cutoffs(snaps: list) -> dict:
    by_series = defaultdict(list)
    for s in snaps:
        by_series[s["series"]].append(s.get("pregame_oi", 0))
    return {k: statistics.quantiles(v, n=3)[0] for k, v in by_series.items() if len(v) >= 3}


def build_strategies():
    cache, quiet = {}, {}
    out = []
    for sid, league, name, rule, files, series, (lo, hi), side, extra in STRATEGIES:
        snaps = []
        for f in files:
            if f not in cache:
                cache[f] = load(f)
                quiet[f] = quiet_cutoffs(cache[f])
            snaps += [(f, s) for s in cache[f]]
        months = defaultdict(lambda: [0, 0.0, 0.0])
        for f, s in snaps:
            if (series and s["series"] not in series) or preseason(s):
                continue
            mid = (s["yes_bid"] + s["yes_ask"]) / 2
            if not lo <= mid < hi:
                continue
            if extra == "quiet" and s.get("pregame_oi", 0) >= quiet[f].get(s["series"], 0):
                continue
            price = s["yes_ask"] if side == "yes" else 1 - s["yes_bid"]
            won = s["result"] if side == "yes" else 1 - s["result"]
            cost = price + fee(price)
            m = months[s["game_date"][:7]]
            m[0] += 1
            m[1] += cost
            m[2] += won - cost
        for month, (n, cost, pnl) in sorted(months.items()):
            out.append({"strategy": sid, "league": league, "name": name, "rule": rule, "side": side.upper(),
                        "month": month, "season": season_of(month + "-01"), "bets": n,
                        "cost": round(cost, 2), "profit": round(pnl, 2)})
    return out


def build_correlations():
    path = DATA / "nfl_player_joined.csv"
    if not path.exists():
        return []
    rows = list(csv.DictReader(open(path)))
    best = {}
    for r in rows:
        r["mid"], r["result"] = float(r["mid"]), int(r["result"])
        pc, game = r["ticker"].split("-")[2], r["ticker"].split("-")[1]
        r["game"], r["team"] = game, next((pc[:k] for k in (3, 2) if game[7:].startswith(pc[:k]) or game[7:].endswith(pc[:k])), None)
        # One rung per player market: the one closest to a coin flip
        key = (game, r["player"], r["series"])
        if .2 <= r["mid"] <= .8 and (key not in best or abs(r["mid"] - .5) < abs(best[key]["mid"] - .5)):
            best[key] = r
    by_game = defaultdict(list)
    for r in best.values():
        by_game[r["game"]].append(r)
    out = []
    for label, a_ser, b_ser, same_team in PAIRS:
        pairs = []
        for legs in by_game.values():
            for a, b in itertools.combinations(legs, 2):
                if a["player"] == b["player"] or (a["team"] == b["team"]) != same_team:
                    continue
                if {a["series"], b["series"]} == {a_ser, b_ser} and (a_ser != b_ser or a["series"] == a_ser):
                    pairs.append((a, b))
        if not pairs:
            continue
        joint = statistics.mean(a["result"] * b["result"] for a, b in pairs)
        priced = statistics.mean(a["mid"] * b["mid"] for a, b in pairs)
        out.append({"pair": label, "pairs": len(pairs), "both_hit": round(joint, 4),
                    "priced": round(priced, 4), "ratio": round(joint / priced, 3)})
    return out


def build_factors():
    path = DATA / "nfl_player_joined.csv"
    if not path.exists():
        return []
    rows = list(csv.DictReader(open(path)))
    out = []
    for name, meaning, col, high_good in FACTORS:
        cells = {}
        vals = sorted(float(r[col]) for r in rows if r[col] not in ("", "None"))
        binary = set(vals) <= {0.0, 1.0}
        lo_cut, hi_cut = (0.5, 0.5) if binary else (vals[len(vals) // 3], vals[2 * len(vals) // 3])
        for season in ("2025-26", "2026-27"):
            sub = [r for r in rows if r["season"] == season and r[col] not in ("", "None")]
            def resid(rs):
                return sum(int(r["result"]) - float(r["mid"]) for r in rs) / len(rs) if rs else 0
            low = [r for r in sub if float(r[col]) < lo_cut]
            high = [r for r in sub if float(r[col]) >= hi_cut]
            good, bad = (high, low) if high_good else (low, high)
            cells[season] = round(resid(good) - resid(bad), 4)
        out.append({"factor": name, "meaning": meaning,
                    "edge_2025": cells["2025-26"], "edge_2026": cells["2026-27"]})
    return out


def write(name: str, rows: list):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"  {len(rows):5d} rows -> {OUT / name}")
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / name.replace(".csv", ".json")).write_text(json.dumps(rows, indent=1))


def main():
    write("strategies.csv", build_strategies())
    write("correlations.csv", build_correlations())
    write("factors.csv", build_factors())
    return 0


if __name__ == "__main__":
    sys.exit(main())
