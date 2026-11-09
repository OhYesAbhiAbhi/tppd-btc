# TPPD BTC bot

Private. Master copy of the code that runs on the Lightsail server.

- `server/` - the server's `~/btc_bot` code as it runs (no .env, keys, data or logs)
- `deploy/` - systemd units, Caddyfile and the BTC crontab line, as on the server
- `CLAUDE.md` - working rules: branches + pull requests, tests on GitHub, release tags, install window 11:35-22:25 IST

Snapshot taken 09 Nov 2026 12:55 IST. condor.py sha256 prefix: 4cf2b5df2905a527.
Secrets live only on the server in `~/btc_bot/.env` and are never committed.
