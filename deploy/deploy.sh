#!/bin/bash
# Install a version of the BTC code from GitHub onto this server.
#
#   ~/deploy r3          install release r3 (a tag), or a commit id, or: main
#   ~/deploy --check     is the server running exactly what GitHub has? (changes nothing)
#   ~/deploy --status    which release is installed, service states
#
# Safety: refuses when tppd-condor would restart outside 11:35-22:25 IST or while it holds a position;
# runs the full test suite on this server first; backs up every file it replaces; never touches .env or data.
set -u
[ -c /dev/null ] || { echo "DEPLOY STOPPED - /dev/null is damaged. Repair: sudo rm -f /dev/null && sudo mknod -m 666 /dev/null c 1 3"; exit 1; }
if [ -z "${DEPLOY_COPY:-}" ]; then                       # run from a copy: the checkout below may change this file
    c=$(mktemp /tmp/deploy.XXXXXX); cp "$0" "$c"; DEPLOY_COPY=1 exec bash "$c" "$@"
fi
BOT="${DEPLOY_BOT:-$HOME/btc_bot}"; REPO="${DEPLOY_REPO:-$HOME/tppd-repo}"; PY="${DEPLOY_PY:-$BOT/.venv/bin/python}"
BACKUPS="${DEPLOY_BACKUPS:-$HOME/btc_releases}"; SYSTEMCTL="${DEPLOY_SYSTEMCTL:-sudo systemctl}"
NOW_HM="${DEPLOY_NOW_HM:-$(TZ=Asia/Kolkata date +%H%M)}"
SELF="${DEPLOY_SELF:-$HOME/deploy}"
fail() { echo "DEPLOY STOPPED - $*  (nothing was changed)"; exit 1; }
say() { echo "  $*"; }

cd "$REPO" 2>/dev/null || fail "no repo at $REPO"
git fetch -q --tags --force origin 2>/dev/null || fail "cannot reach GitHub (check the deploy key)"

