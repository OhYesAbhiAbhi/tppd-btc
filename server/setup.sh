#!/usr/bin/env bash
# Installs or updates the TPPD BTC bot on Ubuntu. Safe to re-run: keeps .env and data/.
set -e
cd "$HOME/btc_bot"
echo "== TPPD BTC bot setup in $PWD (user $USER) =="

if ! python3 -m venv --help >/dev/null 2>&1 || ! dpkg -s python3-venv >/dev/null 2>&1; then
  echo "-- installing python3-venv"
  sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv >/dev/null
fi
[ -x .venv/bin/python ] || python3 -m venv .venv
echo "-- installing Python packages"
.venv/bin/pip install -q --upgrade pip >/dev/null
.venv/bin/pip install -q -r requirements.txt

echo "-- running offline tests"
.venv/bin/python test_bot.py 2>&1 | tail -3

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "-- created .env (paper mode, real prices, no orders)"
fi

# Reuse Telegram settings from the Nifty bot's .env, if one exists (values are never printed).
.venv/bin/python - <<'PY'
import glob, os, re
home = os.path.expanduser("~")
mine = os.path.join(home, "btc_bot", ".env")
def read(p):
    out = {}
    for line in open(p, errors="ignore"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out
cur = read(mine)
if cur.get("TELEGRAM_BOT_TOKEN") and cur.get("TELEGRAM_CHAT_ID"):
    print("-- Telegram already configured")
    raise SystemExit
cands = [p for p in glob.glob(f"{home}/.env") + glob.glob(f"{home}/*/.env") + glob.glob(f"{home}/*/*/.env")
         if "btc_bot" not in p]
for p in cands:
    env = read(p)
    tok = next((v for k, v in env.items() if re.match(r"TELEGRAM\w*TOKEN", k) and v), None)
    chat = next((v for k, v in env.items() if re.match(r"TELEGRAM\w*CHAT\w*", k) and v), None)
    if tok and chat:
        chat = chat.split(",")[0].strip()
        lines = open(mine).read().splitlines()
        def put(key, val):
            for i, l in enumerate(lines):
                if l.startswith(key + "="):
                    lines[i] = f"{key}={val}"
                    return
            lines.append(f"{key}={val}")
        put("TELEGRAM_BOT_TOKEN", tok)
        put("TELEGRAM_CHAT_ID", chat)
        open(mine, "w").write("\n".join(lines) + "\n")
        print(f"-- Telegram settings copied from {p}")
        break
else:
    print("-- No Telegram settings found in other .env files; alerts will only go to the log")
PY
chmod 600 .env

echo "-- dry-run scan against live Delta market data (never trades)"
.venv/bin/python bot.py --once || echo "!! dry run failed: see message above"

echo "-- installing background service"
sudo tee /etc/systemd/system/tppd-btc.service >/dev/null <<UNIT
[Unit]
Description=TPPD BTC options-writing bot (Delta Exchange)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/btc_bot
ExecStart=$HOME/btc_bot/.venv/bin/python bot.py
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable tppd-btc >/dev/null 2>&1
sudo systemctl restart tppd-btc
sleep 8
echo
systemctl is-active tppd-btc && echo "== Bot is RUNNING ==" || echo "!! service not running"
grep -E "^(MODE|DELTA_ENV|HEDGE|RUNNER)=" .env | sed 's/ *#.*//'
echo "server public IP (whitelist this on Delta later): $(curl -s -m 5 https://api.ipify.org)"
echo "-- last log lines:"
tail -n 8 data/bot.log 2>/dev/null || true
