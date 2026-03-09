#!/usr/bin/env python3
"""
Prediction Market Arbitrage Scanner

Detects arb opportunities on Polymarket and Kalshi:
1. Intra-platform arbs (Yes + No asks < $1.00)
2. Multi-outcome arbs (sum of all asks < $1.00)
3. Cross-platform arbs (buy Yes cheap on one, No cheap on other)

Usage:
    python scripts/prediction_arb.py                    # Full scan
    python scripts/prediction_arb.py --dry-run           # Sample data, no API calls
    python scripts/prediction_arb.py --polymarket-only   # Skip Kalshi
"""

import argparse
import difflib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MIN_ROI_PERCENT = 0.1
MAX_ROI_PERCENT = 20.0
MIN_VOLUME = 1000  # $1,000 minimum market volume
POLYMARKET_CLOB_DELAY = 0.1  # seconds between CLOB API requests
POLYMARKET_PRE_FILTER_SUM = 1.02  # skip markets where outcome price sum >= this
CROSS_MATCH_THRESHOLD = 0.75  # minimum fuzzy match score for cross-platform

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY", "")
KALSHI_RSA_KEY_PATH = os.environ.get("KALSHI_RSA_KEY_PATH", "")

ALERTED_FILE = Path(__file__).parent.parent / "data" / "prediction_markets" / "alerted_pred_arbs.json"

