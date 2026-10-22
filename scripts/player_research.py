#!/usr/bin/env python3
"""
NFL player-prop research: which pregame player/matchup factors does Kalshi
underprice?

Joins every settled Kalshi NFL player-prop snapshot (from prop_calibration.py)
to nflverse box scores, schedules and snap counts, then builds pregame-only
features for each market:

    usage       player's average of the stat over his last 8 games, vs the line
    trend       last 3 games vs last 8 (heating up / cooling off)
    snap_pct    share of offensive snaps over his last 8 games
    opp         what this defense allowed to the position so far this season,
                relative to league average
    team_total  Vegas implied points for the player's team
    home, dome, wind, div_game
    hype        open interest on the Kalshi market before kickoff

For each factor it reports, per season, how often YES hit versus the price
("hit-vs-price", in points) and the return from buying cheap overs. A factor
only counts if it points the same way in both seasons. Then it fits a logistic
model of the outcome on the market price plus the factors, trained on 2025-26
and tested on 2026-27, and simulates betting where the model disagrees with
the price by more than fees.

Inputs (download with --download):
    data/prediction_markets/nfl_v2_snapshots.json     prop_calibration.py --out
    data/prediction_markets/nflverse/*.csv            nflverse-data releases

    python scripts/player_research.py --download
    python scripts/player_research.py
"""

import argparse
import csv
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import urlretrieve

DATA = Path(__file__).parent.parent / "data" / "prediction_markets"
NFLVERSE = DATA / "nflverse"
NFLVERSE_URL = "https://github.com/nflverse/nflverse-data/releases/download"
NFLVERSE_FILES = [
    "stats_player/stats_player_week_2025.csv", "stats_player/stats_player_week_2026.csv",
    "schedules/games.csv",
    "snap_counts/snap_counts_2025.csv", "snap_counts/snap_counts_2026.csv",
]
SEASONS = ("2025", "2026")
TAKER_COEFF = 0.07
MIN_EDGE = 0.03          # model must beat the price by this much after fees to bet

# Kalshi series -> nflverse stat (a function of the box-score row)
STATS = {
    "KXNFLREC": lambda r: _n(r["receptions"]),
    "KXNFLRECYDS": lambda r: _n(r["receiving_yards"]),
    "KXNFLRSHYDS": lambda r: _n(r["rushing_yards"]),
    "KXNFLPASSYDS": lambda r: _n(r["passing_yards"]),
    "KXNFLPASSTDS": lambda r: _n(r["passing_tds"]),
    "KXNFLANYTD": lambda r: _n(r["rushing_tds"]) + _n(r["receiving_tds"]),
    "KXNFL2TD": lambda r: _n(r["rushing_tds"]) + _n(r["receiving_tds"]),
}
FIXED_LINE = {"KXNFLANYTD": 1, "KXNFL2TD": 2}

# Kalshi uses a few different team codes from nflverse
TEAM_ALIASES = {"JAX": ["JAX", "JAC"], "LA": ["LA", "LAR"], "WAS": ["WAS", "WSH"]}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def _n(v) -> float:
    try:
        return float(v) if v not in (None, "", "NA") else 0.0
    except ValueError:
        return 0.0


def norm_name(name: str) -> str:
    name = re.sub(r"[^a-z ]", "", name.lower().replace("-", " "))
    return " ".join(w for w in name.split() if w not in ("jr", "sr", "ii", "iii", "iv", "v"))


def taker_fee(p: float) -> float:
    return TAKER_COEFF * p * (1 - p)


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))

# ---------------------------------------------------------------------------
# Load + join
# ---------------------------------------------------------------------------

def download():
    NFLVERSE.mkdir(parents=True, exist_ok=True)
    for f in NFLVERSE_FILES:
        print(f"  {f}")
        urlretrieve(f"{NFLVERSE_URL}/{f}", NFLVERSE / Path(f).name)


def load_games() -> Dict[tuple, dict]:
    """(gameday, kalshi game code) -> schedule row."""
    games = {}
    for g in csv.DictReader(open(NFLVERSE / "games.csv")):
        if g["season"] not in SEASONS:
            continue
        for a in TEAM_ALIASES.get(g["away_team"], [g["away_team"]]):
            for h in TEAM_ALIASES.get(g["home_team"], [g["home_team"]]):
                games[(g["gameday"], a + h)] = g
    return games


