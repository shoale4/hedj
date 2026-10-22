# Hedj trading desk (hedj.app)

Private dashboard for the Kalshi strategy work in this repo.

- **Today**: picks the paper trader (`scripts/paper_trader.py`) logged for today's games
- **Results**: paper P&L by strategy against the backtest
- **Strategies**: the strategy board (data from `scripts/build_dashboard_data.py`, in `data/board/`)

Picks are read at request time from `paper/picks.csv` on GitHub (refreshed every
2 minutes), so the site doesn't need to rebuild when the bot commits.

## Environment

| Variable | What |
|---|---|
| `DASHBOARD_PASSWORD` | the sign-in password |
| `AUTH_SECRET` | 32+ random characters used to sign the session cookie |
| `PICKS_URL` | optional override for where picks.csv is read from |

```bash
npm run dev     # needs the two variables above in .env.local
```
