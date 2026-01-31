# Hedj - Claude Code Context Document

## What is Hedj?
Hedj is a sports betting arbitrage and line tracking tool. It compares odds across 12+ sportsbooks to find:
1. **Arbitrage opportunities** - Guaranteed profit by betting both sides at different books
2. **Line movements** - Track how odds change over time to spot sharp money
3. **Best odds** - Find the best price for any bet across all books

The primary user is based in **Illinois** and manually places arb bets. The system is optimized for alerting on fresh, actionable arbs via Discord.

## Architecture Overview

```
The Odds API → Python CLI → CSV files → GitHub → Next.js frontend (via raw GitHub URLs)
                                          ↓
                              GitHub Actions → arb_alerts.py → Discord webhook
```

## Tech Stack

### Backend (Python)
- **Location:** `/hedj/`
- **Entry point:** `python -m hedj.cli --config config.json`
- **Key files:**
  - `cli.py` - Main CLI, orchestrates fetching and storage. Supports `--props`, `--cleanup`, `--verbose` flags.
  - `providers/example_provider.py` - The Odds API integration. Handles regular sports (h2h, spreads, totals markets) and has unused futures/outrights support.
  - `storage.py` - CSV read/write, history management, latest snapshot generation with movement calculations
  - `models.py` - Data structures (EventOdds, MarketOdds, OutcomeOdds, OddsRow, LatestOddsRow, PlayerPropOdds, PlayerPropRow)

### Frontend (Next.js)
- **Location:** `/web/`
- **Framework:** Next.js 16.1.1 + React 19 + TypeScript
- **Styling:** Tailwind CSS 4
- **Deployment:** Vercel (auto-deploys from `main` branch)
- **Key pages:**
  - `/` - Dashboard with games, arbs, line movements
  - `/arbitrage` - Dedicated arbitrage scanner with sport grouping
  - `/feed` - Unified activity feed with games, movements, news, weather
  - `/my-tracker` - Bet tracking page (Pro-only, shows 404 for non-Pro)

### Data Storage
- **CSV-based** (no database for odds data)
- `data/history/odds_history.csv` - Append-only historical snapshots
- `data/latest/latest.csv` - Computed latest with movement calculations (opening/current price, line movement, last_updated)
- `data/raw/*.json` - Raw API response backups
- `data/alerted_arbs.json` - Tracks which arbs have already been sent to Discord (prevents duplicate alerts, auto-clears after 24h). Gitignored.
- **Vercel KV** - Promo codes, waitlist emails (runtime data)

### External APIs & Services
- **The Odds API** (`api.the-odds-api.com/v4/sports`) - Primary odds data source (paid, credits-based)
- **Anthropic Claude** - AI game analysis (`/api/analyze`)
- **ESPN News API** - Sports news integration for feed
- **Discord Webhook** - Arb alert notifications (stored as GitHub Secret `DISCORD_WEBHOOK_URL`)

## Scripts

### `scripts/arb_alerts.py`
Arb detection and Discord alerting. Runs after every odds update (integrated into the odds feed workflow).

**Key features:**
- Filters to Illinois-legal books only: FanDuel, DraftKings, BetMGM, Caesars, PointsBet, BetRivers, ESPN BET
- **Freshness filter:** Skips odds older than 2 hours (`MAX_ODDS_AGE_HOURS = 2`) - stale odds have likely changed
- **Deduplication:** Tracks alerted arbs in `data/alerted_arbs.json` by key `event_id:market:book1:book2`. Only sends new arbs. Auto-clears entries after 24h.
- **Deep links:** Book names in Discord embeds are clickable links to sportsbook websites (opens apps on mobile)
- **Freshness indicator:** Shows odds age with color coding (green <1hr, yellow 1-24hr, red >24hr)
- ROI filter: 0.5% minimum, 15% maximum (above 15% is likely bad data)
- Total line matching: Groups over/under by line value to prevent false arbs
- Stakes calculated for $100 total

### `scripts/tight_line_summary.py`
Morning alert for pre-arb candidates. Finds games where implied probability sum is between 100% and 102% (close to arb but not quite there yet).

**Key features:**
- Only looks at games in next 24 hours
- Same 2-hour freshness filter as arb alerts
- Groups results by sport
- Shows gap-to-arb percentage with indicators (flame <0.5%, eyes <1%, chart otherwise)
- Includes sportsbook deep links

### `scripts/update_feed.py`
Older script for updating the odds feed (predates CLI refactor).

## GitHub Actions Workflows

