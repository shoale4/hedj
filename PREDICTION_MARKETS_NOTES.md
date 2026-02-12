# Prediction Market Notes - Train Review Sheet

## The Basics (if it comes up)

**Prediction market** = trade binary contracts on real-world events. YES token + NO token. Exactly one pays $1.00 at resolution. Price = market's implied probability.

**Polymarket** = crypto-native (Polygon/USDC), non-US only, open-ended markets on anything, UMA Oracle resolves outcomes. Think Uniswap for predictions.

**Kalshi** = CFTC-regulated US exchange, KYC required, structured series (BTC daily, GDP, CPI, Fed), traditional banking. Think NYSE for predictions.

## Three Ways to Make Money

### 1. Sportsbook Arbitrage (what Hedj does)
- Compare odds across FanDuel, DraftKings, etc.
- When implied probabilities sum to <100%, bet both sides = guaranteed profit
- One-shot: place bets, wait for game, collect
- Edge: speed (find it before lines move)

### 2. Prediction Market Arbitrage
- Same concept, cross-platform: buy YES cheap on Polymarket, NO cheap on Kalshi
- Or intra-platform: YES ask + NO ask < $1.00
- Reality: most "arbs" are illiquidity artifacts (no real seller at that price)
- Real engines need: WebSocket feeds, orderbook depth checks, sub-second execution

### 3. Market Making (the real play)
- Post limit orders on BOTH sides of a market (bid + ask)
- Buy YES at 48c from one person, buy NO at 49c from another = $0.97 for guaranteed $1.00
- You're not predicting outcomes, you're capturing the spread
- Revenue = spread capture + platform liquidity rewards

## Market Making Key Concepts

**Limit order** = "I'll buy at this price, put me in the queue" (you're a maker)
**Market order** = "fill me now at whatever price" (you're a taker)
**Spread** = gap between best bid and best ask. That gap is your profit.
**post_only** = guarantees your order doesn't cross the spread. Stays as a resting maker order. Cheaper fees.

**How both sides fill:**
Someone bearish sells YES into your bid. Later, someone bullish sells NO into your bid. You now hold YES+NO = guaranteed $1.00. You paid <$1.00. Profit.

**Inventory risk** = the #1 danger. If only one side fills, you're exposed. News breaks, price moves against you.

**Inventory skewing** = when you hold too much of one side, shift your quotes against your position to encourage fills that reduce exposure.

**Circuit breaker** = cancel all orders if price moves too fast. Prevents getting picked off on stale quotes.

## Why Daily/Weekly Series Are Better Than Long-Term

1. **Inventory risk capped by time** - worst case resolves tomorrow, not 10 months from now
2. **Faster capital turnover** - same $500 does more round trips
3. **Mean reversion** - BTC oscillates within the day, both sides fill. Long-term markets drift one direction = adverse selection
4. **No event risk surprise** - BTC price is continuous/observable. "Will X win election?" has a single binary shock moment
5. **Fresh orderbooks daily** - not competing with someone queued for 3 weeks

## Kalshi vs Polymarket for Market Making

**Kalshi (what I'm using):**
- US-legal, already have an account
- Simple REST API, RSA key auth
- Wider spreads = more profit per trade
- Less competition
- Daily BTC/ETH/GDP/CPI series = bounded risk
- Maker fee: 0.0175 * contracts * p * (1-p)

**Polymarket (what they're probably using):**
- Non-US only (VPN = ToS violation, fund seizure risk)
- Polygon blockchain, EIP-712 signing, USDC
- Zero maker fees
- $12M/year liquidity rewards (quadratic scoring, two-sided quotes earn 3x)
- Tighter spreads, more competition
- UMA Oracle = resolution risk (has been manipulated)

## What I Built

**Prediction arb scanner** (`prediction_arb.py`):
- Fetches Polymarket (Gamma API + CLOB orderbooks) and Kalshi (RSA auth)
- Detects intra-platform arbs (YES+NO < $1) and cross-platform (fuzzy market matching)
- First real scan: 106 cross-platform arbs found, most are illiquidity artifacts
- Runs every 5 min via GitHub Actions, alerts to Discord

**Kalshi market maker** (`kalshi_market_maker.py`):
- Discovery mode: scans BTC/ETH/GDP/CPI/Fed series, ranks by volume + spread
- Posts two-sided post_only limit orders every 30 seconds
- Inventory skewing, circuit breaker (10c move = cancel all), max loss kill switch
- Currently running live on GDP >3.0% market (6c spread, 324k volume)
- $0.04 net profit per round trip after maker fees

## Smart Questions to Ask Them

- Are you doing pure arb or market making? (arb is finding mispricing, MM is providing liquidity)
- How are you matching markets across platforms? (curated mapping vs fuzzy text?)
- Are you using WebSocket feeds or REST polling? (speed difference: ms vs seconds)
- How do you handle inventory risk on longer-dated contracts?
- What's your resolution risk strategy given UMA oracle manipulation?
- Are the liquidity rewards actually worth it or is spread capture the main revenue?
- What's your execution latency look like? (sub-second needed for arb, minutes fine for MM)
- How much capital are you deploying per market?

## Key Numbers to Know

- Polymarket: ~$12M/year in liquidity rewards, spreads averaged 4.5% in 2023 -> 1.2% in 2025
- Kalshi: 10 writes/sec basic tier, up to 400/sec premier
- Kalshi maker fee at 50c: ~0.4c/contract. Taker: ~1.75c/contract
- Polymarket maker fee: $0.00
- Profitable MM target: 1-5% return per trade, scale through frequency
- Starting capital: $5-10k for meaningful MM
- Top bot operators: ~$500 avg profit per trade, $700-800/day at peak on $10k capital
- Only 10-30% of bot operators achieve consistent profitability

## The One-Liner

"I built a sports betting arb tool that compares odds across 12 sportsbooks, then extended it to prediction markets - cross-platform arb detection for Polymarket and Kalshi, and a market maker that posts two-sided quotes on Kalshi's daily contracts. It's running live right now on a GDP market."