def load_box_scores():
    """Box-score rows by (game_id, normalized name), and each player's games in order."""
    by_game, by_player = {}, defaultdict(list)
    for season in SEASONS:
        for r in csv.DictReader(open(NFLVERSE / f"stats_player_week_{season}.csv")):
            by_game[(r["game_id"], norm_name(r["player_display_name"]))] = r
            by_player[r["player_id"]].append(r)
    for rows in by_player.values():
        rows.sort(key=lambda r: (int(r["season"]), int(r["week"])))
    return by_game, by_player


def load_snaps() -> Dict[tuple, float]:
    snaps = {}
    for season in SEASONS:
        for r in csv.DictReader(open(NFLVERSE / f"snap_counts_{season}.csv")):
            snaps[(r["game_id"], norm_name(r["player"]))] = _n(r["offense_pct"])
    return snaps


def opponent_allowed(by_player) -> Dict[tuple, Dict[str, float]]:
    """(season, defense, week) -> {series|position: average allowed per game in earlier weeks}."""
    per_game = defaultdict(float)          # (season, week, defense, series, pos) -> total
    for rows in by_player.values():
        for r in rows:
            for series, stat in STATS.items():
                per_game[(r["season"], int(r["week"]), r["opponent_team"], series, r["position_group"])] += stat(r)
    hist = defaultdict(list)               # (season, defense, series, pos) -> [(week, total)]
    league = defaultdict(list)             # (season, series, pos) -> [(week, total)]
    for (season, week, d, series, pos), v in per_game.items():
        hist[(season, d, series, pos)].append((week, v))
        league[(season, series, pos)].append((week, v))
    return hist, league


def ratio_allowed(hist, league, season, defense, series, pos, week) -> Optional[float]:
    prior = [v for w, v in hist.get((season, defense, series, pos), []) if w < week]
    lg = [v for w, v in league.get((season, series, pos), []) if w < week]
    if len(prior) < 3 or not lg or statistics.mean(lg) == 0:
        return None
    return statistics.mean(prior) / statistics.mean(lg)


def parse_title(title: str) -> tuple:
    """Player name and prop text. Kalshi has used three title formats:
    'Name: 60+ receiving yards', 'Name records 60+ receiving yards',
    'Away at Home: Two or More Touchdowns Scorer: Name'."""
    if "Scorer:" in title:
        return title.rsplit("Scorer:", 1)[1].strip(), title
    if " records " in title:
        name, _, prop = title.partition(" records ")
        return name.strip(), prop
    name, _, prop = title.partition(":")
    return name.strip(), prop


