#!/usr/bin/env python3
"""
Arbitrage Alert System

Checks for arbitrage opportunities in a specific state and sends Discord alerts.
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from urllib.request import Request, urlopen
from urllib.error import URLError

# Illinois-legal books (update as needed)
ILLINOIS_BOOKS = {
    "fanduel", "draftkings", "betmgm", "caesars",
    "pointsbetus", "betrivers", "espnbet"
}

# Sportsbook deep links (web URLs that open apps on mobile)
SPORTSBOOK_LINKS = {
    "fanduel": "https://sportsbook.fanduel.com",
    "draftkings": "https://sportsbook.draftkings.com",
    "betmgm": "https://sports.betmgm.com",
    "caesars": "https://sportsbook.caesars.com",
    "pointsbetus": "https://pointsbet.com",
    "betrivers": "https://il.betrivers.com",
    "espnbet": "https://espnbet.com",
    "bet365": "https://bet365.com",
    "betonlineag": "https://betonline.ag",
    "unibet": "https://unibet.com",
    "wynnbet": "https://wynnbet.com",
    "superbook": "https://superbook.com"
}

# Minimum ROI to alert on (percentage)
MIN_ROI_PERCENT = 0.1

# Maximum ROI (above this is likely bad data)
MAX_ROI_PERCENT = 15.0

# Maximum age of odds to consider (in hours)
# Data updates every ~40 min, so 1.5h covers ~2 update cycles
MAX_ODDS_AGE_HOURS = 1.5

# Discord webhook URL from environment
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# File to track already-alerted arbs (avoid spam)
ALERTED_ARBS_FILE = Path(__file__).parent.parent / "data" / "alerted_arbs.json"


def load_alerted_arbs() -> Dict[str, str]:
    """Load the set of arb keys we've already alerted on."""
    if not ALERTED_ARBS_FILE.exists():
        return {}
    try:
        with open(ALERTED_ARBS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}


def save_alerted_arbs(alerted: Dict[str, str]) -> None:
    """Save the set of alerted arb keys."""
    ALERTED_ARBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTED_ARBS_FILE, 'w') as f:
        json.dump(alerted, f, indent=2)


