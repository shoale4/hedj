#!/usr/bin/env python3
"""
Kalshi Market Maker

Posts two-sided limit orders (post_only) to capture bid-ask spread.
Skews quotes based on inventory to manage directional risk.
Circuit breaker cancels all orders on rapid price movement.

Usage:
    # Discovery: find good markets to make
    python scripts/kalshi_market_maker.py --discover

    # Dry run (DEFAULT): real data, simulated orders
    python scripts/kalshi_market_maker.py --ticker TICKER --spread 4 --size 10

    # Live trading (real money!)
    python scripts/kalshi_market_maker.py --ticker TICKER --spread 4 --size 10 --live

    # Live with Discord alerts on fills
    DISCORD_WEBHOOK_URL=... python scripts/kalshi_market_maker.py --ticker TICKER --live
"""

import argparse
import base64
import json
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY", "")
KALSHI_RSA_KEY_PATH = os.environ.get("KALSHI_RSA_KEY_PATH", "")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Safety defaults
DEFAULT_SPREAD_CENTS = 4       # 4 cent spread ($0.04)
DEFAULT_SIZE = 10              # 10 contracts per side
DEFAULT_MAX_POSITION = 100     # Max contracts in one direction
DEFAULT_MAX_LOSS_CENTS = 5000  # $50 max loss before shutdown
DEFAULT_REFRESH_SEC = 30       # Seconds between quote updates
CIRCUIT_BREAKER_MOVE = 10      # Cancel all if midpoint moves 10c in one cycle
MIN_SPREAD_CENTS = 2           # Never quote tighter than 2c
MAX_SPREAD_CENTS = 30          # Never quote wider than 30c

# ---------------------------------------------------------------------------
# Kalshi API client
# ---------------------------------------------------------------------------

