#!/usr/bin/env python3
"""
Tight Line Summary - Morning Alert for Pre-Arb Candidates

Identifies games with tight lines (close to arbitrage) that are worth monitoring.
These are games where the implied probability sum is between 100% and 102%.
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

# Illinois-legal books
ILLINOIS_BOOKS = {
    "fanduel", "draftkings", "betmgm", "caesars",
    "pointsbetus", "betrivers", "espnbet"
}

# Tight line threshold (100% = true arb, 102% = close to arb)
MAX_IMPLIED_PROB = 1.02  # 102%

# Maximum age of odds to consider (in hours)
MAX_ODDS_AGE_HOURS = 1.5

# Discord webhook URL from environment
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Sportsbook links
SPORTSBOOK_LINKS = {
    "fanduel": "https://sportsbook.fanduel.com",
    "draftkings": "https://sportsbook.draftkings.com",
    "betmgm": "https://sports.betmgm.com",
    "caesars": "https://sportsbook.caesars.com",
    "pointsbetus": "https://pointsbet.com",
    "betrivers": "https://il.betrivers.com",
    "espnbet": "https://espnbet.com",
}


def load_latest_odds(csv_path: str) -> List[Dict[str, Any]]:
    """Load the latest odds from CSV."""
    odds = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            odds.append(row)
    return odds


def american_to_decimal(american: int) -> float:
    """Convert American odds to decimal odds."""
    if american >= 100:
        return 1 + american / 100
    elif american <= -100:
        return 1 + 100 / abs(american)
    else:
        raise ValueError(f"Invalid American odds: {american}")


def is_valid_american_odds(odds: int) -> bool:
    """Check if American odds are valid."""
    return odds >= 100 or odds <= -100


def find_tight_lines(odds: List[Dict[str, Any]], allowed_books: set) -> List[Dict[str, Any]]:
    """Find games with tight lines (close to arbitrage)."""
    # Group odds by event and market
    events: Dict[str, Dict[str, Any]] = {}

    now = datetime.now(timezone.utc)
    tomorrow = now + timedelta(days=1)
    max_age = timedelta(hours=MAX_ODDS_AGE_HOURS)

    for row in odds:
        book = row['book'].lower()
        if book not in allowed_books:
            continue

        event_id = row['event_id']
        market = row['market_type']
        outcome = row['outcome']

        # Only look at games in the next 24 hours
        try:
            event_time = datetime.fromisoformat(row['event_start_time'].replace('+00:00', '+00:00'))
            if event_time < now or event_time > tomorrow:
                continue
        except:
            continue

        # Skip stale odds
        last_updated = row.get('last_updated', '')
        if last_updated:
            try:
                update_time = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                if now - update_time > max_age:
                    continue
            except:
                pass

        try:
            price = int(row['current_price'])
            if not is_valid_american_odds(price):
                continue
        except:
            continue

        if event_id not in events:
            events[event_id] = {
                'sport': row['sport'],
                'home_team': row['home_team'],
                'away_team': row['away_team'],
                'event_start_time': row['event_start_time'],
                'markets': {}
            }

        if market not in events[event_id]['markets']:
            events[event_id]['markets'][market] = {}

        if outcome not in events[event_id]['markets'][market]:
            events[event_id]['markets'][market][outcome] = []

        events[event_id]['markets'][market][outcome].append({
            'book': book,
            'price': price,
            'line': row.get('current_line'),
        })

    # Find tight lines
    tight_lines = []

    for event_id, event in events.items():
        for market, outcomes in event['markets'].items():
            # Two-way markets
            if market == 'moneyline':
                result = check_tight_line(
                    outcomes.get('home', []),
                    outcomes.get('away', []),
                    event,
                    market,
                    event_id
                )
                if result:
                    tight_lines.append(result)

            elif market == 'total':
                # Group by line value
                over_by_line: Dict[str, List] = {}
                under_by_line: Dict[str, List] = {}

                for o in outcomes.get('over', []):
                    line = o.get('line') or '0'
                    if line not in over_by_line:
                        over_by_line[line] = []
                    over_by_line[line].append(o)

                for o in outcomes.get('under', []):
                    line = o.get('line') or '0'
                    if line not in under_by_line:
                        under_by_line[line] = []
                    under_by_line[line].append(o)

                # Check each line
                for line, overs in over_by_line.items():
                    unders = under_by_line.get(line, [])
                    if unders:
                        result = check_tight_line(
                            overs, unders, event, market, event_id,
                            side1_label=f"Over {line}", side2_label=f"Under {line}"
                        )
                        if result:
                            tight_lines.append(result)

    return sorted(tight_lines, key=lambda x: x['implied_prob_sum'])


def check_tight_line(
    side1_odds: List[Dict],
    side2_odds: List[Dict],
    event: Dict,
    market: str,
    event_id: str,
    side1_label: Optional[str] = None,
    side2_label: Optional[str] = None
) -> Optional[Dict]:
    """Check if a market has tight lines (close to arb)."""
    if not side1_odds or not side2_odds:
        return None

    # Find best odds for each side
    best1 = max(side1_odds, key=lambda x: x['price'])
    best2 = max(side2_odds, key=lambda x: x['price'])

    try:
        dec1 = american_to_decimal(best1['price'])
        dec2 = american_to_decimal(best2['price'])
    except:
        return None

    implied_sum = (1/dec1) + (1/dec2)

    # Already an arb or too far from arb
    if implied_sum < 1.0 or implied_sum > MAX_IMPLIED_PROB:
        return None

    # Calculate how close to arb (1.0 = true arb)
    gap_to_arb = (implied_sum - 1.0) * 100  # Percentage points away

    return {
        'event_id': event_id,
        'sport': event['sport'],
        'home_team': event['home_team'],
        'away_team': event['away_team'],
        'event_start_time': event['event_start_time'],
        'market': market,
        'implied_prob_sum': round(implied_sum, 4),
        'gap_to_arb': round(gap_to_arb, 2),
        'legs': [
            {
                'side': side1_label or event['home_team'],
                'book': best1['book'],
                'odds': best1['price'],
            },
            {
                'side': side2_label or event['away_team'],
                'book': best2['book'],
                'odds': best2['price'],
            }
        ]
    }


def format_discord_message(tight_lines: List[Dict]) -> Dict:
    """Format tight lines as a Discord webhook message."""
    if not tight_lines:
        return None

    # Group by sport
    by_sport: Dict[str, List[Dict]] = {}
    for tl in tight_lines:
        sport = tl['sport']
        if sport not in by_sport:
            by_sport[sport] = []
        by_sport[sport].append(tl)

    embeds = []
    total_count = len(tight_lines)

    for sport, games in by_sport.items():
        # Format sport name
        sport_display = {
            'americanfootball_nfl': 'NFL',
            'basketball_nba': 'NBA',
            'basketball_ncaab': 'NCAAB',
            'icehockey_nhl': 'NHL',
            'mma_mixed_martial_arts': 'MMA',
        }.get(sport, sport.upper())

        fields = []

        for game in games[:6]:  # Limit per sport
            leg1 = game['legs'][0]
            leg2 = game['legs'][1]

            def fmt_odds(odds):
                return f"+{odds}" if odds > 0 else str(odds)

            # Get sportsbook links
            link1 = SPORTSBOOK_LINKS.get(leg1['book'].lower(), "")
            link2 = SPORTSBOOK_LINKS.get(leg2['book'].lower(), "")

            book1 = f"[{leg1['book'].upper()}]({link1})" if link1 else leg1['book'].upper()
            book2 = f"[{leg2['book'].upper()}]({link2})" if link2 else leg2['book'].upper()

            # Format game time
            try:
                game_time = datetime.fromisoformat(game['event_start_time'].replace('+00:00', '+00:00'))
                time_str = game_time.strftime("%I:%M %p")
            except:
                time_str = "TBD"

            gap_emoji = "🔥" if game['gap_to_arb'] < 0.5 else "👀" if game['gap_to_arb'] < 1.0 else "📊"

            fields.append({
                "name": f"{gap_emoji} {game['away_team']} @ {game['home_team']} ({time_str})",
                "value": (
                    f"**{game['market'].title()}** • {game['gap_to_arb']:.1f}% from arb\n"
                    f"{leg1['side']}: {book1} @ {fmt_odds(leg1['odds'])}\n"
                    f"{leg2['side']}: {book2} @ {fmt_odds(leg2['odds'])}"
                ),
                "inline": False
            })

        embed = {
            "title": f"🏀 {sport_display} - {len(games)} Tight Line{'s' if len(games) != 1 else ''}",
            "color": 0xFFAA00,  # Orange
            "fields": fields,
        }
        embeds.append(embed)

    return {
        "content": (
            f"**☀️ Morning Summary: {total_count} Tight Line{'s' if total_count != 1 else ''} Today**\n"
            f"These games are close to arbitrage opportunities. Watch for line movement!"
        ),
        "embeds": embeds[:4]  # Discord limit
    }


def send_discord_alert(message: Dict) -> bool:
    """Send alert to Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        print("No Discord webhook URL configured")
        return False

    try:
        data = json.dumps(message).encode('utf-8')
        req = Request(
            DISCORD_WEBHOOK_URL,
            data=data,
            headers={'Content-Type': 'application/json'}
        )
        with urlopen(req, timeout=10) as response:
            return response.status == 204
    except URLError as e:
        print(f"Failed to send Discord alert: {e}")
        return False