### `.github/workflows/update_odds_feed.yml`
**Primary workflow.** Runs every 40 minutes.
```
Schedule: 3 cron entries that stagger across hours
  - "0 */2 * * *"     (even hours at :00)
  - "40 */2 * * *"    (even hours at :40)
  - "20 1-23/2 * * *" (odd hours at :20)
```
**Steps:**
1. Checkout repo
2. Install Python deps
3. Create config from template + inject API key from secrets
4. Run `python -m hedj.cli --config config.json`
5. **Run `python scripts/arb_alerts.py`** (arb check runs immediately after fresh data)
6. Commit and push CSV changes

### `.github/workflows/morning-summary.yml`
Runs daily at 8am EST (13:00 UTC). Executes `scripts/tight_line_summary.py` to send morning tight-line alerts to Discord.

### `.github/workflows/cleanup_history.yml`
Weekly cleanup of old history data (keeps 7 days).

### `.github/workflows/twitter_alerts.yml`
Twitter/X alerts (may be inactive/unused).

### `.github/workflows/update_player_props.yml`
Player props fetching (may be inactive - props not currently fetched in main workflow).

## API Cost Management

**The Odds API pricing:** Credits based on regions x markets per sport
- **Current config:** 1 region (us) x 3 markets (h2h, spreads, totals) = 3 credits per sport
- **6 sports** x 3 credits = **18 credits per run**
- Every 40 min = 36 runs/day = 648 credits/day = **~19,440/month**
- **$25/mo plan = 20,000 credits** (very close to limit!)

**Cost optimizations implemented:**
1. Every 40 minutes (was hourly, then briefly considered 30-min)
2. Removed soccer leagues (EPL, La Liga, UCL) to save credits
3. Removed futures (Super Bowl Winner, NBA Championship, Stanley Cup) - not real arbs
4. Added WNBA (same cost, better arb potential)
5. Made markets configurable in `config.json`
6. API quota tracking (logs remaining credits, warns below 1000)
7. `--cleanup` CLI flag to remove old history data
8. Arb alerts run in same workflow as data fetch (no separate cron = no wasted runs)

**WARNING:** Adding any new sport costs +3 credits/run = +3,240/month. Budget is ~560 credits/month of headroom.

## Config File (`config.json`)

**Note:** `config.json` is gitignored and contains the real API key. `config.example.json` is the template.

Current sports:
- `americanfootball_nfl` - NFL (active through playoffs/Super Bowl)
- `basketball_nba` - NBA
- `basketball_ncaab` - NCAAB (college basketball)
- `basketball_wnba` - WNBA (season May-October, less efficient market = more arbs)
- `icehockey_nhl` - NHL
- `mma_mixed_martial_arts` - MMA/UFC

Current books (12):
`fanduel, draftkings, betmgm, caesars, pointsbetus, bet365, betonlineag, betrivers, unibet, wynnbet, superbook, espnbet`

## Critical Files for Arbitrage

### `web/lib/arbitrage.ts`
Core arbitrage detection algorithm (frontend). Key functions:
- `findAllArbitrage(games)` - Main entry, returns all arb opportunities
- `findArbitrageOpportunities(game)` - Per-game arb detection
- `checkTwoWayArbitrage()` - Compares two odds for arb (moneyline, spread, total)
- `checkThreeWayArbitrage()` - Three-way markets (soccer with draw)

**CRITICAL BUG FIXES (Jan 2026):**
1. **Spread matching:** Home spread at line X must match with away spread at line -X (e.g., home -3.5 matches away +3.5)
2. **Total matching:** Over/under must be on the SAME line. Was incorrectly pairing Over 6.5 from FanDuel with Under 5.5 from DraftKings as an "arb" - this is INVALID because if the total lands at 6, you lose BOTH bets! Fixed by grouping totals by line value using a Map.

### `web/lib/data.ts`
Data fetching and parsing:
- `getLatestOdds()` - Fetches latest.csv (from GitHub raw URL in prod, local file in dev)
- `getGameOdds()` - Parses CSV into GameOdds objects. **Filters out futures market type** since futures have no home/away structure.
- `getLineMovements()` - Calculates price/line movements from history
- `getLastUpdated()` - Gets most recent update timestamp from data
- `findBestOdds()` - Find best odds across books for a given outcome

**IMPORTANT:** The `getGameOdds()` function has a safety check: `if (snapshot.marketType in game.markets)` before pushing to avoid crashes on unknown market types. The `"futures"` market type is explicitly skipped.