PLATFORM_LINKS = {
    "polymarket": "https://polymarket.com",
    "kalshi": "https://kalshi.com",
}

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PredictionOutcome:
    name: str
    token_id: str = ""
    best_bid: float = 0.0
    best_ask: float = 0.0

    @property
    def midpoint(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.best_ask or self.best_bid


@dataclass
class PredictionMarket:
    platform: str
    market_id: str
    question: str
    category: str
    outcomes: List[PredictionOutcome] = field(default_factory=list)
    volume: float = 0.0
    end_date: str = ""
    url: str = ""
    fetched_at: str = ""


@dataclass
class PredictionArbLeg:
    platform: str
    market_id: str
    outcome_name: str
    price: float
    url: str = ""


@dataclass
class PredictionArb:
    arb_type: str  # "intra_binary", "intra_multi", "cross_platform"
    question: str
    roi_percent: float
    total_cost: float
    profit: float
    legs: List[PredictionArbLeg] = field(default_factory=list)
    category: str = ""
    volume: float = 0.0
    platform: str = ""
    found_at: str = ""

# ---------------------------------------------------------------------------
# Dedup (mirrors arb_alerts.py)
# ---------------------------------------------------------------------------

def load_alerted_pred_arbs() -> Dict[str, str]:
    if not ALERTED_FILE.exists():
        return {}
    try:
        with open(ALERTED_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_alerted_pred_arbs(alerted: Dict[str, str]) -> None:
    ALERTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTED_FILE, "w") as f:
        json.dump(alerted, f, indent=2)


def cleanup_old_alerts(alerted: Dict[str, str]) -> Dict[str, str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return {k: v for k, v in alerted.items() if v > cutoff}


def get_pred_arb_key(arb: PredictionArb) -> str:
    if arb.arb_type == "cross_platform":
        ids = sorted(leg.market_id for leg in arb.legs)
        return f"cross:{ids[0]}:{ids[1]}"
    return f"{arb.arb_type}:{arb.platform}:{arb.legs[0].market_id if arb.legs else 'unknown'}"

# ---------------------------------------------------------------------------
# Polymarket API
# ---------------------------------------------------------------------------

def _api_get(url: str, headers: Optional[Dict] = None) -> dict:
    """Simple GET request returning parsed JSON."""
    req = Request(url)
    req.add_header("User-Agent", "hedj-prediction-arb/1.0")
    req.add_header("Accept", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


POLYMARKET_MAX_PAGES = 20  # 20 pages x 100 = 2000 markets max
POLYMARKET_VOLUME_MIN = 10000  # $10k+ volume for API filter (reduces pages)

def fetch_polymarket_markets() -> List[PredictionMarket]:
    """Fetch active markets from Polymarket Gamma API, then enrich candidates via CLOB."""
    print("Fetching Polymarket markets...")
    markets: List[PredictionMarket] = []
    offset = 0
    limit = 100
    total_fetched = 0
    pages = 0

    while pages < POLYMARKET_MAX_PAGES:
        url = (
            f"https://gamma-api.polymarket.com/markets"
            f"?limit={limit}&offset={offset}&active=true&closed=false"
            f"&volume_num_min={POLYMARKET_VOLUME_MIN}"
        )
        try:
            data = _api_get(url)
        except Exception as e:
            print(f"  Polymarket API error at offset {offset}: {e}")
            break

        if not data:
            break

        for item in data:
            total_fetched += 1
            # Parse embedded JSON strings
            try:
                outcome_prices = json.loads(item.get("outcomePrices", "[]"))
            except (json.JSONDecodeError, TypeError):
                continue

            if not outcome_prices:
                continue

            try:
                clob_token_ids = json.loads(item.get("clobTokenIds", "[]"))
            except (json.JSONDecodeError, TypeError):
                clob_token_ids = []

            try:
                outcomes_raw = json.loads(item.get("outcomes", "[]"))
            except (json.JSONDecodeError, TypeError):
                outcomes_raw = []

            # First-pass filter: skip if prices sum too high (no arb potential)
            price_sum = sum(float(p) for p in outcome_prices)
            if price_sum >= POLYMARKET_PRE_FILTER_SUM:
                continue

            # Volume filter
            volume = float(item.get("volume", 0) or 0)
            if volume < MIN_VOLUME:
                continue

            # Build outcomes with gamma prices as initial estimates
            outcomes = []
            for i, price_str in enumerate(outcome_prices):
                name = outcomes_raw[i] if i < len(outcomes_raw) else f"Outcome {i+1}"
                token_id = clob_token_ids[i] if i < len(clob_token_ids) else ""
                price = float(price_str)
                outcomes.append(PredictionOutcome(
                    name=name,
                    token_id=token_id,
                    best_bid=price,  # gamma price as estimate until CLOB enrichment
                    best_ask=price,
                ))

            slug = item.get("slug", item.get("conditionId", ""))
            market = PredictionMarket(
                platform="polymarket",
                market_id=item.get("conditionId", str(item.get("id", ""))),
                question=item.get("question", ""),
                category=item.get("groupItemTitle", item.get("category", "")),
                outcomes=outcomes,
                volume=volume,
                end_date=item.get("endDate", ""),
                url=f"https://polymarket.com/event/{slug}" if slug else "",
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            markets.append(market)

        if len(data) < limit:
            break
        offset += limit
        pages += 1

    print(f"  Fetched {total_fetched} total, {len(markets)} passed pre-filter (sum < {POLYMARKET_PRE_FILTER_SUM}, vol >= ${POLYMARKET_VOLUME_MIN:,})")

    # Second pass: only CLOB-enrich markets where gamma prices show arb potential (sum < 1.0)
    clob_candidates = [m for m in markets if sum(o.best_ask for o in m.outcomes) < 1.0]
    print(f"  CLOB enrichment candidates (gamma sum < 1.0): {len(clob_candidates)}")

    enriched = 0
    for mkt in clob_candidates:
        for outcome in mkt.outcomes:
            if not outcome.token_id:
                continue
            try:
                book = _api_get(f"https://clob.polymarket.com/book?token_id={outcome.token_id}")
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids:
                    outcome.best_bid = float(bids[0].get("price", 0))
                if asks:
                    outcome.best_ask = float(asks[0].get("price", 0))
                enriched += 1
            except Exception:
                pass  # Keep gamma estimates
            time.sleep(POLYMARKET_CLOB_DELAY)

    print(f"  Enriched {enriched} outcomes with CLOB orderbook data")
    return markets

# ---------------------------------------------------------------------------
# Kalshi API
# ---------------------------------------------------------------------------

def _kalshi_auth_headers(method: str, path: str, api_key: str, private_key_path: str) -> Dict[str, str]:
    """Generate Kalshi RSA auth headers."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, utils as asym_utils
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        print("  cryptography package not installed, cannot auth with Kalshi")
        raise

    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}"

    with open(private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

    # Kalshi expects base64-encoded RSA-PSS signature
    import base64
    signature = private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }


def fetch_kalshi_markets(api_key: str, private_key_path: str) -> List[PredictionMarket]:
    """Fetch active markets from Kalshi API."""
    print("Fetching Kalshi markets...")
    markets: List[PredictionMarket] = []
    cursor = None
    total_fetched = 0
    base_url = "https://api.elections.kalshi.com/trade-api/v2"

    while True:
        path = "/trade-api/v2/markets?status=open&limit=200"
        if cursor:
            path += f"&cursor={cursor}"
        full_url = f"https://api.elections.kalshi.com{path}"

        try:
            headers = _kalshi_auth_headers("GET", path, api_key, private_key_path)
            data = _api_get(full_url, headers=headers)
        except Exception as e:
            print(f"  Kalshi API error: {e}")
            break

        kalshi_markets = data.get("markets", [])
        cursor = data.get("cursor", "")

        for item in kalshi_markets:
            total_fetched += 1

            # Kalshi prices are in cents (0-100)
            yes_ask = item.get("yes_ask", 0) / 100.0 if item.get("yes_ask") else 0
            no_ask = item.get("no_ask", 0) / 100.0 if item.get("no_ask") else 0
            yes_bid = item.get("yes_bid", 0) / 100.0 if item.get("yes_bid") else 0
            no_bid = item.get("no_bid", 0) / 100.0 if item.get("no_bid") else 0
            volume = item.get("volume", 0) or 0

            if volume < MIN_VOLUME:
                continue

            ticker = item.get("ticker", "")
            outcomes = [
                PredictionOutcome(name="Yes", token_id=f"{ticker}_yes", best_bid=yes_bid, best_ask=yes_ask),
                PredictionOutcome(name="No", token_id=f"{ticker}_no", best_bid=no_bid, best_ask=no_ask),
            ]

            market = PredictionMarket(
                platform="kalshi",
                market_id=ticker,
                question=item.get("title", ""),
                category=item.get("category", ""),
                outcomes=outcomes,
                volume=volume,
                end_date=item.get("close_time", ""),
                url=f"https://kalshi.com/markets/{ticker}" if ticker else "",
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            markets.append(market)

        if not cursor or not kalshi_markets:
            break

    print(f"  Fetched {total_fetched} total, {len(markets)} passed volume filter (>= ${MIN_VOLUME:,})")
    return markets

# ---------------------------------------------------------------------------
# Intra-platform arb detection
# ---------------------------------------------------------------------------

POLYMARKET_FEE_PER_ORDER = 0.02  # ~$0.02 gas per order

def kalshi_fee(contracts: int, price: float) -> float:
    """Kalshi fee: ceil(0.07 * contracts * price * (1 - price)) per side."""
    if price <= 0 or price >= 1:
        return 0
    return math.ceil(0.07 * contracts * price * (1 - price) * 100) / 100


def detect_intra_arbs(markets: List[PredictionMarket]) -> List[PredictionArb]:
    """Detect arbs within a single platform's markets."""
    arbs: List[PredictionArb] = []

    for mkt in markets:
        asks = [o.best_ask for o in mkt.outcomes if o.best_ask > 0]
        if not asks or len(asks) < 2:
            continue

        total_ask = sum(asks)
        if total_ask >= 1.0:
            continue  # No arb

        # Raw profit per $1 payout
        raw_profit = 1.0 - total_ask

        # Fee calculation
        num_outcomes = len(mkt.outcomes)
        if mkt.platform == "polymarket":
            fees = POLYMARKET_FEE_PER_ORDER * num_outcomes
        else:
            # Kalshi fees depend on contracts; estimate for 1 contract
            fees = sum(kalshi_fee(1, o.best_ask) for o in mkt.outcomes if o.best_ask > 0)

        net_profit = raw_profit - fees
        if net_profit <= 0:
            continue

        roi = (net_profit / total_ask) * 100
        if roi < MIN_ROI_PERCENT or roi > MAX_ROI_PERCENT:
            continue

        arb_type = "intra_binary" if num_outcomes == 2 else "intra_multi"

        legs = []
        for o in mkt.outcomes:
            if o.best_ask <= 0:
                continue
            legs.append(PredictionArbLeg(
                platform=mkt.platform,
                market_id=mkt.market_id,
                outcome_name=o.name,
                price=o.best_ask,
                url=mkt.url,
            ))

        arb = PredictionArb(
            arb_type=arb_type,
            question=mkt.question,
            roi_percent=round(roi, 2),
            total_cost=round(total_ask, 4),
            profit=round(net_profit, 4),
            legs=legs,
            category=mkt.category,
            volume=mkt.volume,
            platform=mkt.platform,
            found_at=datetime.now(timezone.utc).isoformat(),
        )
        arbs.append(arb)

    return sorted(arbs, key=lambda a: a.roi_percent, reverse=True)

# ---------------------------------------------------------------------------
# Cross-platform matching + arb detection
# ---------------------------------------------------------------------------

FILLER_WORDS = {"will", "the", "be", "a", "an", "is", "of", "to", "in", "for", "on", "by", "at"}

def normalize_question(text: str) -> str:
    """Normalize question text for matching."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)  # Strip punctuation
    words = [w for w in text.split() if w not in FILLER_WORDS]
    return " ".join(words)


def match_markets(
    poly_markets: List[PredictionMarket],
    kalshi_markets: List[PredictionMarket],
) -> List[Tuple[PredictionMarket, PredictionMarket, float]]:
    """Match markets across platforms using fuzzy text matching."""
    matches: List[Tuple[PredictionMarket, PredictionMarket, float]] = []

    # Pre-normalize all questions
    poly_normalized = [(m, normalize_question(m.question)) for m in poly_markets]
    kalshi_normalized = [(m, normalize_question(m.question)) for m in kalshi_markets]

    for poly_mkt, poly_q in poly_normalized:
        best_match = None
        best_score = 0.0

        for kalshi_mkt, kalshi_q in kalshi_normalized:
            # Only match binary markets (Yes/No) for cross-platform
            if len(poly_mkt.outcomes) != 2 or len(kalshi_mkt.outcomes) != 2:
                continue

            # Exact normalized match
            if poly_q == kalshi_q:
                score = 1.0
            else:
                score = difflib.SequenceMatcher(None, poly_q, kalshi_q).ratio()

            # Category boost
            if poly_mkt.category and kalshi_mkt.category:
                if poly_mkt.category.lower() == kalshi_mkt.category.lower():
                    score += 0.1

            if score > best_score:
                best_score = score
                best_match = kalshi_mkt

        if best_match and best_score >= CROSS_MATCH_THRESHOLD:
            matches.append((poly_mkt, best_match, best_score))

    print(f"  Matched {len(matches)} market pairs across platforms (threshold >= {CROSS_MATCH_THRESHOLD})")
    return matches


def detect_cross_platform_arbs(
    matches: List[Tuple[PredictionMarket, PredictionMarket, float]],
) -> List[PredictionArb]:
    """Detect arbs across matched Polymarket/Kalshi markets."""
    arbs: List[PredictionArb] = []

    for poly_mkt, kalshi_mkt, confidence in matches:
        # Get Yes/No outcomes
        poly_yes = next((o for o in poly_mkt.outcomes if o.name.lower() == "yes"), None)
        poly_no = next((o for o in poly_mkt.outcomes if o.name.lower() == "no"), None)
        kalshi_yes = next((o for o in kalshi_mkt.outcomes if o.name.lower() == "yes"), None)
        kalshi_no = next((o for o in kalshi_mkt.outcomes if o.name.lower() == "no"), None)

        if not all([poly_yes, poly_no, kalshi_yes, kalshi_no]):
            continue

        # Check: buy Yes on Poly + No on Kalshi
        if poly_yes.best_ask > 0 and kalshi_no.best_ask > 0:
            cost = poly_yes.best_ask + kalshi_no.best_ask
            if cost < 1.0:
                raw_profit = 1.0 - cost
                fees = POLYMARKET_FEE_PER_ORDER + kalshi_fee(1, kalshi_no.best_ask)
                net_profit = raw_profit - fees
                if net_profit > 0:
                    roi = (net_profit / cost) * 100
                    if MIN_ROI_PERCENT <= roi <= MAX_ROI_PERCENT:
                        arbs.append(PredictionArb(
                            arb_type="cross_platform",
                            question=poly_mkt.question,
                            roi_percent=round(roi, 2),
                            total_cost=round(cost, 4),
                            profit=round(net_profit, 4),
                            legs=[
                                PredictionArbLeg("polymarket", poly_mkt.market_id, "Yes", poly_yes.best_ask, poly_mkt.url),
                                PredictionArbLeg("kalshi", kalshi_mkt.market_id, "No", kalshi_no.best_ask, kalshi_mkt.url),
                            ],
                            category=poly_mkt.category,
                            volume=min(poly_mkt.volume, kalshi_mkt.volume),
                            platform="cross",
                            found_at=datetime.now(timezone.utc).isoformat(),
                        ))

        # Check: buy No on Poly + Yes on Kalshi
        if poly_no.best_ask > 0 and kalshi_yes.best_ask > 0:
            cost = poly_no.best_ask + kalshi_yes.best_ask
            if cost < 1.0:
                raw_profit = 1.0 - cost
                fees = POLYMARKET_FEE_PER_ORDER + kalshi_fee(1, kalshi_yes.best_ask)
                net_profit = raw_profit - fees
                if net_profit > 0:
                    roi = (net_profit / cost) * 100
                    if MIN_ROI_PERCENT <= roi <= MAX_ROI_PERCENT:
                        arbs.append(PredictionArb(
                            arb_type="cross_platform",
                            question=poly_mkt.question,
                            roi_percent=round(roi, 2),
                            total_cost=round(cost, 4),
                            profit=round(net_profit, 4),
                            legs=[
                                PredictionArbLeg("polymarket", poly_mkt.market_id, "No", poly_no.best_ask, poly_mkt.url),
                                PredictionArbLeg("kalshi", kalshi_mkt.market_id, "Yes", kalshi_yes.best_ask, kalshi_mkt.url),
                            ],
                            category=poly_mkt.category,
                            volume=min(poly_mkt.volume, kalshi_mkt.volume),
                            platform="cross",
                            found_at=datetime.now(timezone.utc).isoformat(),
                        ))

    return sorted(arbs, key=lambda a: a.roi_percent, reverse=True)

# ---------------------------------------------------------------------------
# Discord alerting
# ---------------------------------------------------------------------------

def format_freshness(found_at: Optional[str]) -> str:
    if not found_at:
        return "Unknown"
    try:
        found_time = datetime.fromisoformat(found_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - found_time
        minutes = int(delta.total_seconds() / 60)
        if minutes < 1:
            return "Just now"
        elif minutes < 60:
            return f"{minutes}m ago"
        elif minutes < 1440:
            return f"{minutes // 60}h ago"
        else:
            return f"{minutes // 1440}d ago"
    except Exception:
        return "Unknown"


def format_discord_embeds(arbs: List[PredictionArb]) -> Optional[Dict]:
    """Format arbs as Discord webhook message with embeds."""
    if not arbs:
        return None

    embeds = []
    for arb in arbs[:5]:  # Limit to 5 per message
        # Color by type
        color = {
            "intra_binary": 0x00FF00,   # green
            "intra_multi": 0x00CCFF,    # blue
            "cross_platform": 0xFFAA00, # orange
        }.get(arb.arb_type, 0x888888)

        type_label = {
            "intra_binary": "Intra-Platform (Binary)",
            "intra_multi": "Intra-Platform (Multi)",
            "cross_platform": "Cross-Platform",
        }.get(arb.arb_type, arb.arb_type)

        # Build leg fields
        fields = []
        for i, leg in enumerate(arb.legs):
            platform_upper = leg.platform.upper()
            link = leg.url
            platform_display = f"[{platform_upper}]({link})" if link else platform_upper
            fields.append({
                "name": f"{'📍'} Buy {leg.outcome_name}",
                "value": f"**{platform_display}** @ ${leg.price:.2f}",
                "inline": True,
            })

        fields.append({
            "name": "💰 Profit",
            "value": f"**${arb.profit:.4f}** per pair on ${arb.total_cost:.4f}\nROI: {arb.roi_percent}%",
            "inline": True,
        })

        vol_str = f"${arb.volume:,.0f}" if arb.volume else "N/A"
        cat_str = arb.category or "N/A"
        fields.append({
            "name": "📊 Info",
            "value": f"Category: {cat_str} | Volume: {vol_str}",
            "inline": False,
        })

        embed = {
            "title": f"🎯 {arb.roi_percent}% ARB: {arb.question[:80]}",
            "color": color,
            "fields": fields,
            "footer": {"text": f"{arb.platform.title()} | {type_label}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        embeds.append(embed)

    count = len(arbs)
    return {
        "content": f"**{count} Prediction Market Arb{'s' if count != 1 else ''} Found!**",
        "embeds": embeds,
    }


def send_discord_alert(message: Dict) -> bool:
    """Send alert to Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        print("No Discord webhook URL configured, skipping alert")
        return False

    try:
        data = json.dumps(message).encode("utf-8")
        req = Request(DISCORD_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as resp:
            return resp.status == 204
    except URLError as e:
        print(f"Failed to send Discord alert: {e}")
        return False

# ---------------------------------------------------------------------------
# Dry-run sample data
# ---------------------------------------------------------------------------

def generate_sample_data() -> Tuple[List[PredictionMarket], List[PredictionMarket]]:
    """Generate sample data for --dry-run testing."""
    now = datetime.now(timezone.utc).isoformat()

    poly_markets = [
        # Binary arb candidate: Yes 0.42 + No 0.48 = 0.90 -> ~7% arb after fees
        PredictionMarket(
            platform="polymarket", market_id="sample_btc_100k",
            question="Will Bitcoin hit $100k by March 2026?",
            category="Crypto", volume=1_200_000, url="https://polymarket.com/event/btc-100k",
            fetched_at=now,
            outcomes=[
                PredictionOutcome(name="Yes", token_id="t1", best_bid=0.41, best_ask=0.42),
                PredictionOutcome(name="No", token_id="t2", best_bid=0.47, best_ask=0.48),
            ],
        ),
        # No arb: Yes 0.55 + No 0.48 = 1.03
        PredictionMarket(
            platform="polymarket", market_id="sample_fed_rate",
            question="Will the Fed cut rates in March 2026?",
            category="Economics", volume=500_000, url="https://polymarket.com/event/fed-rate-march",
            fetched_at=now,
            outcomes=[
                PredictionOutcome(name="Yes", token_id="t3", best_bid=0.54, best_ask=0.55),
                PredictionOutcome(name="No", token_id="t4", best_bid=0.47, best_ask=0.48),
            ],
        ),
        # Multi-outcome arb: 0.28 + 0.23 + 0.18 + 0.20 = 0.89 -> ~4% after fees
        PredictionMarket(
            platform="polymarket", market_id="sample_super_bowl",
            question="Who will win Super Bowl LX?",
            category="Sports", volume=3_000_000, url="https://polymarket.com/event/super-bowl-lx",
            fetched_at=now,
            outcomes=[
                PredictionOutcome(name="Chiefs", token_id="t5", best_bid=0.27, best_ask=0.28),
                PredictionOutcome(name="Lions", token_id="t6", best_bid=0.22, best_ask=0.23),
                PredictionOutcome(name="Eagles", token_id="t7", best_bid=0.17, best_ask=0.18),
                PredictionOutcome(name="Bills", token_id="t8", best_bid=0.19, best_ask=0.20),
            ],
        ),
        # Cross-platform match candidate
        PredictionMarket(
            platform="polymarket", market_id="sample_pres_approval",
            question="Will presidential approval rating exceed 50% by April?",
            category="Politics", volume=800_000, url="https://polymarket.com/event/pres-approval",
            fetched_at=now,
            outcomes=[
                PredictionOutcome(name="Yes", token_id="t9", best_bid=0.34, best_ask=0.35),
                PredictionOutcome(name="No", token_id="t10", best_bid=0.60, best_ask=0.62),
            ],
        ),
    ]

    kalshi_markets = [
        # Cross-platform match: same question, different prices
        PredictionMarket(
            platform="kalshi", market_id="PRES-APPROVE-50-APR",
            question="Presidential approval rating above 50% by April?",
            category="Politics", volume=200_000, url="https://kalshi.com/markets/PRES-APPROVE-50-APR",
            fetched_at=now,
            outcomes=[
                PredictionOutcome(name="Yes", token_id="PRES-APPROVE-50-APR_yes", best_bid=0.40, best_ask=0.42),
                PredictionOutcome(name="No", token_id="PRES-APPROVE-50-APR_no", best_bid=0.55, best_ask=0.57),
            ],
        ),
        # Kalshi intra arb: Yes 0.45 + No 0.52 = 0.97
        PredictionMarket(
            platform="kalshi", market_id="RECESSION-2026",
            question="Will the US enter a recession in 2026?",
            category="Economics", volume=500_000, url="https://kalshi.com/markets/RECESSION-2026",
            fetched_at=now,
            outcomes=[
                PredictionOutcome(name="Yes", token_id="RECESSION-2026_yes", best_bid=0.44, best_ask=0.45),
                PredictionOutcome(name="No", token_id="RECESSION-2026_no", best_bid=0.51, best_ask=0.52),
            ],
        ),
    ]

    return poly_markets, kalshi_markets

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Prediction Market Arb Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Use sample data, no API calls")
    parser.add_argument("--polymarket-only", action="store_true", help="Skip Kalshi")
    args = parser.parse_args()

    print("=" * 60)
    print("Prediction Market Arb Scanner")
    print("=" * 60)

    # --- Fetch markets ---
    if args.dry_run:
        print("\n[DRY RUN] Using sample data\n")
        poly_markets, kalshi_markets = generate_sample_data()
        if args.polymarket_only:
            kalshi_markets = []
    else:
        poly_markets = fetch_polymarket_markets()
        kalshi_markets = []
        if not args.polymarket_only:
            if KALSHI_API_KEY and KALSHI_RSA_KEY_PATH:
                kalshi_markets = fetch_kalshi_markets(KALSHI_API_KEY, KALSHI_RSA_KEY_PATH)
            else:
                print("\nKalshi credentials not set (KALSHI_API_KEY / KALSHI_RSA_KEY_PATH), skipping Kalshi")

    print(f"\nMarkets loaded: {len(poly_markets)} Polymarket, {len(kalshi_markets)} Kalshi")

    # --- Detect intra-platform arbs ---
    print("\n--- Intra-platform arb detection ---")
    all_arbs: List[PredictionArb] = []

    poly_intra = detect_intra_arbs(poly_markets)
    print(f"  Polymarket intra arbs: {len(poly_intra)}")
    all_arbs.extend(poly_intra)

    if kalshi_markets:
        kalshi_intra = detect_intra_arbs(kalshi_markets)
        print(f"  Kalshi intra arbs: {len(kalshi_intra)}")
        all_arbs.extend(kalshi_intra)

    # --- Cross-platform matching + detection ---
    if poly_markets and kalshi_markets:
        print("\n--- Cross-platform matching ---")
        matches = match_markets(poly_markets, kalshi_markets)
        if matches:
            cross_arbs = detect_cross_platform_arbs(matches)
            print(f"  Cross-platform arbs: {len(cross_arbs)}")
            all_arbs.extend(cross_arbs)
    else:
        print("\n--- Cross-platform matching skipped (need both platforms) ---")

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print(f"Total arbs found: {len(all_arbs)}")

    if not all_arbs:
        print("No arbitrage opportunities found")
        return 0

    # Sort all arbs by ROI
    all_arbs.sort(key=lambda a: a.roi_percent, reverse=True)

    # --- Dedup ---
    alerted = load_alerted_pred_arbs()
    alerted = cleanup_old_alerts(alerted)
    now = datetime.now(timezone.utc).isoformat()

    new_arbs = []
    for arb in all_arbs:
        key = get_pred_arb_key(arb)
        if key not in alerted:
            new_arbs.append(arb)
            alerted[key] = now

    print(f"New (un-alerted) arbs: {len(new_arbs)}")

    # --- Print details ---
    for arb in all_arbs:
        marker = "NEW" if arb in new_arbs else "dup"
        print(f"\n  [{marker}] {arb.roi_percent}% {arb.arb_type}: {arb.question[:70]}")
        for leg in arb.legs:
            print(f"    Buy {leg.outcome_name} on {leg.platform.upper()} @ ${leg.price:.2f}")
        print(f"    Cost: ${arb.total_cost:.4f} -> Profit: ${arb.profit:.4f}")

    # --- Discord alert ---
    if new_arbs:
        message = format_discord_embeds(new_arbs)
        if message and DISCORD_WEBHOOK_URL:
            print("\nSending Discord alert...")
            if send_discord_alert(message):
                print("Alert sent successfully!")
            else:
                print("Failed to send alert")
        elif not DISCORD_WEBHOOK_URL:
            print("\nNo DISCORD_WEBHOOK_URL set, skipping Discord alert")

    # --- Save dedup state ---
    save_alerted_pred_arbs(alerted)
    print(f"\nTracking {len(alerted)} alerted opportunities (cleared after 24h)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
