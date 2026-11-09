# TPPD BTC bot

Private. Master copy of the code that runs on the Lightsail server.

- `server/` - the server's `~/btc_bot` code as it runs (no .env, keys, data or logs)
- `deploy/` - systemd units, Caddyfile, crontab from the server

Snapshot taken 09 Nov 2026 12:55 IST. condor.py sha256 prefix: 4cf2b5df2905a527.
Secrets live only on the server in `~/btc_bot/.env` and are never committed.