### `web/lib/types.ts`
Core TypeScript types:
- `Sport` = `"NFL" | "NBA" | "NCAAB" | "WNBA" | "MLB" | "NHL" | "MMA" | "Soccer"`
- `OddsSnapshot` - Raw parsed CSV row (marketType includes "futures")
- `GameOdds` - Grouped by event with moneyline/spread/total markets
- `BookOdds` - Single book's odds for a market
- `LineMovement` - Price/line change record
- `FeedItem` - Union type for game/movement/news feed items

### `web/lib/state-legality.ts`
Maps US states to legal sportsbooks. Used to filter arbs/odds display to only show books available in the user's state. Illinois-legal books: FanDuel, DraftKings, BetMGM, Caesars, PointsBet, BetRivers, ESPN BET.

### `web/components/ArbitrageClient.tsx`
Main arbitrage page component. Orchestrates:
- Sport filtering via FloatingContextBar
- State-based book filtering via `isBookAvailable()`
- Arb count calculation (filtered by state)
- Modal management (waitlist, redeem code)
- Launch promo banner

### `web/components/ArbitrageFinderBySport.tsx`
Displays arb opportunities grouped by sport. Includes:
- Timestamp display with `formatRelativeTime()`
- Pro-only refresh button (uses server action `refreshArbitrageData()` + `router.refresh()`)
- Visual "Refreshed!" feedback

### `web/components/ArbCalculator.tsx`
Manual arb calculator for verifying changed odds. Features:
- 2-way and 3-way market support
- Custom labels for each side
- Total stake presets ($50, $100, $250, $500, $1000)
- Shows: ROI%, implied probability sum, stakes per leg, guaranteed profit
- Collapsible accordion UI

### `web/app/actions.ts`
Server action for cache invalidation:
```typescript
"use server";
export async function refreshArbitrageData() {
  revalidatePath("/arbitrage");
  revalidatePath("/");
}
```

## Monetization

- **Free tier:** See bottom 2 arbs per sport, basic features
- **Pro tier:** All arbs, AI analysis, refresh button, bet tracker
- **Promo codes:** Stored in Vercel KV, admin API at `/api/admin/codes`
- **Admin secret:** Check `ADMIN_SECRET` env var (default: "da-bears")
- **Launch promo:** "DABEARSCHAMPS26" pre-filled in redeem modal
- **Waitlist modal:** For maxed-out promo codes and feature interest

## Pro Features

Features gated behind `usePro()` hook (checks localStorage key `hedj_pro_status`):
- Full arbitrage list (free users see bottom 2 per sport)
- "Track Bets" button on arb cards
- Manual refresh button
- `/my-tracker` bet tracking page (shows 404 for non-Pro)

## Common Issues & Fixes

### Build Fails on Vercel
**Common cause:** Adding a new Sport type but not updating all `Record<Sport, string>` objects.
**Files that need SPORT_EMOJI/SPORTS array updates when adding a sport:**
- `components/ArbitrageFinder.tsx`
- `components/ArbitrageFinderBySport.tsx`
- `components/FloatingContextBar.tsx`
- `components/BookLeaderboard.tsx`
- `components/feed/GameFeedCard.tsx`
- `lib/types.ts` (Sport type)
- `lib/teamLogos.ts` (Sport type + teams)
- `lib/espn-news.ts` (SportType + endpoints + team lists)
- `lib/news-sources.ts` (SportType + team lists)
- `lib/news.ts` (sport field)

### Fake/Impossible Arbitrage (50%+ profit)
**Cause:** Data quality issues or incorrect spread/total matching
**Fix:**
1. Spread matching uses complementary lines (home X with away -X)
2. Total matching groups by line value (Over 6.5 only matches Under 6.5)
3. Added 15% max profit filter in `findAllArbitrage()`

### Stale Arb Alerts
**Cause:** Old odds in CSV that have since changed at the sportsbook
**Fix:** `MAX_ODDS_AGE_HOURS = 2` - any odds with `last_updated` older than 2 hours are skipped before arb detection

### Duplicate Discord Alerts
**Cause:** Same arb persisting across multiple data fetches
**Fix:** Deduplication via `data/alerted_arbs.json` keyed by `event_id:market:book1:book2`. Auto-clears after 24h.

### History CSV Too Large (>2MB)
**Cause:** Unbounded append-only history
**Fix:** Weekly cleanup workflow, `--cleanup` CLI flag. Note: Next.js data cache warns on items >2MB but doesn't fail.

### Git Push Rejected
**Cause:** Concurrent GitHub Actions updates (odds feed commits)
**Fix:** `git pull --rebase origin main && git push` (already in workflow)