class KalshiAPI:
    def __init__(self, api_key: str, private_key_path: str):
        self.api_key = api_key
        self.private_key = self._load_key(private_key_path)

    @staticmethod
    def _load_key(path: str):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

    def _sign(self, method: str, path: str) -> Dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = str(int(time.time() * 1000))
        # Strip query params for signing
        path_no_query = path.split("?")[0]
        message = f"{timestamp}{method}{path_no_query}"

        signature = self.private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{KALSHI_BASE}{path}"
        headers = self._sign(method, f"/trade-api/v2{path}")

        if body is not None:
            data = json.dumps(body).encode()
        else:
            data = None

        req = Request(url, data=data, headers=headers, method=method)
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            if not raw:
                return {}
            return json.loads(raw)

    # -- Market data --

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook?depth={depth}").get("orderbook", {})

    def get_markets(self, status: str = "open", limit: int = 200, cursor: str = "") -> dict:
        path = f"/markets?status={status}&limit={limit}"
        if cursor:
            path += f"&cursor={cursor}"
        return self._request("GET", path)

    # -- Trading --

    def place_order(self, ticker: str, side: str, action: str, price_cents: int,
                    count: int, post_only: bool = True) -> dict:
        body = {
            "ticker": ticker,
            "side": side,        # "yes" or "no"
            "action": action,    # "buy" or "sell"
            "type": "limit",
            "count": count,
            "post_only": post_only,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents
        return self._request("POST", "/portfolio/orders", body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def get_orders(self, ticker: str = "", status: str = "resting") -> List[dict]:
        path = f"/portfolio/orders?status={status}&limit=200"
        if ticker:
            path += f"&ticker={ticker}"
        return self._request("GET", path).get("orders", [])

    def get_positions(self, ticker: str = "") -> List[dict]:
        path = "/portfolio/positions?limit=200"
        if ticker:
            path += f"&ticker={ticker}"
        return self._request("GET", path).get("market_positions", [])

    def get_balance(self) -> int:
        """Returns balance in cents."""
        return self._request("GET", "/portfolio/balance").get("balance", 0)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    side: str       # "yes" or "no"
    price_cents: int
    size: int

@dataclass
class OpenOrder:
    order_id: str
    side: str
    action: str
    price_cents: int
    remaining: int

@dataclass
class Fill:
    side: str
    price_cents: int
    count: int
    is_maker: bool
    timestamp: str

# ---------------------------------------------------------------------------
# Quote engine
# ---------------------------------------------------------------------------

def calc_midpoint(orderbook: dict) -> Optional[int]:
    """Calculate midpoint from Kalshi orderbook in cents.

    Kalshi orderbook returns YES bids and NO bids.
    YES best ask = 100 - best NO bid.
    """
    yes_bids = orderbook.get("yes", [])
    no_bids = orderbook.get("no", [])

    if not yes_bids and not no_bids:
        return None

    # Best YES bid is last element (sorted ascending)
    best_yes_bid = yes_bids[-1][0] if yes_bids else None
    # Best NO bid → YES ask = 100 - no_bid
    best_yes_ask = (100 - no_bids[-1][0]) if no_bids else None

    if best_yes_bid is not None and best_yes_ask is not None:
        return (best_yes_bid + best_yes_ask) // 2
    elif best_yes_bid is not None:
        return best_yes_bid
    elif best_yes_ask is not None:
        return best_yes_ask
    return 50  # fallback


def calc_quotes(midpoint: int, spread_cents: int, inventory: int,
                skew_per_contract: float = 0.05) -> Tuple[Quote, Quote]:
    """Calculate two-sided quotes with inventory skew.

    Inventory skew: if we're long YES (positive inventory), shift mid down
    to encourage selling YES (buying NO) and discourage buying more YES.
    """
    half_spread = spread_cents / 2.0

    # Skew: shift midpoint against our inventory
    inventory_shift = -skew_per_contract * inventory
    adjusted_mid = midpoint + inventory_shift

    bid_price = max(1, min(99, int(adjusted_mid - half_spread)))
    ask_price = max(1, min(99, int(adjusted_mid + half_spread)))

    # Ensure minimum spread
    if ask_price - bid_price < MIN_SPREAD_CENTS:
        ask_price = min(99, bid_price + MIN_SPREAD_CENTS)

    return (
        Quote(side="yes", price_cents=bid_price, size=0),    # Buy YES at bid
        Quote(side="no", price_cents=100 - ask_price, size=0) # Buy NO (= sell YES at ask)
    )


def kalshi_fee(contracts: int, price_cents: int, is_maker: bool = True) -> int:
    """Calculate Kalshi fee in cents. Maker = 0.0175, Taker = 0.07."""
    p = price_cents / 100.0
    coeff = 0.0175 if is_maker else 0.07
    return math.ceil(coeff * contracts * p * (1 - p) * 100)

# ---------------------------------------------------------------------------
# Inventory & P&L tracking
# ---------------------------------------------------------------------------

@dataclass
class InventoryState:
    yes_position: int = 0       # Net YES contracts (positive = long YES)
    total_cost_cents: int = 0   # Total cost basis in cents
    total_revenue_cents: int = 0
    total_fees_cents: int = 0
    fills: List[dict] = field(default_factory=list)

    @property
    def net_pnl_cents(self) -> int:
        return self.total_revenue_cents - self.total_cost_cents - self.total_fees_cents

    def record_fill(self, side: str, price_cents: int, count: int, is_maker: bool):
        fee = kalshi_fee(count, price_cents, is_maker)
        cost = price_cents * count

        if side == "yes":
            self.yes_position += count
            self.total_cost_cents += cost + fee
        else:  # "no"
            self.yes_position -= count
            self.total_cost_cents += (100 - price_cents) * count + fee
            # Buying NO at X is like selling YES at (100-X)

        self.total_fees_cents += fee
        self.fills.append({
            "side": side, "price": price_cents, "count": count,
            "fee": fee, "is_maker": is_maker,
            "time": datetime.now(timezone.utc).isoformat(),
        })

    def mark_to_market(self, midpoint_cents: int) -> int:
        """Unrealized P&L based on current midpoint."""
        if self.yes_position > 0:
            return self.yes_position * midpoint_cents - self.total_cost_cents
        elif self.yes_position < 0:
            return abs(self.yes_position) * (100 - midpoint_cents) - self.total_cost_cents
        return -self.total_cost_cents

    def summary(self, midpoint_cents: int) -> str:
        mtm = self.mark_to_market(midpoint_cents)
        return (
            f"Position: {self.yes_position:+d} YES | "
            f"Cost: ${self.total_cost_cents/100:.2f} | "
            f"Fees: ${self.total_fees_cents/100:.2f} | "
            f"Fills: {len(self.fills)} | "
            f"MTM P&L: ${mtm/100:+.2f}"
        )

# ---------------------------------------------------------------------------
# Discord alerts for fills
# ---------------------------------------------------------------------------

def send_fill_alert(ticker: str, side: str, price_cents: int, count: int,
                    inventory: InventoryState, midpoint: int):
    if not DISCORD_WEBHOOK_URL:
        return
    embed = {
        "title": f"{'🟢' if side == 'yes' else '🔴'} Fill: {ticker}",
        "color": 0x00FF00 if side == "yes" else 0xFF4444,
        "fields": [
            {"name": "Side", "value": side.upper(), "inline": True},
            {"name": "Price", "value": f"${price_cents}c", "inline": True},
            {"name": "Count", "value": str(count), "inline": True},
            {"name": "Position", "value": f"{inventory.yes_position:+d} YES", "inline": True},
            {"name": "MTM P&L", "value": f"${inventory.mark_to_market(midpoint)/100:+.2f}", "inline": True},
        ],
        "footer": {"text": "Kalshi Market Maker"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    msg = {"embeds": [embed]}
    try:
        data = json.dumps(msg).encode()
        req = Request(DISCORD_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=5)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

DISCOVERY_SERIES = [
    "KXBTC", "KXBTCD", "KXBTCW", "KXETH", "KXETHD",
    "KXGDP", "KXCPI", "KXFED",
    "INX", "INXD", "NASDAQ100Y", "NASDAQ100D",
    "KXTREMP",
]


def discover_markets(api: KalshiAPI):
    """Find markets with good spread + volume for market making."""
    print("Discovering markets across key series...\n")

    all_markets = []
    for series in DISCOVERY_SERIES:
        try:
            data = api._request("GET", f"/markets?status=open&series_ticker={series}&limit=50")
            mkts = data.get("markets", [])
            all_markets.extend(mkts)
        except Exception:
            pass

    print(f"Fetched {len(all_markets)} markets across {len(DISCOVERY_SERIES)} series\n")

    candidates = []
    for m in all_markets:
        yes_bid = m.get("yes_bid", 0) or 0
        no_bid = m.get("no_bid", 0) or 0
        volume = m.get("volume", 0) or 0

        if not yes_bid and not no_bid:
            continue

        best_yes_ask = (100 - no_bid) if no_bid else 99
        spread = best_yes_ask - yes_bid if yes_bid else 99
        midpoint = (yes_bid + best_yes_ask) // 2 if yes_bid else best_yes_ask

        distance_from_50 = abs(midpoint - 50)

        close_time = m.get("close_time", "")
        try:
            close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            hours_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_to_close < 1:
                continue
        except Exception:
            hours_to_close = 999

        candidates.append({
            "ticker": m.get("ticker", ""),
            "title": m.get("title", "")[:55],
            "volume": volume,
            "spread": spread,
            "midpoint": midpoint,
            "hours_to_close": hours_to_close,
            "distance_from_50": distance_from_50,
        })

    # Sort by volume descending, then tightest spread
    candidates.sort(key=lambda c: (-min(c["volume"], 100000), c["spread"], c["distance_from_50"]))

    print(f"{'Ticker':<40} {'Mid':>4} {'Sprd':>5} {'Vol':>8} {'Hrs':>5}  Title")
    print("-" * 120)
    for c in candidates[:30]:
        hrs = f"{c['hours_to_close']:.0f}" if c['hours_to_close'] < 9999 else "n/a"
        print(
            f"{c['ticker']:<40} {c['midpoint']:>3}c {c['spread']:>4}c "
            f"{c['volume']:>7,} {hrs:>5}  {c['title']}"
        )

    if not candidates:
        print("No suitable markets found")

# ---------------------------------------------------------------------------
# Main market maker loop
# ---------------------------------------------------------------------------

class MarketMaker:
    def __init__(self, api: KalshiAPI, ticker: str, spread_cents: int, size: int,
                 max_position: int, max_loss_cents: int, refresh_sec: int,
                 live: bool = False):
        self.api = api
        self.ticker = ticker
        self.spread_cents = spread_cents
        self.size = size
        self.max_position = max_position
        self.max_loss_cents = max_loss_cents
        self.refresh_sec = refresh_sec
        self.live = live

        self.inventory = InventoryState()
        self.prev_midpoint: Optional[int] = None
        self.our_order_ids: Dict[str, str] = {}  # "yes_bid"/"no_bid" -> order_id
        self.running = True
        self.cycle_count = 0

        # Register signal handler for clean shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        print(f"\n{'='*60}")
        print("SHUTDOWN SIGNAL RECEIVED — canceling all orders...")
        self.running = False
        self._cancel_all_orders()
        print(f"\nFinal state: {self.inventory.summary(self.prev_midpoint or 50)}")
        print(f"{'='*60}")
        sys.exit(0)

    def _cancel_all_orders(self):
        """Cancel all our resting orders."""
        for label, order_id in list(self.our_order_ids.items()):
            try:
                if self.live:
                    self.api.cancel_order(order_id)
                print(f"  Canceled {label}: {order_id}")
            except Exception as e:
                print(f"  Failed to cancel {label}: {e}")
        self.our_order_ids.clear()

    def _check_fills(self):
        """Check if any of our orders have been filled."""
        for label, order_id in list(self.our_order_ids.items()):
            try:
                # Get order status by listing resting orders
                # If our order_id is no longer in resting orders, it was filled
                pass  # We check via position changes below
            except Exception:
                pass

        # Check position to detect fills
        if self.live:
            positions = self.api.get_positions(self.ticker)
            for pos in positions:
                if pos.get("ticker") == self.ticker:
                    server_yes = pos.get("market_exposure", 0)
                    # Detect fills by comparing server position to our tracked position
                    # This is a simplified approach; production would use WebSocket fills
                    break

    def _reconcile_orders(self, desired_bid: Quote, desired_ask: Quote):
        """Cancel stale orders and place new ones."""
        # Cancel existing orders
        self._cancel_all_orders()

        # Skip if position limit reached
        if abs(self.inventory.yes_position) >= self.max_position:
            print(f"  POSITION LIMIT ({self.max_position}) reached — pulling quotes")
            return

        # Place new bid (buy YES)
        print(f"  BID: Buy YES @ {desired_bid.price_cents}c x {self.size}")
        if self.live:
            try:
                resp = self.api.place_order(
                    self.ticker, "yes", "buy",
                    desired_bid.price_cents, self.size, post_only=True
                )
                order = resp.get("order", {})
                oid = order.get("order_id", "")
                if oid:
                    self.our_order_ids["yes_bid"] = oid
                    print(f"    → Order {oid[:12]}...")
                else:
                    print(f"    → Response: {resp}")
            except Exception as e:
                print(f"    → FAILED: {e}")

        # Place new ask (buy NO = sell YES)
        ask_cents_yes = 100 - desired_ask.price_cents  # Convert NO price to YES equivalent
        print(f"  ASK: Buy NO @ {desired_ask.price_cents}c x {self.size} (= sell YES @ {ask_cents_yes}c)")
        if self.live:
            try:
                resp = self.api.place_order(
                    self.ticker, "no", "buy",
                    desired_ask.price_cents, self.size, post_only=True
                )
                order = resp.get("order", {})
                oid = order.get("order_id", "")
                if oid:
                    self.our_order_ids["no_bid"] = oid
                    print(f"    → Order {oid[:12]}...")
                else:
                    print(f"    → Response: {resp}")
            except Exception as e:
                print(f"    → FAILED: {e}")

    def run(self):
        mode = "LIVE" if self.live else "DRY RUN"
        print(f"\n{'='*60}")
        print(f"Kalshi Market Maker [{mode}]")
        print(f"{'='*60}")
        print(f"Ticker:       {self.ticker}")
        print(f"Spread:       {self.spread_cents}c")
        print(f"Size:         {self.size} contracts/side")
        print(f"Max position: {self.max_position}")
        print(f"Max loss:     ${self.max_loss_cents/100:.2f}")
        print(f"Refresh:      {self.refresh_sec}s")
        print(f"{'='*60}\n")

        if self.live:
            balance = self.api.get_balance()
            print(f"Account balance: ${balance/100:.2f}\n")

        # Get market info
        market = self.api.get_market(self.ticker)
        if not market:
            print(f"Market {self.ticker} not found")
            return 1

        print(f"Market: {market.get('title', 'Unknown')}")
        print(f"Category: {market.get('category', 'Unknown')}")
        close_time = market.get("close_time", "")
        if close_time:
            try:
                close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                hours_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                print(f"Closes in: {hours_left:.1f} hours")
            except Exception:
                pass
        print()

        while self.running:
            self.cycle_count += 1
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] Cycle #{self.cycle_count}")

            try:
                # 1. Get orderbook
                book = self.api.get_orderbook(self.ticker)
                midpoint = calc_midpoint(book)

                if midpoint is None:
                    print("  No orderbook data, skipping")
                    time.sleep(self.refresh_sec)
                    continue

                # 2. Circuit breaker: check for rapid movement
                if self.prev_midpoint is not None:
                    move = abs(midpoint - self.prev_midpoint)
                    if move >= CIRCUIT_BREAKER_MOVE:
                        print(f"  ⚠️  CIRCUIT BREAKER: midpoint moved {move}c ({self.prev_midpoint}→{midpoint})")
                        self._cancel_all_orders()
                        print(f"  Pausing {self.refresh_sec * 2}s...")
                        time.sleep(self.refresh_sec * 2)
                        self.prev_midpoint = midpoint
                        continue

                self.prev_midpoint = midpoint

                # 3. Check for fills (by checking resting orders)
                if self.live:
                    resting = self.api.get_orders(ticker=self.ticker, status="resting")
                    resting_ids = {o["order_id"] for o in resting}

                    for label, oid in list(self.our_order_ids.items()):
                        if oid not in resting_ids:
                            # Order was filled!
                            side = "yes" if label == "yes_bid" else "no"
                            price = midpoint  # Approximate; real impl uses WebSocket fill data
                            print(f"  🎯 FILL detected: {label} ({side} @ ~{price}c)")
                            self.inventory.record_fill(side, price, self.size, is_maker=True)
                            del self.our_order_ids[label]

                            send_fill_alert(
                                self.ticker, side, price, self.size,
                                self.inventory, midpoint
                            )

                # 4. Check loss limit
                mtm = self.inventory.mark_to_market(midpoint)
                if mtm < -self.max_loss_cents:
                    print(f"  🛑 MAX LOSS REACHED: ${mtm/100:.2f} (limit: ${-self.max_loss_cents/100:.2f})")
                    self._cancel_all_orders()
                    print(f"\nFinal: {self.inventory.summary(midpoint)}")
                    return 1

                # 5. Calculate desired quotes
                desired_bid, desired_ask = calc_quotes(
                    midpoint, self.spread_cents, self.inventory.yes_position
                )
                desired_bid.size = self.size
                desired_ask.size = self.size

                # 6. Display state
                yes_bids = book.get("yes", [])
                no_bids = book.get("no", [])
                best_bid = yes_bids[-1][0] if yes_bids else "?"
                best_ask = (100 - no_bids[-1][0]) if no_bids else "?"
                print(f"  Book: {best_bid}c / {best_ask}c | Mid: {midpoint}c")
                print(f"  {self.inventory.summary(midpoint)}")

                # 7. Place/update orders
                self._reconcile_orders(desired_bid, desired_ask)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  ERROR: {e}")

            # Sleep
            print(f"  Sleeping {self.refresh_sec}s...\n")
            time.sleep(self.refresh_sec)

        return 0

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Kalshi Market Maker")
    parser.add_argument("--discover", action="store_true", help="Find good markets to make")
    parser.add_argument("--ticker", type=str, help="Market ticker to trade")
    parser.add_argument("--spread", type=int, default=DEFAULT_SPREAD_CENTS, help=f"Spread in cents (default: {DEFAULT_SPREAD_CENTS})")
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE, help=f"Contracts per side (default: {DEFAULT_SIZE})")
    parser.add_argument("--max-position", type=int, default=DEFAULT_MAX_POSITION, help=f"Max inventory (default: {DEFAULT_MAX_POSITION})")
    parser.add_argument("--max-loss", type=float, default=DEFAULT_MAX_LOSS_CENTS/100, help=f"Max loss in dollars (default: ${DEFAULT_MAX_LOSS_CENTS/100:.0f})")
    parser.add_argument("--refresh", type=int, default=DEFAULT_REFRESH_SEC, help=f"Refresh interval seconds (default: {DEFAULT_REFRESH_SEC})")
    parser.add_argument("--live", action="store_true", help="LIVE TRADING (real money!). Default is dry run.")
    args = parser.parse_args()

    # Validate credentials
    if not KALSHI_API_KEY or not KALSHI_RSA_KEY_PATH:
        print("Error: Set KALSHI_API_KEY and KALSHI_RSA_KEY_PATH environment variables")
        return 1

    api = KalshiAPI(KALSHI_API_KEY, KALSHI_RSA_KEY_PATH)

    if args.discover:
        discover_markets(api)
        return 0

    if not args.ticker:
        print("Error: --ticker required (or use --discover to find markets)")
        return 1

    if args.live:
        print("\n" + "!"*60)
        print("  WARNING: LIVE TRADING MODE — REAL MONEY AT RISK")
        print("  Ctrl+C to stop and cancel all orders")
        print("!"*60)
        confirm = input("\nType 'YES' to confirm: ")
        if confirm != "YES":
            print("Aborted.")
            return 0

    mm = MarketMaker(
        api=api,
        ticker=args.ticker,
        spread_cents=args.spread,
        size=args.size,
        max_position=args.max_position,
        max_loss_cents=int(args.max_loss * 100),
        refresh_sec=args.refresh,
        live=args.live,
    )
    return mm.run()


if __name__ == "__main__":
    sys.exit(main())