def cleanup_old_alerts(alerted: Dict[str, str]) -> Dict[str, str]:
    """Remove alerts older than 24 hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return {k: v for k, v in alerted.items() if v > cutoff}


def get_arb_key(opp: Dict) -> str:
    """Generate a unique key for an arb opportunity."""
    # Key = event + market + both books (sorted for consistency)
    books = sorted([opp['legs'][0]['book'], opp['legs'][1]['book']])
    return f"{opp['event_id']}:{opp['market']}:{books[0]}:{books[1]}"


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


def find_arbitrage_opportunities(odds: List[Dict[str, Any]], allowed_books: set) -> List[Dict[str, Any]]:
    """Find arbitrage opportunities filtered by allowed books."""
    # Group odds by event and market
    events: Dict[str, Dict[str, Dict[str, List[Dict]]]] = {}

    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=MAX_ODDS_AGE_HOURS)

    for row in odds:
        book = row['book'].lower()
        if book not in allowed_books:
            continue

        event_id = row['event_id']
        market = row['market_type']
        outcome = row['outcome']

        # Skip past events
        try:
            event_time = datetime.fromisoformat(row['event_start_time'].replace('+00:00', '+00:00'))
            if event_time < now:
                continue
        except:
            continue

        # Skip stale odds (older than MAX_ODDS_AGE_HOURS)
        last_updated = row.get('last_updated', '')
        if last_updated:
            try:
                update_time = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                if now - update_time > max_age:
                    continue  # Skip stale odds
            except:
                pass  # If we can't parse, include it

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
            'last_updated': row.get('last_updated', '')
        })

    # Find arbs
    opportunities = []

    for event_id, event in events.items():
        for market, outcomes in event['markets'].items():
            # Two-way markets (moneyline home/away, totals over/under)
            if market == 'moneyline':
                arb = check_two_way_arb(
                    outcomes.get('home', []),
                    outcomes.get('away', []),
                    event,
                    market,
                    event_id
                )
                if arb:
                    opportunities.append(arb)

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
                        arb = check_two_way_arb(
                            overs, unders, event, market, event_id,
                            side1_label=f"Over {line}", side2_label=f"Under {line}"
                        )
                        if arb:
                            opportunities.append(arb)

    return sorted(opportunities, key=lambda x: x['roi_percent'], reverse=True)


def check_two_way_arb(
    side1_odds: List[Dict],
    side2_odds: List[Dict],
    event: Dict,
    market: str,
    event_id: str,
    side1_label: Optional[str] = None,
    side2_label: Optional[str] = None
) -> Optional[Dict]:
    """Check for two-way arbitrage opportunity."""
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

    if implied_sum >= 1:
        return None  # No arb

    roi_percent = ((1 / implied_sum) - 1) * 100

    if roi_percent < MIN_ROI_PERCENT or roi_percent > MAX_ROI_PERCENT:
        return None

    # Calculate stakes for $100 total
    total_stake = 100
    stake1 = ((1/dec1) / implied_sum) * total_stake
    stake2 = ((1/dec2) / implied_sum) * total_stake
    profit = (stake1 * dec1) - total_stake

    # Calculate freshness (oldest leg determines arb freshness)
    oldest_update = None
    for leg_data in [best1, best2]:
        if leg_data.get('last_updated'):
            try:
                leg_time = datetime.fromisoformat(leg_data['last_updated'].replace('Z', '+00:00'))
                if oldest_update is None or leg_time < oldest_update:
                    oldest_update = leg_time
            except:
                pass

    return {
        'event_id': event_id,
        'sport': event['sport'],
        'home_team': event['home_team'],
        'away_team': event['away_team'],
        'event_start_time': event['event_start_time'],
        'market': market,
        'roi_percent': round(roi_percent, 2),
        'profit': round(profit, 2),
        'found_at': oldest_update.isoformat() if oldest_update else None,
        'legs': [
            {
                'side': side1_label or event['home_team'],
                'book': best1['book'],
                'odds': best1['price'],
                'stake': round(stake1, 2),
                'last_updated': best1.get('last_updated')
            },
            {
                'side': side2_label or event['away_team'],
                'book': best2['book'],
                'odds': best2['price'],
                'stake': round(stake2, 2),
                'last_updated': best2.get('last_updated')
            }
        ]
    }


def format_freshness(found_at: Optional[str]) -> str:
    """Format how long ago the arb was found."""
    if not found_at:
        return "Unknown"

    try:
        found_time = datetime.fromisoformat(found_at.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        delta = now - found_time

        minutes = int(delta.total_seconds() / 60)
        if minutes < 1:
            return "Just now"
        elif minutes < 60:
            return f"{minutes}m ago"
        elif minutes < 1440:
            hours = minutes // 60
            return f"{hours}h ago"
        else:
            days = minutes // 1440
            return f"{days}d ago"
    except:
        return "Unknown"


def get_book_link(book: str) -> str:
    """Get the sportsbook link for a book."""
    return SPORTSBOOK_LINKS.get(book.lower(), "")


def format_discord_message(opportunities: List[Dict]) -> Dict:
    """Format opportunities as a Discord webhook message."""
    if not opportunities:
        return None

    embeds = []

    for opp in opportunities[:5]:  # Limit to 5 per message
        # Format game time
        try:
            game_time = datetime.fromisoformat(opp['event_start_time'].replace('+00:00', '+00:00'))
            time_str = game_time.strftime("%a %I:%M %p ET")
        except:
            time_str = "TBD"

        # Format odds with + sign for positive
        def fmt_odds(odds):
            return f"+{odds}" if odds > 0 else str(odds)

        leg1 = opp['legs'][0]
        leg2 = opp['legs'][1]

        # Get sportsbook links
        link1 = get_book_link(leg1['book'])
        link2 = get_book_link(leg2['book'])

        # Format book names with links
        book1_display = f"[{leg1['book'].upper()}]({link1})" if link1 else leg1['book'].upper()
        book2_display = f"[{leg2['book'].upper()}]({link2})" if link2 else leg2['book'].upper()

        # Freshness indicator
        freshness = format_freshness(opp.get('found_at'))
        freshness_emoji = "🟢" if "m ago" in freshness or freshness == "Just now" else "🟡" if "h ago" in freshness else "🔴"

        embed = {
            "title": f"🚨 {opp['roi_percent']}% ARB: {opp['away_team']} @ {opp['home_team']}",
            "color": 0x00FF00,  # Green
            "fields": [
                {
                    "name": f"📍 {leg1['side']}",
                    "value": f"**{book1_display}** @ {fmt_odds(leg1['odds'])}\nStake: ${leg1['stake']:.2f}",
                    "inline": True
                },
                {
                    "name": f"📍 {leg2['side']}",
                    "value": f"**{book2_display}** @ {fmt_odds(leg2['odds'])}\nStake: ${leg2['stake']:.2f}",
                    "inline": True
                },
                {
                    "name": "💰 Guaranteed Profit",
                    "value": f"**${opp['profit']:.2f}** on $100",
                    "inline": True
                },
                {
                    "name": f"{freshness_emoji} Odds Age",
                    "value": freshness,
                    "inline": True
                }
            ],
            "footer": {
                "text": f"{opp['sport']} • {opp['market'].title()} • {time_str}"
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        embeds.append(embed)

    return {
        "content": f"**{len(opportunities)} Arbitrage Opportunity{'s' if len(opportunities) != 1 else ''} Found in Illinois!**",
        "embeds": embeds
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

    # Load already-alerted arbs and clean up old ones
    alerted = load_alerted_arbs()
    alerted = cleanup_old_alerts(alerted)
    now = datetime.now(timezone.utc).isoformat()

    print(f"Checking for arbs in Illinois-legal books: {', '.join(sorted(ILLINOIS_BOOKS))}")
    all_opportunities = find_arbitrage_opportunities(odds, ILLINOIS_BOOKS)

    # Filter out already-alerted arbs
    new_opportunities = []
    for opp in all_opportunities:
        key = get_arb_key(opp)
        if key not in alerted:
            new_opportunities.append(opp)
            alerted[key] = now  # Mark as alerted

    print(f"Found {len(all_opportunities)} total arbs, {len(new_opportunities)} new")

    if new_opportunities:
        for opp in new_opportunities:
            print(f"  NEW: {opp['roi_percent']}% - {opp['away_team']} @ {opp['home_team']} ({opp['market']})")
            for leg in opp['legs']:
                print(f"    {leg['side']} @ {leg['book'].upper()}: {leg['odds']} (${leg['stake']})")

        message = format_discord_message(new_opportunities)
        if message and DISCORD_WEBHOOK_URL:
            print("Sending Discord alert...")
            if send_discord_alert(message):
                print("Alert sent successfully!")
            else:
                print("Failed to send alert")
    else:
        print("No new arbitrage opportunities found")

    # Save updated alerted set
    save_alerted_arbs(alerted)
    print(f"\nTracking {len(alerted)} alerted opportunities (cleared after 24h)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