def main():
    # Find the data file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    csv_path = os.path.join(repo_root, "data", "latest", "latest.csv")

    if not os.path.exists(csv_path):
        print(f"Data file not found: {csv_path}")
        return 1

    print(f"Loading odds from {csv_path}")
    odds = load_latest_odds(csv_path)
    print(f"Loaded {len(odds)} odds records")

    print(f"Finding tight lines (<{MAX_IMPLIED_PROB * 100}% implied prob)")
    tight_lines = find_tight_lines(odds, ILLINOIS_BOOKS)

    print(f"Found {len(tight_lines)} tight line opportunities")

    if tight_lines:
        for tl in tight_lines:
            print(f"  {tl['gap_to_arb']:.1f}% gap - {tl['away_team']} @ {tl['home_team']} ({tl['market']})")
            for leg in tl['legs']:
                print(f"    {leg['side']} @ {leg['book'].upper()}: {leg['odds']}")

        message = format_discord_message(tight_lines)
        if message and DISCORD_WEBHOOK_URL:
            print("Sending Discord alert...")
            if send_discord_alert(message):
                print("Alert sent successfully!")
            else:
                print("Failed to send alert")
    else:
        print("No tight lines found - odds may have shifted")
        # Still send a summary even if empty
        if DISCORD_WEBHOOK_URL:
            empty_message = {
                "content": "**☀️ Morning Summary: No Tight Lines Today**\nNo games are close to arbitrage right now. Check back later!"
            }
            send_discord_alert(empty_message)

    return 0


if __name__ == "__main__":
    sys.exit(main())