def build_rows(snapshots: List[dict]) -> List[dict]:
    games = load_games()
    by_game, by_player = load_box_scores()
    snaps = load_snaps()
    hist, league = opponent_allowed(by_player)

    rows, misses = [], defaultdict(int)
    for s in snapshots:
        if s["series"] not in STATS:
            continue
        parts = s["ticker"].split("-")
        d = parts[1][:7]
        gameday = f"20{d[:2]}-{MONTHS[d[2:5]]:02d}-{d[5:7]}"
        game = games.get((gameday, parts[1][7:]))
        if not game:
            misses["game"] += 1
            continue
        name, prop = parse_title(s["title"])
        box = by_game.get((game["game_id"], norm_name(name)))
        if not box:
            misses["player"] += 1   # no box-score line: inactive or zero touches
            continue
        m = re.search(r"(\d+)\+", prop)
        line = FIXED_LINE.get(s["series"]) or (int(m.group(1)) if m else None)
        if line is None:
            misses["line"] += 1
            continue

        stat = STATS[s["series"]]
        actual = stat(box)
        prior = [r for r in by_player[box["player_id"]]
                 if (int(r["season"]), int(r["week"])) < (int(box["season"]), int(box["week"]))][-8:]
        if len(prior) < 3:
            misses["history"] += 1
            continue
        vals = [stat(r) for r in prior]
        avg8, avg3 = statistics.mean(vals), statistics.mean(vals[-3:])
        sd = statistics.pstdev(vals) or 1.0
        prior_snaps = [snaps.get((r["game_id"], norm_name(r["player_display_name"]))) for r in prior]
        prior_snaps = [x for x in prior_snaps if x is not None]

        home = box["team"] == game["home_team"]
        spread, total = _n(game["spread_line"]), _n(game["total_line"])   # spread: + = home favored
        team_total = total / 2 + (spread / 2 if home else -spread / 2)
        opp = ratio_allowed(hist, league, box["season"], box["opponent_team"],
                            s["series"], box["position_group"], int(box["week"]))
        mid = (s["yes_bid"] + s["yes_ask"]) / 2
        rows.append({
            "ticker": s["ticker"], "series": s["series"], "player": name.strip(),
            "position": box["position_group"], "season": f"{box['season']}-{int(box['season'][2:]) + 1}",
            "week": int(box["week"]), "line": line, "actual": actual, "result": s["result"],
            "consistent": (actual >= line) == bool(s["result"]),
            "yes_bid": s["yes_bid"], "yes_ask": s["yes_ask"], "mid": mid,
            "usage": (avg8 - line) / sd,
            "trend": (avg3 - avg8) / sd,
            "snap_pct": statistics.mean(prior_snaps) if prior_snaps else None,
            "opp": opp,
            "team_total": team_total,
            "home": int(home),
            "dome": int(game["roof"] in ("dome", "closed")),
            "wind": _n(game["wind"]),
            "div_game": int(game["div_game"] == "1"),
            "hype": math.log1p(s.get("pregame_oi", 0)),
        })
    return rows, misses

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def cheap_overs_roi(rows) -> tuple:
    cheap = [r for r in rows if .03 <= r["mid"] < .30]
    cost = sum(r["yes_ask"] + taker_fee(r["yes_ask"]) for r in cheap)
    return len(cheap), (sum(r["result"] for r in cheap) - cost) / cost * 100 if cost else 0.0


def resid(rows) -> float:
    return sum(r["result"] - r["mid"] for r in rows) / len(rows) * 100 if rows else 0.0


FACTORS = ["usage", "trend", "snap_pct", "opp", "team_total", "home", "dome", "wind", "div_game", "hype"]