### API Quota Running Out
**Cause:** Too many sports or too frequent fetches
**Fix:** Currently at 18 credits/run x 36 runs/day = 19,440/month (under 20k limit with ~560 headroom)

## Testing Locally

```bash
# Fetch fresh odds (requires config.json with real API key)
python3 -m hedj.cli --config config.json --verbose

# Run arb alerts locally (reads from data/latest/latest.csv)
DISCORD_WEBHOOK_URL="your_url" python3 scripts/arb_alerts.py

# Run morning summary locally
DISCORD_WEBHOOK_URL="your_url" python3 scripts/tight_line_summary.py

# Run web dev server (reads local CSV files instead of GitHub)
cd web && npm run dev

# Build web for production
cd web && npm run build

# Cleanup old data (keep last 7 days)
python3 -m hedj.cli --config config.json --cleanup --cleanup-days 7
```

## Vercel Environment Variables

Required for production:
- `KV_REST_API_URL` - Vercel KV connection
- `KV_REST_API_TOKEN` - Vercel KV auth
- `ANTHROPIC_API_KEY` - For AI analysis
- `ADMIN_SECRET` - Admin API authentication

## GitHub Secrets

- `ODDS_API_KEY` - The Odds API key
- `DISCORD_WEBHOOK_URL` - Discord webhook for arb alerts

## Sports Covered

| Sport | API Key | Status | Notes |
|-------|---------|--------|-------|
| NFL | `americanfootball_nfl` | Active | Through playoffs/Super Bowl |
| NBA | `basketball_nba` | Active | Regular season + playoffs |
| NCAAB | `basketball_ncaab` | Active | College basketball |
| WNBA | `basketball_wnba` | Active | Season May-Oct. Less efficient market = more arbs |
| NHL | `icehockey_nhl` | Active | Regular season + playoffs |
| MMA | `mma_mixed_martial_arts` | Active | UFC events |
| MLB | `baseball_mlb` | Disabled | Offseason. Re-enable late March |
| Soccer | Various | Removed | EPL/La Liga/UCL removed to save API credits |
| Futures | Various | Removed | Not real arbs, just value spots. Removed to save credits |

## Sportsbook Deep Links

Used in Discord alerts so user can quickly open the right app:
```
fanduel    → https://sportsbook.fanduel.com
draftkings → https://sportsbook.draftkings.com
betmgm     → https://sports.betmgm.com
caesars    → https://sportsbook.caesars.com
pointsbetus → https://pointsbet.com
betrivers  → https://il.betrivers.com
espnbet    → https://espnbet.com
```

## Recent Changes (Jan 2026)

### Latest Session (Jan 31, 2026)
1. **Discord alert improvements:** Added deep links to sportsbook apps, odds freshness indicator with color coding, deduplication to prevent repeat alerts
2. **Morning tight-line summary:** New script + workflow that runs at 8am EST, finds games close to arb (<2% implied prob gap)
3. **Consolidated arb alerts into odds workflow:** Removed separate 15-min arb alert cron (was pointless since data only updates every 40 min). Now runs right after fresh data fetch.
4. **Added WNBA:** Less efficient market = more arb opportunities. Season May-Oct.
5. **Removed futures:** Super Bowl/NBA Championship/Stanley Cup futures weren't real arbs. Saved 3 API credits/run.
6. **Stale odds filter:** Skip odds older than 2 hours in arb detection (`MAX_ODDS_AGE_HOURS = 2`). Prevents alerting on stale arbs that have already changed.
7. **Build fix:** Added futures market type to TypeScript types, skip futures in `getGameOdds()` parsing to prevent crash.

### Earlier Session (Jan 24-25, 2026)
1. **Fixed critical total line bug** - Was matching Over 6.5 vs Under 5.5 as valid arbs
2. **Added timestamp display** - Shows "X min ago" data freshness
3. **Added Pro-only refresh button** - `revalidatePath()` + `router.refresh()` with visual feedback
4. **Discord arb alerts** - Posts to Discord when Illinois arbs found
5. **Increased fetch frequency** - Every 40 minutes (was hourly)
6. **Removed soccer leagues** - EPL, La Liga, UCL removed to stay under API budget
7. **Added manual arb calculator** - `ArbCalculator.tsx` for verifying changed odds

### Earlier Jan 2026
1. Fixed spread arbitrage bug (wrong line matching)
2. Added 15% profit cap filter
3. API quota logging
4. Weekly cleanup workflow
5. State selector + book legality filtering
6. NCAAB support
7. Launch promo banner