if [ "${1:-}" = "--restart-partners" ]; then
    for u in $($SYSTEMCTL list-units --type=service --state=active --no-legend 'tppd-partner@*' 2>/dev/null | awk '{print $1}'); do
        slug=${u#tppd-partner@}; slug=${slug%.service}
        if "$PY" -c "import json,sys;sys.exit(1 if (json.load(open(sys.argv[1])) or {}).get('open') else 0)" "$HOME/btc_partners/$slug/data/state.json" 2>/dev/null; then
            $SYSTEMCTL restart "$u" && say "restarted $slug"; else say "$slug: trade open, try later"; fi
    done; exit 0
fi
if [ "${1:-}" = "--status" ]; then
    echo "Installed: $(cat "$BOT/RELEASE" 2>/dev/null || echo 'unknown (before deploy tooling)')"
    for s in tppd-condor tppd-pnl tppd-collector tppd-thursday tppd-btc tppd-partner-portal tppd-partner-results; do say "$s: $($SYSTEMCTL is-active $s 2>/dev/null)"; done
    exit 0
fi

REF="${1:-}"; [ -n "$REF" ] || { echo "usage: ~/deploy <release e.g. r2 | commit | main>   or   ~/deploy --check"; exit 1; }
CHECK=0; [ "$REF" = "--check" ] && { CHECK=1; REF=$(cat "$BOT/RELEASE" 2>/dev/null | awk '{print $2}'); REF=${REF:-main}; }

# the first commit is the 9 Nov 2026 server snapshot: release r1 (created here once; this server can push tags)
if [ -z "$(git tag -l 'r*')" ]; then
    root=$(git rev-list --max-parents=0 origin/main | tail -1)
    git tag -a r1 "$root" -m "r1: server snapshot 9 Nov 2026" && git push -q origin r1 2>/dev/null
fi

git checkout -q --detach "origin/$REF" 2>/dev/null || git checkout -q --detach "$REF" 2>/dev/null \
    || fail "unknown version '$REF' (see: git -C ~/tppd-repo tag)"
SHA=$(git rev-parse --short=10 HEAD)

CHANGED=""
for f in "$REPO"/server/*.py; do
    n=$(basename "$f"); cmp -s "$f" "$BOT/$n" || CHANGED="$CHANGED $n"
done
if [ "$CHECK" = 1 ]; then
    if [ -z "$CHANGED" ]; then echo "CHECK-OK: server code matches $REF ($SHA)"; else
        echo "DIFFERENT from $REF ($SHA):$CHANGED"; fi
    exit 0
fi
[ -n "$CHANGED" ] || { echo "ALREADY-INSTALLED: server code already matches $REF ($SHA)"; exit 0; }

# which services read the changed files
SVC=""
add() { case " $SVC " in *" $1 "*) ;; *) SVC="$SVC $1";; esac; }
for n in $CHANGED; do
    case "$n" in
        test_*) ;;
        condor.py) add tppd-condor; add tppd-thursday;;
        config.py|strategy.py|delta_client.py|bot.py) add tppd-condor; add tppd-thursday; add tppd-collector; add tppd-btc;;
        btc_pnl.py) add tppd-pnl;;
        collector.py) add tppd-collector;;
        thursday_paper.py) add tppd-thursday;;
        partner_portal.py|partner_results.py|partner_vault.py) add tppd-partner-portal; add tppd-partner-results;;
        partner_results_bot.py) add tppd-partner-results;;
        *) ;;
    esac
done
ACTIVE=""
for s in $SVC; do [ "$($SYSTEMCTL is-active $s 2>/dev/null)" = "active" ] && ACTIVE="$ACTIVE $s"; done

case " $ACTIVE " in *" tppd-condor "*)
    [ "$NOW_HM" -ge 1135 ] && [ "$NOW_HM" -lt 2225 ] || fail "this restarts the trading bot; install between 11:35 and 22:25 IST"
    "$PY" - "$BOT/data_condor/state.json" <<'PYX' || fail "the trading bot holds a position; install after it closes"
import json, sys
try:
    sys.exit(1 if (json.load(open(sys.argv[1])) or {}).get("open") else 0)
except FileNotFoundError:
    sys.exit(0)
PYX
;; esac

# libraries the version needs (adds missing ones; doesn't touch running bots)
PIP="${DEPLOY_PIP:-$(dirname "$PY")/pip}"
if [ -f "$REPO/server/requirements.txt" ] && ! cmp -s "$REPO/server/requirements.txt" "$BOT/.requirements.installed"; then
    "$PIP" install -q -r "$REPO/server/requirements.txt" > /dev/null 2>&1 || fail "could not install the Python libraries it needs"
    cp "$REPO/server/requirements.txt" "$BOT/.requirements.installed"
fi

# test exactly the version being installed: its own code and its own tests
T=$(mktemp -d); cp "$REPO"/server/*.py "$T"/
TESTS=$(cd "$T" && ls test_*.py | sed 's/\.py$//' | tr '\n' ' ')
( cd "$T" && env -u CREDENTIALS_DIRECTORY BOT_ENV_FILE=/nonexistent "$PY" -m unittest $TESTS ) > "$T/tests.log" 2>&1 \
    || { tail -15 "$T/tests.log"; fail "tests failed on this server"; }
NT=$(grep -oE 'Ran [0-9]+ tests' "$T/tests.log" | awk '{s+=$2} END {print s}')

B="$BACKUPS/before-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$B"
for n in $CHANGED; do [ -f "$BOT/$n" ] && cp -p "$BOT/$n" "$B/"; done
cat "$BOT/RELEASE" > "$B/RELEASE" 2>/dev/null || echo "before deploy tooling" > "$B/RELEASE"
for n in $CHANGED; do cp "$REPO/server/$n" "$BOT/$n.new" && mv "$BOT/$n.new" "$BOT/$n"; done

TAG=$(git tag --points-at HEAD -l 'r*' | sort -V | tail -1)
if [ -z "$TAG" ]; then
    last=$(git tag -l 'r*' | sed 's/^r//' | sort -n | tail -1); TAG="r$((last + 1))"
    git tag -a "$TAG" HEAD -m "$TAG deployed $(TZ=Asia/Kolkata date '+%d %b %Y %H:%M IST')" && git push -q origin "$TAG" 2>/dev/null \
        || say "(note: tag $TAG made here but not pushed to GitHub)"
fi
echo "$TAG $SHA $(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M IST')" > "$BOT/RELEASE"

for s in $ACTIVE; do $SYSTEMCTL restart "$s" || say "WARNING: restart of $s failed"; done
# partner bots run the same condor.py: restart each one only when it holds no position
LATER=""
case " $SVC " in *" tppd-condor "*)
    for u in $($SYSTEMCTL list-units --type=service --state=active --no-legend 'tppd-partner@*' 2>/dev/null | awk '{print $1}'); do
        slug=${u#tppd-partner@}; slug=${slug%.service}
        if "$PY" -c "import json,sys;sys.exit(1 if (json.load(open(sys.argv[1])) or {}).get('open') else 0)" \
              "$HOME/btc_partners/$slug/data/state.json" 2>/dev/null || [ ! -f "$HOME/btc_partners/$slug/data/state.json" ]; then
            $SYSTEMCTL restart "$u" && ACTIVE="$ACTIVE $u"
        else LATER="$LATER $slug"; fi
    done;;
esac
# keep ~/deploy itself current (older releases such as r1 have no deploy tool: keep the newer one)
[ -f "$REPO/deploy/deploy.sh" ] && cp "$REPO/deploy/deploy.sh" "$SELF.new" && chmod +x "$SELF.new" && mv "$SELF.new" "$SELF"
sleep "${DEPLOY_SETTLE:-4}"
echo "DEPLOY-OK $TAG ($SHA). Tests on this server: $NT passed."
say "files:$CHANGED"
say "backup: $B   (undo: ~/deploy <previous release>)"
for s in $ACTIVE; do say "$s: $($SYSTEMCTL is-active $s)"; done
[ -z "$ACTIVE" ] && say "no running service needed a restart"
[ -n "$LATER" ] && say "partner bots with a trade open, NOT restarted yet:$LATER  (run ~/deploy --restart-partners after their trades close)"
exit 0
