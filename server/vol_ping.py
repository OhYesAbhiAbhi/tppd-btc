"""One-line hourly volatility message to the BTCClaude1 Telegram chat (run by cron at the top of each hour).

  [vol] 22:00 IST | DVOL 41.2% (+0.8 in 1h) | BTC 24h vol 27.0% -> CALM (limit 40%) | 60m vol 35.1% (exit level 60%) | BTC 81,950

DVOL = Deribit's BTC implied-volatility index (public API, no key).
24h vol = BTC's realised volatility over the last 24 h from Delta's 1-minute BTCUSD candles, annualised -
the same kind of reading the strangle bot's volatility switch uses (CONDOR_VOL_ENTRY_RV, default 40).
60m vol = the same over the last 60 minutes: the reading behind the bot's overnight VOL EXIT
(CONDOR_DIP_RV, default 60; it acts on calm nights from 60 min after entry).
Read-only: places no orders and doesn't touch the trading bot.

    python vol_ping.py           # send now
    python vol_ping.py --print   # just print the line
"""
import math
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config  # noqa: F401  (loads .env)
from delta_client import DeltaClient

IST = ZoneInfo("Asia/Kolkata")


def dvol(now_ms):
    r = requests.get("https://www.deribit.com/api/v2/public/get_volatility_index_data",
                     params={"currency": "BTC", "start_timestamp": now_ms - 2 * 3600_000,
                             "end_timestamp": now_ms, "resolution": "60"}, timeout=15).json()
    rows = (r.get("result") or {}).get("data") or []
    if not rows:
        return None, None
    rows.sort(key=lambda x: x[0])
    last = rows[-1]
    ago = [x for x in rows if x[0] <= last[0] - 3600_000]
    return last[4], (last[4] - ago[-1][4]) if ago else None


def _rv(closes):
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(r) < 2:
        return None
    mu = sum(r) / len(r)
    return math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(525_600) * 100


def realised_vol(client, now_s):
    """(24 h vol, 60 min vol, last price) from Delta's 1-minute candles."""
    c = client.candles("BTCUSD", "1m", now_s - 86400, now_s)
    closes = [float(x["close"]) for x in c if x.get("close")]
    last = closes[-1] if closes else None
    rv24 = _rv(closes) if len(closes) >= 600 else None
    rv60 = _rv(closes[-61:]) if len(closes) >= 50 else None
    return rv24, rv60, last


def line(client=None, now=None):
    now = now or time.time()
    client = client or DeltaClient(env=os.environ.get("DELTA_ENV", "prod"))
    parts = [datetime.fromtimestamp(now, IST).strftime("%H:%M IST")]
    try:
        d, ch = dvol(int(now * 1000))
        parts.append(f"DVOL {d:.1f}%" + (f" ({ch:+.1f} in 1h)" if ch is not None else "") if d is not None
                     else "DVOL n/a")
    except Exception as e:  # keep the message going even if one source fails
        parts.append(f"DVOL n/a ({type(e).__name__})")
    try:
        rv, rv60, px = realised_vol(client, int(now))
        lim = float(os.environ.get("CONDOR_VOL_ENTRY_RV", "40").split()[0])
        dip = float(os.environ.get("CONDOR_DIP_RV", "60").split()[0])
        if rv is not None:
            parts.append(f"BTC 24h vol {rv:.1f}% -> {'VOLATILE' if rv > lim else 'CALM'} (limit {lim:.0f}%)")
        else:
            parts.append("BTC 24h vol n/a")
        if rv60 is not None:
            parts.append(f"60m vol {rv60:.1f}% " + (f"ABOVE exit level {dip:.0f}%" if rv60 > dip
                                                     else f"(exit level {dip:.0f}%)"))
        else:
            parts.append("60m vol n/a")
        if px:
            parts.append(f"BTC {px:,.0f}")
    except Exception as e:
        parts.append(f"BTC vol n/a ({type(e).__name__})")
    return "[vol] " + " | ".join(parts)


def send(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (tok and chat):
        print("Telegram not configured")
        return False
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text},
                      timeout=10)
    return r.ok


if __name__ == "__main__":
    msg = line()
    print(msg)
    if "--print" not in sys.argv:
        send(msg)
