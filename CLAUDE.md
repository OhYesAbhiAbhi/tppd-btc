# TPPD BTC — rules for working in this repo

This repo is the master copy of the code that trades real money on Delta Exchange India,
running on Abhineet's AWS Lightsail server (3.106.111.8, user `ubuntu`, code in `~/btc_bot`).

## Layout
- `server/` — the server's `~/btc_bot` code, file for file. `condor.py` is the live nightly strangle
  (service `tppd-condor`). `btc_pnl.py` = P&L calendar page (`tppd-pnl`), `collector.py` = market data
  recorder (`tppd-collector`), `thursday_paper.py` = paper tracker (`tppd-thursday`), `vol_ping.py` = hourly
  cron. `bot.py` = the old intraday bot (`tppd-btc`).
- `deploy/` — systemd units, Caddyfile and the BTC crontab line, as on the server.

## Hard rules
1. **Never commit secrets.** No `.env`, API keys/secrets, Telegram tokens, P&L page keys, trade data,
   logs or state files. `.env.example` holds names only.
2. **Every change goes through a branch + pull request**, with tests passing on GitHub (Actions) before
   merge. Never push straight to `main` except repo housekeeping.
3. **Tests first.** Each behaviour change ships with tests; the full suite in `server/` must stay green
   (`cd server && python -m unittest test_condor test_btc_pnl test_collector test_thursday test_bot`).
4. **Install window:** anything that restarts `tppd-condor` is installed only between **11:35 and 22:25
   IST**, and never while a position is open. Deploy tooling must refuse outside that window.
5. **Releases are tags** (`r1`, `r2`, …) on `main`. The server runs a tag, never a branch.
6. Plain-language summaries for Abhineet: what changes for trading, what to paste, what he should see.

## Current live settings (owner's .env, not in git)
Strangle, 200 lots, $180 stop ($0.90/lot), vol switch, dampened entry 22:30–11:30 IST, 10-min dip confirm,
2% drift exit, skip Thursdays, hot-night skip (`CONDOR_VOL_SKIP_RV`, 24h vol > 50%), Fed-day skip (`CONDOR_FED_SKIP`).
