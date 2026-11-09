#!/bin/bash
# One-time setup of the partner platform. Run AFTER `~/deploy main` has installed the phase-2 code:
#     sudo bash ~/tppd-repo/deploy/partner/install.sh
# Safe to run again. Does not touch tppd-condor, your .env or your trade data.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
H=/home/ubuntu; BOT=$H/btc_bot; PY=$BOT/.venv/bin/python; SRC="$(cd "$(dirname "$0")" && pwd)"; PART=$H/btc_partners
[ -f "$BOT/partner_portal.py" ] || { echo "run ~/deploy main first (partner code not installed)"; exit 1; }

echo "1/8 packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sqlite3 acl >/dev/null
sudo -u ubuntu "$BOT/.venv/bin/pip" install -q "cryptography>=41"

echo "2/8 partner group and read-only access to the code (never to your .env or data)"
getent group tppd-partners >/dev/null || groupadd --system tppd-partners
setfacl -m g:tppd-partners:x "$H"
setfacl -R -m g:tppd-partners:rX "$BOT"
setfacl -R -d -m g:tppd-partners:rX "$BOT"
for f in "$BOT"/.env* "$BOT"/data* "$BOT"/RELEASE; do [ -e "$f" ] && setfacl -R -x g:tppd-partners "$f" 2>/dev/null || true; done
for d in "$BOT"/data*; do [ -d "$d" ] && setfacl -R -d -x g:tppd-partners "$d" 2>/dev/null || true; done
sudo -u nobody -g tppd-partners test -r "$BOT/.env" 2>/dev/null && { echo "STOP: .env readable by partners"; exit 1; } || true

echo "3/8 vault key (root only)"
mkdir -p /etc/tppd && chmod 755 /etc/tppd
"$PY" "$BOT/partner_vault.py" init /etc/tppd/vault.key /etc/tppd/vault.pub
chmod 600 /etc/tppd/vault.key && chmod 644 /etc/tppd/vault.pub

echo "4/8 helpers and the one sudo rule the portal needs"
install -m 755 -o root -g root "$SRC/tppd-partner-activate" /usr/local/sbin/tppd-partner-activate
install -m 755 -o root -g root "$SRC/tppd-partner-deactivate" /usr/local/sbin/tppd-partner-deactivate
printf 'ubuntu ALL=(root) NOPASSWD: /usr/local/sbin/tppd-partner-activate *, /usr/local/sbin/tppd-partner-deactivate *\n' > /etc/sudoers.d/tppd-partners.tmp
chmod 440 /etc/sudoers.d/tppd-partners.tmp && visudo -cf /etc/sudoers.d/tppd-partners.tmp >/dev/null && mv /etc/sudoers.d/tppd-partners.tmp /etc/sudoers.d/tppd-partners

echo "5/8 partner folder"
mkdir -p "$PART" && chown ubuntu:ubuntu "$PART" && chmod 711 "$PART"

echo "6/8 services"
install -m 644 "$SRC/tppd-partner@.service" /etc/systemd/system/tppd-partner@.service
cat > /etc/systemd/system/tppd-partner-portal.service <<EOF
[Unit]
Description=TPPD partner portal (sign-up, partner pages, master page)
After=network-online.target
[Service]
User=ubuntu
WorkingDirectory=$BOT
ExecStart=$PY $BOT/partner_portal.py serve
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/tppd-partner-results.service <<EOF
[Unit]
Description=TPPD partner results bot (Telegram)
After=network-online.target
[Service]
User=ubuntu
WorkingDirectory=$BOT
EnvironmentFile=$PART/results_bot.env
ExecStart=$PY $BOT/partner_results_bot.py
Restart=always
RestartSec=20
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now tppd-partner-portal >/dev/null 2>&1

echo "7/8 web address: /p/* and /master on the existing site"
CF=/etc/caddy/Caddyfile
if ! grep -q "127.0.0.1:8096" "$CF"; then
    cp "$CF" "$CF.bak-partners"
    "$PY" - "$CF" <<'PYX'
import sys
p = sys.argv[1]; s = open(p).read()
block = "\thandle /p/* {\n\t\treverse_proxy 127.0.0.1:8096\n\t}\n\thandle /master* {\n\t\treverse_proxy 127.0.0.1:8096\n\t}\n"
i = s.index("3-106-111-8.sslip.io {") + len("3-106-111-8.sslip.io {\n")
open(p, "w").write(s[:i] + block + s[i:])
PYX
    if caddy validate --config "$CF" --adapter caddyfile >/dev/null 2>&1; then systemctl reload caddy
    else cp "$CF.bak-partners" "$CF"; echo "Caddy check failed: web routes NOT added (old file restored)"; fi
fi

echo "8/8 master password and Telegram results bot"
if ! sudo -u ubuntu "$PY" -c "import sqlite3,sys;c=sqlite3.connect('$PART/portal.db');sys.exit(0 if c.execute(\"select 1 from settings where k='m_hash'\").fetchone() else 1)" 2>/dev/null; then
    cd "$BOT" && sudo -u ubuntu env PARTNER_ROOT="$PART" "$PY" partner_portal.py set-master-password
else echo "   master password already set (reset: cd ~/btc_bot && .venv/bin/python partner_portal.py set-master-password)"; fi
if [ -s "$PART/results_bot.env" ]; then systemctl enable --now tppd-partner-results >/dev/null 2>&1; RB=$(systemctl is-active tppd-partner-results)
else RB="not started: put PARTNER_TG_TOKEN=<token from @BotFather> in $PART/results_bot.env, then: sudo systemctl enable --now tppd-partner-results"; fi
command -v systemd-creds >/dev/null && KEYS="encrypted service credentials" || KEYS="root-only key files (no systemd-creds on this Ubuntu)"
echo
echo "PARTNERS-OK"
echo "  portal: $(systemctl is-active tppd-partner-portal)   results bot: $RB"
echo "  master page: https://3-106-111-8.sslip.io/master"
echo "  partner keys will be stored as: $KEYS"
echo "  your bot: $(systemctl is-active tppd-condor) (untouched)"