def factor_report(rows):
    print("\nEach factor split into low / mid / high thirds (0/1 factors: no / yes).")
    print("hit = how many points more often YES hit than the price said; ROI = buying YES on 3-30c overs.\n")
    print(f"{'factor':11s} {'group':6s} {'2025-26 hit':>12s} {'ROI':>8s} {'2026-27 hit':>12s} {'ROI':>8s}   stable?")
    for f in FACTORS:
        have = [r for r in rows if r[f] is not None]
        vals = sorted(r[f] for r in have)
        binary = set(vals) <= {0, 1}
        if binary:
            groups = [("no", lambda v: v == 0), ("yes", lambda v: v == 1)]
        else:
            a, b = vals[len(vals) // 3], vals[2 * len(vals) // 3]
            groups = [("low", lambda v, a=a: v < a), ("mid", lambda v, a=a, b=b: a <= v < b), ("high", lambda v, b=b: v >= b)]
        hits = {}
        for label, test in groups:
            cells = []
            for season in ("2025-26", "2026-27"):
                sub = [r for r in have if r["season"] == season and test(r[f])]
                n, roi = cheap_overs_roi(sub)
                hits[(label, season)] = resid(sub)
                cells.append(f"{resid(sub):+8.1f} pts {roi:+7.1f}%")
            print(f"{f:11s} {label:6s} {'  '.join(cells)}")
        lo, hi = groups[0][0], groups[-1][0]
        spreads = [hits[(hi, s)] - hits[(lo, s)] for s in ("2025-26", "2026-27")]
        stable = spreads[0] * spreads[1] > 0 and min(abs(x) for x in spreads) >= 1.0
        print(f"{'':11s} {'':6s} high-minus-low: {spreads[0]:+.1f} / {spreads[1]:+.1f} pts   {'YES' if stable else 'no'}\n")


def fit_logistic(X: List[List[float]], y: List[int], iters: int = 400, lr: float = 0.5, l2: float = 1e-3):
    w = [0.0] * len(X[0])
    n = len(X)
    for _ in range(iters):
        grad = [0.0] * len(w)
        for xi, yi in zip(X, y):
            z = sum(a * b for a, b in zip(w, xi))
            p = 1 / (1 + math.exp(-max(min(z, 30), -30)))
            for j, xj in enumerate(xi):
                grad[j] += (p - yi) * xj
        w = [wj - lr * (g / n + l2 * wj) for wj, g in zip(w, grad)]
    return w


def model_report(rows):
    feats = ["usage", "trend", "snap_pct", "opp", "team_total", "home", "dome", "wind", "div_game", "hype"]
    usable = [r for r in rows if all(r[f] is not None for f in feats)]
    train = [r for r in usable if r["season"] == "2025-26"]
    test = [r for r in usable if r["season"] == "2026-27"]
    mu = {f: statistics.mean(r[f] for r in train) for f in feats}
    sd = {f: statistics.pstdev(r[f] for r in train) or 1 for f in feats}

    def x(r, with_feats=True):
        base = [1.0, logit(r["mid"])]
        return base + ([(r[f] - mu[f]) / sd[f] for f in feats] if with_feats else [])

    w_mkt = fit_logistic([x(r, False) for r in train], [r["result"] for r in train])
    w_all = fit_logistic([x(r) for r in train], [r["result"] for r in train])

    def p(w, xi):
        return 1 / (1 + math.exp(-sum(a * b for a, b in zip(w, xi))))

    def logloss(rows, prob):
        return -statistics.mean(math.log(prob(r)) if r["result"] else math.log(1 - prob(r)) for r in rows)

    print(f"\nModel trained on {len(train):,} markets from 2025-26, tested on {len(test):,} from 2026-27")
    print("Weights (per 1 standard deviation; + means YES hits more than the price says):")
    for f, wj in zip(feats, w_all[2:]):
        print(f"  {f:11s} {wj:+.3f}")
    for name, prob in [("market price alone", lambda r: min(max(r["mid"], 1e-4), 1 - 1e-4)),
                       ("price, recalibrated", lambda r: p(w_mkt, x(r, False))),
                       ("price + player factors", lambda r: p(w_all, x(r)))]:
        print(f"  test log loss, {name:24s} {logloss(test, prob):.4f}")

    # Bet wherever the model beats the quote by MIN_EDGE after fees
    bets = []
    for r in test:
        q = p(w_all, x(r))
        yes_cost = r["yes_ask"] + taker_fee(r["yes_ask"])
        no_cost = (1 - r["yes_bid"]) + taker_fee(1 - r["yes_bid"])
        if q - yes_cost > MIN_EDGE:
            bets.append((r, "YES", yes_cost, r["result"]))
        elif (1 - q) - no_cost > MIN_EDGE:
            bets.append((r, "NO", no_cost, 1 - r["result"]))
    cost = sum(b[2] for b in bets)
    pnl = sum(b[3] - b[2] for b in bets)
    print(f"\nOut-of-sample bets (model edge > {MIN_EDGE*100:.0f}c after fees): {len(bets):,} of {len(test):,} markets")
    if bets:
        print(f"  ROI {pnl / cost * 100:+.1f}% on ${cost:,.0f} staked at $1/contract")
        for side in ("YES", "NO"):
            sub = [b for b in bets if b[1] == side]
            if sub:
                c = sum(b[2] for b in sub)
                print(f"  {side}: {len(sub):,} bets, ROI {sum(b[3] - b[2] for b in sub) / c * 100:+.1f}%")
        weeks = sorted({b[0]["week"] for b in bets})
        print("  by week: " + "  ".join(
            f"wk{wk} {sum(b[3] - b[2] for b in bets if b[0]['week'] == wk):+.0f}" for wk in weeks))
    return w_all, feats, mu, sd


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--download", action="store_true", help="refresh nflverse CSVs first")
    ap.add_argument("--snapshots", default="nfl_v2_snapshots.json")
    ap.add_argument("--out", default="nfl_player_joined.csv")
    args = ap.parse_args()

    if args.download:
        download()
    snapshots = json.load(open(DATA / args.snapshots))
    rows, misses = build_rows(snapshots)
    consistent = sum(r["consistent"] for r in rows)
    print(f"Joined {len(rows):,} player-prop markets to box scores "
          f"(skipped: {dict(misses)}); box score agrees with Kalshi's settlement on "
          f"{consistent / len(rows) * 100:.1f}%")
    rows = [r for r in rows if r["consistent"]]

    with open(DATA / args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    factor_report(rows)
    model_report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
