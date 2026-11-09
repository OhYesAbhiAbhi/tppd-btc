"""Nightly iron condor / short strangle for BTC daily options on Delta Exchange India.

CONDOR_STYLE=strangle (default): sell the same ~0.20-delta put and call, no wings, and close both
if the position is down CONDOR_MAX_LOSS_USD (marked at the ask, after fees). Lots are the same as the
condor would trade. The stop is NOT a guarantee: a gap or a failed exit can lose more.
CONDOR_STYLE=condor: the original iron condor below.

From 24 Oct 2026 (strangle only):
  CONDOR_FIXED_LOTS=N      trade exactly N lots (capped at CONDOR_MAX_LOTS) instead of the condor sizing; 0 = off.
  CONDOR_TAKE_PROFIT=0.75  buy both legs back once the cost to close (at the ask) is <= 25% of the credit,
                           i.e. 75% of the premium has decayed; 0 = off (hold to settlement).

From 24 Oct 2026 evening (strangle only), CONDOR_VOL_SWITCH=on:
  At entry, BTC's realised vol over the last 24 h (1-min BTCUSD candles, annualised %):
    above CONDOR_VOL_ENTRY_RV (40)  -> "volatile" night: take profit at CONDOR_VOL_TP (0.25)
    otherwise                       -> "calm" night: hold to settlement, but exit at once if the realised
                                       vol over the last CONDOR_DIP_WINDOW_MIN (60) min goes above
                                       CONDOR_DIP_RV (60), checked from 60 min after entry ("dipstick").
  No volatility reading at entry -> treated as volatile. The $CONDOR_MAX_LOSS_USD stop always applies.
  From 26 Oct 2026: the dipstick exits only once the vol has stayed above CONDOR_DIP_RV for
  CONDOR_DIP_CONFIRM_MIN (10) minutes in a row; a reading back below resets the wait. 0 = exit at once.
  With the switch on, CONDOR_TAKE_PROFIT is not used.

From 5 Nov 2026, CONDOR_ENTRY_MODE=dampened (strangle with the vol switch): instead of entering at
CONDOR_ENTRY_IST, check every CONDOR_DAMP_CHECK_MIN (15) min from CONDOR_ENTRY_IST (22:30) until
CONDOR_DAMP_UNTIL_IST (11:30) next morning, and enter at the first check where BTC's realised vol is
calm on all three windows: 1 h <= CONDOR_DAMP_RV1 (30), 6 h <= CONDOR_DAMP_RV6 (30), 24 h <= CONDOR_DAMP_RV24 (40).
One trade per expiry (the 17:30 IST expiry after the check). On Sunday nights checking starts at
CONDOR_DAMP_SUN_FROM_IST (05:00) Monday, after the CME futures reopen. Skip days / dates / Fed nights are
decided per night (the evening's date); the hot-night skip is not used in this mode (a hot night can't be calm).
If no check is calm by the end, no trade, and Telegram says so. CONDOR_ENTRY_MODE=fixed (default) = as before.

From 7 Nov 2026, CONDOR_DRIFT_EXIT_PCT=2.0 (strangle): close both legs once BTC has moved that many % from the
entry price, up or down, even if the volatility exit hasn't fired. Checked with every watch (~10 s). 0 = off.

Once a day, around CONDOR_ENTRY_IST (default 21:30 IST), sell a ~0.20-delta put and call for the
NEXT day's 17:30 IST expiry, buy ~0.07-delta wings further out, and hold everything to
settlement. No stops: the wings cap the loss. Size: lots chosen so a full-width loss plus fees
is at most CONDOR_MAX_LOSS_USD.

This came out of the 22 Oct 2026 backtest on recorded Delta data: intraday direction-picking
lost money; evening-entry condors held to expiry made money (small sample; paper-test first).

    python condor.py             # run forever (paper unless CONDOR_MODE=live)
    python condor.py --once      # what it would trade now for tomorrow's expiry (never trades)
    python condor.py --report    # results so far
"""
import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import config  # noqa: F401  (loads .env into the environment)
import strategy as st
from bot import round_tick
from delta_client import DeltaAPIError, DeltaClient

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("condor")


def _env(name, default, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.lower() in ("1", "true", "yes", "on")
    return cast(v.split(" #", 1)[0].strip())


class CondorConfig:
    MODE = _env("CONDOR_MODE", "paper")                       # paper | live
    STYLE = _env("CONDOR_STYLE", "strangle")                  # strangle (with stop) | condor
    STOP_CONFIRM = _env("CONDOR_STOP_CONFIRM", 2, int)        # readings in a row below the stop
    WARN_USD = _env("CONDOR_WARN_USD", 25.0, float)           # Telegram warning at this open loss
    WATCH_SECONDS = _env("CONDOR_WATCH_SECONDS", 10, int)     # loop speed while a strangle is open
    DELTA_ENV = _env("DELTA_ENV", "prod")
    API_KEY = _env("DELTA_API_KEY", "")
    API_SECRET = _env("DELTA_API_SECRET", "")
    ENTRY_IST = _env("CONDOR_ENTRY_IST", "21:30")             # start of the nightly entry window
    ENTRY_WINDOW_MIN = _env("CONDOR_ENTRY_WINDOW_MIN", 60, int)
    SHORT_DELTA = _env("CONDOR_SHORT_DELTA", 0.20, float)
    SHORT_DELTA_TOL = _env("CONDOR_SHORT_DELTA_TOL", 0.07, float)
    WING_DELTA = _env("CONDOR_WING_DELTA", 0.07, float)
    MAX_WIDTH = _env("CONDOR_MAX_WIDTH", 1500, float)         # wing at most this far from its short
    MIN_CREDIT_SHARE = _env("CONDOR_MIN_CREDIT_SHARE", 0.4, float)  # net credit / premium sold
    MAX_SHORT_SPREAD_PCT = _env("CONDOR_MAX_SHORT_SPREAD_PCT", 0.15, float)
    MAX_LOSS_USD = _env("CONDOR_MAX_LOSS_USD", 40.0, float)
    MAX_LOTS = _env("CONDOR_MAX_LOTS", 100, int)
    FIXED_LOTS = _env("CONDOR_FIXED_LOTS", 0, int)            # strangle: fixed size; 0 = condor sizing
    TAKE_PROFIT = _env("CONDOR_TAKE_PROFIT", 0.0, float)      # strangle: close at this share of decay
    VOL_SWITCH = _env("CONDOR_VOL_SWITCH", False, bool)       # strangle: calm/volatile rules (see top)
    VOL_ENTRY_RV = _env("CONDOR_VOL_ENTRY_RV", 40.0, float)   # 24h realised vol (%) above this = volatile
    VOL_TP = _env("CONDOR_VOL_TP", 0.25, float)               # take profit on volatile nights
    DIP_RV = _env("CONDOR_DIP_RV", 60.0, float)               # calm nights: exit if short-term vol above
    DIP_WINDOW_MIN = _env("CONDOR_DIP_WINDOW_MIN", 60, int)
    DIP_CONFIRM_MIN = _env("CONDOR_DIP_CONFIRM_MIN", 10, int)  # vol must stay above DIP_RV this long
    SKIP_DAYS = _env("CONDOR_SKIP_DAYS", "")                  # entry days (IST) with no trade, e.g. Thu
    VOL_SKIP_RV = _env("CONDOR_VOL_SKIP_RV", 0.0, float)      # 24h realised vol (%) above this = no trade; 0 = off
    FED_SKIP = _env("CONDOR_FED_SKIP", False, bool)           # no trade if a Fed (FOMC) decision falls inside the trade
    FED_URL = _env("CONDOR_FED_URL", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
    SKIP_DATES = _env("CONDOR_SKIP_DATES", "")                # extra entry dates (IST) with no trade: 2026-12-24,...
    ENTRY_MODE = _env("CONDOR_ENTRY_MODE", "fixed")           # fixed | dampened (see top)
    DAMP_RV1 = _env("CONDOR_DAMP_RV1", 30.0, float)           # dampened entry: 1 h vol (%) at most
    DAMP_RV6 = _env("CONDOR_DAMP_RV6", 30.0, float)           # 6 h vol at most
    DAMP_RV24 = _env("CONDOR_DAMP_RV24", 40.0, float)         # 24 h vol at most
    DAMP_UNTIL_IST = _env("CONDOR_DAMP_UNTIL_IST", "11:30")   # last check, next morning
    DAMP_SUN_FROM_IST = _env("CONDOR_DAMP_SUN_FROM_IST", "05:00")  # Sunday nights: first check (Monday)
    DAMP_CHECK_MIN = _env("CONDOR_DAMP_CHECK_MIN", 15, int)   # minutes between checks
    DRIFT_EXIT_PCT = _env("CONDOR_DRIFT_EXIT_PCT", 0.0, float)  # strangle: exit if BTC moves this % from entry; 0 = off
    PERP_SYMBOL = _env("CONDOR_PERP_SYMBOL", "BTCUSD")
    MAX_MARGIN_USE_PCT = _env("CONDOR_MAX_MARGIN_USE_PCT", 0.50, float)
    EXPIRY_HOUR_IST, EXPIRY_MIN_IST = 17, 30
    SETTLE_WAIT_MIN = _env("CONDOR_SETTLE_WAIT_MIN", 10, int)
    # entry orders: rest a limit order, re-price from mid towards the far side
    WORK_SECONDS = _env("CONDOR_WORK_SECONDS", 30, int)
    STEP_SECONDS = _env("CONDOR_STEP_SECONDS", 5, int)
    SLIPPAGE_PCT = _env("CONDOR_SLIPPAGE_PCT", 0.03, float)
    # fees (Delta India options): min(0.01% of notional, 3.5% of premium) + 18% GST
    FEE_RATE_NOTIONAL = _env("FEE_RATE_NOTIONAL", 0.0001, float)
    FEE_CAP_PREMIUM = _env("FEE_CAP_PREMIUM", 0.035, float)
    GST = _env("GST", 0.18, float)
    LOOP_SECONDS = _env("CONDOR_LOOP_SECONDS", 30, int)
    DATA_DIR = _env("CONDOR_DATA_DIR", str(Path(__file__).with_name("data_condor")))
    TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")


TRADE_COLS = ["mode", "style", "entry_ist", "expiry", "lots", "put_short", "put_wing", "call_short", "call_wing",
              "spot_entry", "credit", "max_loss_usd", "settle_price", "payoff", "fees_usd",
              "net_usd", "net_pct_credit", "note"]


NY = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"], 1)}
# Fallback if the Fed's page can't be read and nothing is cached (decision = last day of each meeting).
FOMC_FALLBACK = ["2025-12-10", "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16",
                 "2026-10-28", "2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28",
                 "2027-09-15", "2027-10-27", "2027-12-08"]


def parse_fomc(html):
    """Decision dates (last day of each meeting) from the Fed's FOMC calendar page, as YYYY-MM-DD strings."""
    import re
    out = []
    heads = list(re.finditer(r"(\d{4}) FOMC Meetings", html))
    for k, hd in enumerate(heads):
        year = int(hd.group(1))
        sec = html[hd.end(): heads[k + 1].start() if k + 1 < len(heads) else len(html)]
        for mo, dt in re.findall(r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<",
                                 sec, re.S):
            months = [MONTHS.get(x.strip()[:3].lower()) for x in mo.split("/")]
            days = [int(x) for x in re.findall(r"\d+", dt.split("(")[0])]
            if not days or not months or months[-1] is None:
                continue
            m = months[-1]
            y = year + 1 if (len(months) > 1 and months[0] == 12 and m == 1) else year
            try:
                out.append(datetime(y, m, days[-1]).strftime("%Y-%m-%d"))
            except ValueError:
                continue
    return sorted(set(out))


def intrinsic(kind, strike, s):
    return max(strike - s, 0.0) if kind == "put" else max(s - strike, 0.0)


class Condor:
    def __init__(self, client, cfg=CondorConfig, now_fn=time.time, sleep_fn=time.sleep):
        self.c, self.cfg, self.now, self.sleep = client, cfg, now_fn, sleep_fn
        self.dir = Path(cfg.DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.trades_path = self.dir / "trades.csv"
        self.state = (json.loads(self.state_path.read_text()) if self.state_path.exists()
                      else {"last_entry_date": None, "open": None, "last_skip": ""})
        self.products = {}
        self.last_note = {}
        if self.trades_path.exists():                       # add the 'style' column to older files
            with self.trades_path.open() as f:
                rows = list(csv.reader(f))
            if rows and "style" not in rows[0]:
                rows = [rows[0][:1] + ["style"] + rows[0][1:]] + [r[:1] + ["condor"] + r[1:] for r in rows[1:]]
                with self.trades_path.open("w", newline="") as f:
                    csv.writer(f).writerows(rows)

    # ---------------------------------------------------------------- utils
    def ist(self):
        return datetime.fromtimestamp(self.now(), IST)

    def save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    def notify(self, msg):
        log.info(msg)
        if self.cfg.TELEGRAM_BOT_TOKEN and self.cfg.TELEGRAM_CHAT_ID:
            try:
                requests.post(f"https://api.telegram.org/bot{self.cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
                              json={"chat_id": self.cfg.TELEGRAM_CHAT_ID,
                                    "text": f"[condor-{self.cfg.MODE}] {msg}"}, timeout=5)
            except requests.RequestException:
                pass

    def note(self, key, msg, every_s=1800):
        if self.now() - self.last_note.get(key, 0) >= every_s:
            self.last_note[key] = self.now()
            log.info(msg)

    def product(self, symbol, fresh=False):
        if fresh or symbol not in self.products:
            self.products[symbol] = self.c.product(symbol)
        return self.products[symbol]

    def spot(self):
        t = self.c.ticker("BTCUSD")
        return st._f(t.get("spot_price")) or st._f(t.get("mark_price"))

    def realised_vol(self, minutes):
        """Annualised % realised vol of BTC from 1-min candle closes over the last `minutes`.
        None if the candles can't be fetched or too many minutes are missing."""
        end = int(self.now())
        try:
            cs = self.c.candles(self.cfg.PERP_SYMBOL, "1m", end - (minutes + 3) * 60, end)
        except (DeltaAPIError, requests.RequestException, AttributeError) as e:
            self.note("rv-fail", f"volatility reading failed ({e})", every_s=600)
            return None
        closes = [float(c["close"]) for c in cs if float(c.get("close") or 0) > 0][-(minutes + 1):]
        if len(closes) < 0.8 * minutes:
            self.note("rv-short", f"volatility reading: only {len(closes)} of {minutes} minutes", every_s=600)
            return None
        r = [math.log(b / a) for a, b in zip(closes[:-1], closes[1:])]
        m = sum(r) / len(r)
        sd = math.sqrt(sum((x - m) ** 2 for x in r) / max(len(r) - 1, 1))
        return sd * math.sqrt(525600) * 100

    def vol_set(self):
        """(1 h, 6 h, 24 h) realised vol in % from one fetch of 1-min candles; None for any window that
        can't be measured."""
        end = int(self.now())
        try:
            cs = self.c.candles(self.cfg.PERP_SYMBOL, "1m", end - (1440 + 3) * 60, end)
        except (DeltaAPIError, requests.RequestException, AttributeError) as e:
            self.note("rv-fail", f"volatility reading failed ({e})", every_s=600)
            return None, None, None
        closes = [float(c["close"]) for c in cs if float(c.get("close") or 0) > 0]

        def rv(minutes):
            x = closes[-(minutes + 1):]
            if len(x) < 0.8 * minutes:
                return None
            r = [math.log(b / a) for a, b in zip(x[:-1], x[1:])]
            m = sum(r) / len(r)
            return math.sqrt(sum((y - m) ** 2 for y in r) / max(len(r) - 1, 1)) * math.sqrt(525600) * 100
        return rv(60), rv(360), rv(1440)

    def night_mode(self):
        """(mode, take_profit, dipstick, rv24) for a new strangle under the volatility switch."""
        cfg = self.cfg
        if not (cfg.VOL_SWITCH and cfg.STYLE == "strangle"):
            return "fixed", cfg.TAKE_PROFIT, False, None
        rv = self.realised_vol(1440)
        if rv is None or rv > cfg.VOL_ENTRY_RV:
            return "volatile", cfg.VOL_TP, False, rv
        return "calm", 0.0, True, rv

    def fee(self, price, spot):
        return st.fee_abs(price, spot, self.cfg)

    def settle_fee_max(self, spot):
        """Most the settlement can cost in fees: both legs of one side deep in the money."""
        return 2 * self.cfg.FEE_RATE_NOTIONAL * spot * (1 + self.cfg.GST)

    # ------------------------------------------------------------ schedule
    def dampened(self):
        return (self.cfg.ENTRY_MODE or "").strip().lower() == "dampened"

    def _hm(self, text):
        h, m = (int(x) for x in text.split(":"))
        return h, m

    def damp_session(self):
        """Dampened mode: (night date, first check, end) of the check window we are in now, else None.
        The night is the evening's date: Monday 03:00 belongs to Sunday's night."""
        now = self.ist()
        eh, em = self._hm(self.cfg.ENTRY_IST)
        uh, um = self._hm(self.cfg.DAMP_UNTIL_IST)
        sh, sm = self._hm(self.cfg.DAMP_SUN_FROM_IST)
        for d in (now.date() - timedelta(days=1), now.date()):
            nxt = d + timedelta(days=1)
            start = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
            if d.weekday() == 6:                              # Sunday night: from Monday 05:00
                start = datetime(nxt.year, nxt.month, nxt.day, sh, sm, tzinfo=IST)
            end = datetime(nxt.year, nxt.month, nxt.day, uh, um, tzinfo=IST)
            if start <= now < end:
                return d, start, end
        return None

    def night_day(self):
        """The night (entry evening's date, IST) that rules apply to."""
        if self.dampened():
            s = self.damp_session()
            if s:
                return s[0]
        return self.ist().date()

    def skip_days(self):
        return [d.strip()[:3].lower() for d in (self.cfg.SKIP_DAYS or "").split(",") if d.strip()]

    def skip_today(self):
        """True if today's (IST) entry day is in CONDOR_SKIP_DAYS (e.g. Thu: no Thursday-evening entry)."""
        return self.night_day().strftime("%a").lower() in self.skip_days()

    def hot_night(self):
        """(skip?, rv24) for tonight, decided once at the first check in the entry window and kept for the night.
        Off when CONDOR_VOL_SKIP_RV is 0. No volatility reading -> trade (as before: treated as volatile)."""
        lim = self.cfg.VOL_SKIP_RV
        if not lim or lim <= 0:
            return False, None
        today = self.night_day().isoformat()
        hc = self.state.get("hot_check") or {}
        if hc.get("day") == today:
            return hc.get("skip", False), hc.get("rv")
        rv = self.realised_vol(1440)
        skip = rv is not None and rv > lim
        self.state["hot_check"] = {"day": today, "skip": skip, "rv": None if rv is None else round(rv, 1)}
        self.save()
        return skip, rv

    def http_get(self, url):
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (tppd-bot calendar check)"})
        r.raise_for_status()
        return r.text

    def fomc_dates(self):
        """(decision dates, source text). Reads the Fed's calendar at most every 12 h (6 h after a failure),
        cached in fomc_calendar.json; falls back to the cache, then to the built-in list."""
        path = self.dir / "fomc_calendar.json"
        cache = {}
        try:
            cache = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        age = self.now() - cache.get("tried_ts", 0)
        ok_age = self.now() - cache.get("fetched_ts", 0)
        if (not cache.get("dates") or ok_age > 12 * 3600) and age > (6 * 3600 if cache.get("failed") else 0):
            cache["tried_ts"] = self.now()
            try:
                dates = parse_fomc(self.http_get(self.cfg.FED_URL))
                if len(dates) < 4:
                    raise ValueError(f"only {len(dates)} dates found")
                cache.update(dates=dates, fetched_ts=self.now(), failed=False)
            except Exception as e:  # network, HTTP or page-layout problem
                cache["failed"] = True
                self.note("fed-fail", f"Fed calendar not readable ({e}); using "
                          f"{'cached copy' if cache.get('dates') else 'built-in list'}", every_s=6 * 3600)
            try:
                path.write_text(json.dumps(cache))
            except OSError:
                pass
        if cache.get("dates"):
            when = datetime.fromtimestamp(cache.get("fetched_ts", 0), IST).strftime("%d %b %H:%M")
            return cache["dates"], f"federalreserve.gov, read {when} IST"
        return FOMC_FALLBACK, "built-in list (Fed page not readable)"

    def fed_in_trade(self, expiry=None):
        """The FOMC decision (2:00 pm New York time) that falls between now and this trade's expiry, if any:
        (decision datetime in IST, source) or (None, source)."""
        if not self.cfg.FED_SKIP:
            return None, ""
        exp = expiry or self.next_expiry()
        dates, src = self.fomc_dates()
        for d in dates:
            y, m, dd = (int(x) for x in d.split("-"))
            dec = datetime(y, m, dd, 14, 0, tzinfo=NY)
            if self.now() <= dec.timestamp() <= exp.timestamp():
                return dec.astimezone(IST), src
        return None, src

    def date_skipped(self):
        days = [d.strip() for d in (self.cfg.SKIP_DATES or "").split(",") if d.strip()]
        return self.night_day().isoformat() in days

    def in_entry_window(self):
        if self.dampened():
            return self.damp_session() is not None
        now = self.ist()
        h, m = (int(x) for x in self.cfg.ENTRY_IST.split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return start <= now < start + timedelta(minutes=self.cfg.ENTRY_WINDOW_MIN)

    def next_expiry(self):
        """The 17:30 IST expiry after the next one-day boundary: tomorrow's, for an evening entry."""
        now = self.ist()
        d = now.date() + timedelta(days=1)
        dt = datetime(d.year, d.month, d.day, self.cfg.EXPIRY_HOUR_IST, self.cfg.EXPIRY_MIN_IST, tzinfo=IST)
        if (dt - now).total_seconds() > 29.5 * 3600:        # entry after midnight (before 12:00): today's expiry
            dt -= timedelta(days=1)
        return dt

    # ------------------------------------------------------------ selection
    def plan(self):
        """Pick the four legs for the next expiry. Returns (plan dict, reason)."""
        cfg = self.cfg
        exp = self.next_expiry()
        chain = st.parse_chain(self.c.option_chain("BTC", exp.strftime("%d-%m-%Y")), exp.strftime("%d%m%y"))
        if not chain:
            return None, f"no options listed for {exp:%d %b} 17:30 expiry"
        spot = self.spot()

        def short(kind):
            otm = [c for c in chain if c.kind == kind and (c.strike < spot if kind == "put" else c.strike > spot)
                   and c.bid > 0 and c.ask > 0 and c.spread_pct <= cfg.MAX_SHORT_SPREAD_PCT
                   and abs(abs(c.delta) - cfg.SHORT_DELTA) <= cfg.SHORT_DELTA_TOL]
            return min(otm, key=lambda c: abs(abs(c.delta) - cfg.SHORT_DELTA)) if otm else None

        def wing(s):
            further = [c for c in chain if c.kind == s.kind and c.ask > 0
                       and (c.strike < s.strike if s.kind == "put" else c.strike > s.strike)
                       and abs(c.strike - s.strike) <= cfg.MAX_WIDTH]
            return min(further, key=lambda c: abs(abs(c.delta) - cfg.WING_DELTA)) if further else None

        ps, cs = short("put"), short("call")
        if not ps or not cs:
            return None, f"no tradeable ~{cfg.SHORT_DELTA:.2f}-delta {'put' if not ps else 'call'}"
        pw, cw = wing(ps), wing(cs)
        fixed = cfg.STYLE == "strangle" and cfg.FIXED_LOTS > 0
        cv = float(self.product(ps.symbol).get("contract_value", 0.001))
        if fixed:                                               # strangle at a fixed size: wings not needed
            sold = ps.bid + cs.bid
            return {"expiry": exp, "spot": spot, "legs": {"put_short": ps, "call_short": cs},
                    "credit": sold, "fees_in": self.fee(ps.bid, spot) + self.fee(cs.bid, spot),
                    "width": cs.strike - ps.strike, "lots": min(cfg.FIXED_LOTS, cfg.MAX_LOTS),
                    "max_loss_usd": cfg.MAX_LOSS_USD, "cv": cv}, "ok"
        if not pw or not cw:
            return None, f"no wing within {cfg.MAX_WIDTH:.0f} points"
        sold = ps.bid + cs.bid
        credit = sold - pw.ask - cw.ask
        if credit <= 0 or credit < cfg.MIN_CREDIT_SHARE * sold:
            return None, f"credit too small ({credit:.1f} of {sold:.1f} sold)"
        fees_in = sum(self.fee(p, spot) for p in (ps.bid, cs.bid, pw.ask, cw.ask))
        width = max(ps.strike - pw.strike, cw.strike - cs.strike)
        # worst case per lot: one side settles fully in the money; exit fee on that side's value
        per_lot = (width - credit + fees_in + self.settle_fee_max(spot)) * cv
        lots = min(cfg.MAX_LOTS, int(cfg.MAX_LOSS_USD // per_lot)) if per_lot > 0 else 0
        if lots < 1:
            return None, f"worst case per lot ${per_lot:.2f} exceeds CONDOR_MAX_LOSS_USD"
        legs = {"put_short": ps, "call_short": cs}
        if cfg.STYLE == "condor":
            legs.update(put_wing=pw, call_wing=cw)
        else:                                                   # lots as the condor; risk = the stop
            credit, fees_in = sold, self.fee(ps.bid, spot) + self.fee(cs.bid, spot)
        return {"expiry": exp, "spot": spot, "legs": legs,
                "credit": credit, "fees_in": fees_in, "width": width, "lots": lots,
                "max_loss_usd": round(per_lot * lots, 2) if cfg.STYLE == "condor" else cfg.MAX_LOSS_USD,
                "cv": cv}, "ok"

    # --------------------------------------------------------------- orders
    def _work_order(self, cand, side, size):
        """Rest a limit order from mid towards (and up to SLIPPAGE_PCT past) the far side,
        re-pricing every STEP_SECONDS for WORK_SECONDS. Returns (avg price, filled)."""
        cfg = self.cfg
        tick = float(self.product(cand.symbol).get("tick_size", 0.1))
        steps = max(cfg.WORK_SECONDS // max(cfg.STEP_SECONDS, 1), 1)
        remaining, fills = size, []
        for k in range(steps + 1):
            q = self.c.ticker(cand.symbol).get("quotes") or {}
            bid, ask = st._f(q.get("best_bid")), st._f(q.get("best_ask"))
            if side == "buy":
                far = ask or cand.ask
                start = (bid + far) / 2 if 0 < bid < far else far
                lp = round_tick(start + (far * (1 + cfg.SLIPPAGE_PCT) - start) * k / steps, tick, ROUND_CEILING)
            else:
                far = bid or cand.bid
                start = (far + ask) / 2 if ask > far > 0 else far
                raw = start + (max(far * (1 - cfg.SLIPPAGE_PCT), tick) - start) * k / steps
                lp = round_tick(max(raw, tick), tick, ROUND_FLOOR)
            o = self.c.place_order(product_id=cand.product_id, size=remaining, side=side,
                                   order_type="limit_order", limit_price=lp, time_in_force="gtc",
                                   client_order_id=f"cdr-{side[0]}-{int(self.now() * 1000)}-{k}")
            got = int(o.get("size", 0)) - int(o.get("unfilled_size", 0))
            avg = float(o.get("average_fill_price") or lp)
            if got < remaining and o.get("id"):
                self.sleep(cfg.STEP_SECONDS)
                try:
                    self.c.cancel_order(o["id"], cand.product_id)
                except DeltaAPIError:
                    pass
                try:
                    f = self.c.get_order(o["id"]) or o
                except DeltaAPIError:
                    f = o
                got = int(f.get("size", 0)) - int(f.get("unfilled_size", 0))
                avg = float(f.get("average_fill_price") or lp)
            if got > 0:
                fills.append((got, avg))
                remaining -= got
            log.info(f"{side} {cand.symbol}: step {k + 1}/{steps + 1} at {lp} (bid {bid}/ask {ask}) "
                     f"filled {size - remaining}/{size}")
            if remaining <= 0:
                break
        n = size - remaining
        return (sum(a * b for a, b in fills) / n if n else 0.0), n

    def _close(self, cand_or_leg, side_open, qty):
        """Unwind `qty` of a leg we just opened (used only when an entry is aborted/trimmed)."""
        side = "sell" if side_open == "buy" else "buy"
        pid = cand_or_leg["product_id"]
        fills = []

        def done(o, fallback):
            got = int(o.get("size", 0)) - int(o.get("unfilled_size", 0))
            if got > 0:
                fills.append((got, float(o.get("average_fill_price") or fallback)))
            return got
        for attempt in range(3):
            q = self.c.ticker(cand_or_leg["symbol"]).get("quotes") or {}
            ref = st._f(q.get("best_ask") if side == "buy" else q.get("best_bid"))
            if ref <= 0:
                break
            tick = float(self.product(cand_or_leg["symbol"]).get("tick_size", 0.1))
            lp = round_tick(ref * (1 + 0.02 * attempt) if side == "buy" else ref * (1 - 0.02 * attempt),
                            tick, ROUND_CEILING if side == "buy" else ROUND_FLOOR)
            o = self.c.place_order(product_id=pid, size=qty, side=side, order_type="limit_order",
                                   limit_price=lp, time_in_force="ioc", reduce_only=True,
                                   client_order_id=f"cdr-x-{int(self.now() * 1000)}-{attempt}")
            qty -= done(o, lp)
            if qty <= 0:
                break
        if qty > 0:
            o = self.c.place_order(product_id=pid, size=qty, side=side, order_type="market_order",
                                   reduce_only=True, client_order_id=f"cdr-x-{int(self.now() * 1000)}-m")
            qty -= done(o, 0.0)
            if qty > 0:
                self.notify(f"WARNING: could not close {qty} of {cand_or_leg['symbol']}; check Delta NOW.")
        n = sum(a for a, _ in fills)
        return (sum(a * b for a, b in fills) / n if n else 0.0), n, qty

    def margin_ok(self):
        try:
            w = self.c.wallet() or []
        except DeltaAPIError as e:
            return False, f"wallet unavailable ({e})"
        usd = next((x for x in w if x.get("asset_symbol") == "USD"), None) or {}
        bal, avail = st._f(usd.get("balance")), st._f(usd.get("available_balance"))
        if bal <= 0:
            return False, "wallet balance unavailable"
        used = 1 - avail / bal
        if used > self.cfg.MAX_MARGIN_USE_PCT:
            return False, f"margin in use {used:.0%} > {self.cfg.MAX_MARGIN_USE_PCT:.0%}"
        return True, f"margin in use {used:.0%}"

    # ---------------------------------------------------------------- entry
    def enter(self, p, why=""):
        cfg = self.cfg
        mode, tp, dip, rv24 = self.night_mode()          # decided before any order is sent
        L = p["legs"]
        lots = p["lots"]
        order = [x for x in [("put_wing", "buy"), ("call_wing", "buy"), ("put_short", "sell"),
                             ("call_short", "sell")] if x[0] in L]
        legs = {}
        if cfg.MODE == "paper":
            for name, side in order:
                c = L[name]
                legs[name] = {"symbol": c.symbol, "product_id": c.product_id, "kind": c.kind,
                              "strike": c.strike, "side": side, "entry": c.ask if side == "buy" else c.bid}
        else:
            ok, why = self.margin_ok()
            if not ok:
                return False, why
            want, err = lots, ""
            for name, side in order:
                c = L[name]
                try:
                    px, got = self._work_order(c, side, want)
                except DeltaAPIError as e:              # e.g. insufficient margin on the 2nd leg
                    err, got = f" ({e})", 0
                if got > 0:
                    legs[name] = {"symbol": c.symbol, "product_id": c.product_id, "kind": c.kind,
                                  "strike": c.strike, "side": side, "entry": px, "qty": got}
                want = min(want, got)
                if want <= 0:
                    break
            if want <= 0 or len(legs) < len(order):
                if legs:
                    self.notify(f"Entry aborted{err}: a leg did not fill; closing the "
                                f"{len(legs)} leg(s) that did. Check Delta.")
                for name in ("put_short", "call_short", "put_wing", "call_wing"):
                    if name in legs:
                        self._close(legs[name], legs[name]["side"], legs[name]["qty"])
                return False, f"a leg could not be filled{err}; anything filled was closed again"
            for name in legs:
                extra = legs[name]["qty"] - want
                if extra > 0:
                    self._close(legs[name], legs[name]["side"], extra)
            lots = want
        credit = sum(l["entry"] * (1 if l["side"] == "sell" else -1) for l in legs.values())
        fees_in = sum(self.fee(l["entry"], p["spot"]) for l in legs.values())
        if cfg.STYLE == "condor":
            max_loss = (p["width"] - credit + fees_in + self.settle_fee_max(p["spot"])) * p["cv"] * lots
        else:
            max_loss = cfg.MAX_LOSS_USD
        pos = {"mode": mode, "tp": tp, "dip": dip, "rv24": rv24, "style": cfg.STYLE, "entry_ts": self.now(), "below": 0, "warned": False, "expiry_ts": p["expiry"].timestamp(),
               "expiry": p["expiry"].strftime("%Y-%m-%d %H:%M"), "lots": lots, "cv": p["cv"],
               "spot": p["spot"], "legs": legs, "credit": credit, "fees_in": fees_in,
               "max_loss_usd": round(max_loss, 2)}
        self.state["open"] = pos
        self.state["last_entry_date"] = self.night_day().isoformat()
        self.save()
        s = lambda n: f"{legs[n]['strike']:.0f}"
        if cfg.STYLE == "strangle":
            self.notify(why + f"STRANGLE x{lots} for {p['expiry']:%d %b} 17:30 expiry (BTC {p['spot']:,.0f}): "
                        f"sold put {s('put_short')} + call {s('call_short')}. Credit {credit:.1f}/BTC = "
                        f"${credit * p['cv'] * lots:.2f}. Stop: close both at -${cfg.MAX_LOSS_USD:.0f} "
                        f"(not guaranteed in a gap). " + self.mode_text(mode, tp, dip, rv24, credit))
            return True, "ok"
        self.notify(f"IRON CONDOR x{lots} for {p['expiry']:%d %b} 17:30 expiry (BTC {p['spot']:,.0f}): "
                    f"puts {s('put_wing')}/{s('put_short')}, calls {s('call_short')}/{s('call_wing')}. "
                    f"Credit {credit:.1f}/BTC = ${credit * p['cv'] * lots:.2f}; worst case ${max_loss:.2f}. "
                    f"Holding to settlement.")
        return True, "ok"

    def mode_text(self, mode, tp, dip, rv24, credit):
        rvs = "no reading" if rv24 is None else f"{rv24:.0f}%"
        head = "" if mode == "fixed" else (f"{mode.upper()} night (BTC 24h vol {rvs}, limit "
                                           f"{self.cfg.VOL_ENTRY_RV:.0f}%): ")
        tps = (f"take profit when {tp:.0%} has decayed (cost to close <= {credit * (1 - tp):.1f}/BTC). "
               if tp > 0 else "hold to settlement. ")
        wait = (f" for {self.cfg.DIP_CONFIRM_MIN} min in a row" if self.cfg.DIP_CONFIRM_MIN > 0 else "")
        dips = (f"Exit early if BTC's {self.cfg.DIP_WINDOW_MIN}-min vol stays above {self.cfg.DIP_RV:.0f}%{wait}."
                if dip else "")
        drift = (f" Exit if BTC moves {self.cfg.DRIFT_EXIT_PCT:.1f}% from entry." if self.cfg.DRIFT_EXIT_PCT > 0 else "")
        return head + tps + dips + drift

    # ----------------------------------------------------------------- stop
    def close_cost(self, leg):
        """What it costs now to buy back one unit of a sold leg: the ask, or 1.2x mark if no ask."""
        t = self.c.ticker(leg["symbol"]) or {}
        q = t.get("quotes") or {}
        ask = st._f(q.get("best_ask"))
        return ask if ask > 0 else 1.2 * st._f(t.get("mark_price"))

    def open_pnl(self, pos):
        units = pos["lots"] * pos["cv"]
        asks = {n: self.close_cost(l) for n, l in pos["legs"].items()}
        fees_out = sum(self.fee(a, pos["spot"]) for a in asks.values())
        return (pos["credit"] - sum(asks.values()) - pos["fees_in"] - fees_out) * units, asks

    def watch(self):
        """Strangle only: close both legs once the open loss reaches MAX_LOSS_USD."""
        pos, cfg = self.state["open"], self.cfg
        pnl, asks = self.open_pnl(pos)
        self.note("hold", f"strangle x{pos['lots']} open P&L {pnl:+.2f} USD", every_s=900)
        tp = pos.get("tp", cfg.TAKE_PROFIT)
        if 0 < tp < 1 and sum(asks.values()) <= (1 - tp) * pos["credit"]:
            self.stop_out(pos, pnl, asks, reason="take profit")
            return
        if cfg.DRIFT_EXIT_PCT > 0 and pos.get("spot"):
            sp = self.spot()
            mv = (sp / pos["spot"] - 1) * 100 if sp else 0.0
            if abs(mv) >= cfg.DRIFT_EXIT_PCT:
                self.stop_out(pos, pnl, asks, reason=f"drift exit (BTC {sp:,.0f}, {mv:+.1f}% from entry "
                                                     f"{pos['spot']:,.0f}; limit {cfg.DRIFT_EXIT_PCT:.1f}%)")
                return
        if pos.get("dip") and self.now() - pos["entry_ts"] >= cfg.DIP_WINDOW_MIN * 60 \
                and self.now() - pos.get("dip_checked", 0) >= 55:
            pos["dip_checked"] = self.now()
            rv = self.realised_vol(cfg.DIP_WINDOW_MIN)
            if rv is not None:
                pos["rv_last"] = round(rv, 1)
                self.note("dip", f"dipstick: BTC {cfg.DIP_WINDOW_MIN}-min vol {rv:.0f}% (limit {cfg.DIP_RV:.0f}%)",
                          every_s=1800)
                if rv > cfg.DIP_RV:
                    since = pos.get("dip_over_since")
                    if since is None:
                        since = pos["dip_over_since"] = self.now()
                        self.save()
                        if cfg.DIP_CONFIRM_MIN > 0:
                            self.notify(f"VOL ALERT: BTC {cfg.DIP_WINDOW_MIN}-min vol {rv:.0f}% (limit "
                                        f"{cfg.DIP_RV:.0f}%), BTC {self.spot():,.0f}, open P&L {pnl:+.2f} USD. "
                                        f"Exiting if it stays above for {cfg.DIP_CONFIRM_MIN} min.")
                    if self.now() - since >= cfg.DIP_CONFIRM_MIN * 60:
                        held = (f", above {cfg.DIP_RV:.0f}% for {(self.now() - since) / 60:.0f} min"
                                if cfg.DIP_CONFIRM_MIN > 0 else "")
                        self.stop_out(pos, pnl, asks,
                                      reason=f"vol exit ({cfg.DIP_WINDOW_MIN}-min vol {rv:.0f}%{held})")
                        return
                elif pos.get("dip_over_since") is not None:
                    mins = (self.now() - pos.pop("dip_over_since")) / 60
                    self.save()
                    self.notify(f"VOL ALERT CLEARED: {cfg.DIP_WINDOW_MIN}-min vol back to {rv:.0f}% after "
                                f"{mins:.0f} min; holding. Open P&L {pnl:+.2f} USD.")
        if pnl <= -cfg.WARN_USD and not pos.get("warned"):
            pos["warned"] = True
            self.save()
            self.notify(f"Strangle open loss {pnl:+.2f} USD (stop at -{cfg.MAX_LOSS_USD:.0f}). "
                        f"BTC {self.spot():,.0f}.")
        if pnl > -cfg.MAX_LOSS_USD:
            if pos.get("below"):
                pos["below"] = 0
                self.save()
            return
        pos["below"] = pos.get("below", 0) + 1
        self.save()
        if pos["below"] < cfg.STOP_CONFIRM:
            log.info(f"open P&L {pnl:+.2f} below stop; confirming ({pos['below']}/{cfg.STOP_CONFIRM})")
            return
        self.stop_out(pos, pnl, asks)

    def stop_out(self, pos, pnl_seen, asks, reason="stop"):
        exits, left = {}, 0
        for n, l in pos["legs"].items():
            if self.cfg.MODE == "paper":
                exits[n] = asks[n]
            else:
                px, got, rem = self._close(l, "sell", pos["lots"])
                exits[n], left = (px if got else asks[n]), left + rem
        units = pos["lots"] * pos["cv"]
        paid = sum(exits.values())
        fees_out = sum(self.fee(x, pos["spot"]) for x in exits.values())
        net = (pos["credit"] - paid) * units - (pos["fees_in"] + fees_out) * units
        word = "stopped" if reason == "stop" else reason
        note = f"{word} at {self.ist():%H:%M}" + (f"; {left} lots NOT closed" if left else "")
        self.record(pos, self.spot(), paid, (pos["fees_in"] + fees_out) * units, net, note)
        self.notify(f"{reason.upper()}: strangle closed at {self.ist():%H:%M}, BTC {self.spot():,.0f}. "
                    f"Net {net:+.2f} USD. " + ("" if not left else f"WARNING {left} lots still open - check Delta! ")
                    + self.summary_line())

    def record(self, pos, S, payoff, fees_usd, net, how):
        L = pos["legs"]
        units = pos["lots"] * pos["cv"]
        g = lambda n: L[n]["strike"] if n in L else ""
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(TRADE_COLS)
            w.writerow([self.cfg.MODE, pos.get("style", "condor"),
                        datetime.fromtimestamp(pos["entry_ts"], IST).strftime("%Y-%m-%d %H:%M"),
                        pos["expiry"], pos["lots"], g("put_short"), g("put_wing"), g("call_short"),
                        g("call_wing"), round(pos["spot"], 1), round(pos["credit"], 2), pos["max_loss_usd"],
                        round(S, 1), round(payoff, 2), round(fees_usd, 4), round(net, 4),
                        round(net / (pos["credit"] * units), 4), how])
        self.state["open"] = None
        self.save()

    # ----------------------------------------------------------- settlement
    def settlement_price(self, pos):
        """BTC settlement level: the product's settlement_price if Delta has published it
        (treated as the index if large, else as that option's value), else spot now."""
        sym = pos["legs"]["put_short"]["symbol"]
        try:
            s = st._f(self.product(sym, fresh=True).get("settlement_price"))
        except DeltaAPIError:
            s = 0.0
        if s > 0.3 * pos["legs"]["put_short"]["strike"]:
            return s, "exchange settlement price"
        return self.spot(), "BTC spot at settlement time (exchange price not available)"

    def settle(self):
        pos = self.state["open"]
        if self.cfg.MODE == "live":
            try:
                held = {x.get("product_symbol") for x in self.c.positions() if float(x.get("size", 0))}
            except DeltaAPIError as e:
                self.note("settle-wait", f"positions unavailable ({e}); will retry")
                return
            if held & {l["symbol"] for l in pos["legs"].values()}:
                self.note("settle-wait", "expiry passed but positions still shown; waiting for Delta to settle",
                          every_s=600)
                if self.now() < pos["expiry_ts"] + 3 * 3600:
                    return
        S, how = self.settlement_price(pos)
        payoff = sum(intrinsic(l["kind"], l["strike"], S) * (1 if l["side"] == "sell" else -1)
                     for l in pos["legs"].values())
        fees_out = sum(self.fee(intrinsic(l["kind"], l["strike"], S), pos["spot"]) for l in pos["legs"].values())
        units = pos["lots"] * pos["cv"]
        net = (pos["credit"] - payoff) * units - (pos["fees_in"] + fees_out) * units
        net_pct = net / (pos["credit"] * units)
        L = pos["legs"]
        self.record(pos, S, payoff, (pos["fees_in"] + fees_out) * units, net, how)
        self.notify(f"SETTLED {pos['expiry']} {pos.get('style', 'condor')} x{pos['lots']}: BTC settled {S:,.0f} "
                    f"(puts {L['put_short']['strike']:.0f}, calls {L['call_short']['strike']:.0f}). "
                    f"Net {net:+.2f} USD ({net_pct:+.0%} of credit). {self.summary_line()}")

    def summary_line(self):
        if not self.trades_path.exists():
            return ""
        with self.trades_path.open() as f:
            rows = list(csv.DictReader(f))
        tot = sum(float(r["net_usd"]) for r in rows)
        wins = sum(float(r["net_usd"]) > 0 for r in rows)
        return f"Total {len(rows)} trades, {wins} wins, {tot:+.2f} USD."

    # ------------------------------------------------------------------ loop
    def step(self):
        pos = self.state["open"]
        if pos:
            if self.now() >= pos["expiry_ts"] + self.cfg.SETTLE_WAIT_MIN * 60:
                self.settle()
            elif pos.get("style") == "strangle" and self.now() < pos["expiry_ts"]:
                self.watch()
            else:
                self.note("hold", f"holding {pos.get('style', 'condor')} for {pos['expiry']}, x{pos['lots']}",
                          every_s=3600)
            return
        if self.dampened():
            self.damp_wrapup()
        if not self.in_entry_window():
            return
        today = self.night_day().isoformat()
        if self.state.get("last_entry_date") == today:
            return
        if self.dampened() and self.state.get("skipped_day") == today:
            return                                          # decided for the whole night
        if self.skip_today():
            if self.state.get("skipped_day") != today:
                self.state["skipped_day"] = today
                self.save()
                self.notify(f"No trade tonight: {self.ist():%A} entries are skipped "
                            f"(CONDOR_SKIP_DAYS={self.cfg.SKIP_DAYS}). Normal entry tomorrow.")
            return
        if self.date_skipped():
            if self.state.get("skipped_day") != today:
                self.state["skipped_day"] = today
                self.save()
                self.notify(f"No trade tonight: {today} is in CONDOR_SKIP_DATES. Normal entry tomorrow.")
            return
        fed, src = self.fed_in_trade()
        if fed is not None:
            if self.state.get("skipped_day") != today:
                self.state["skipped_day"] = today
                self.save()
                self.notify(f"No trade tonight: FED DECISION due {fed:%a %d %b %H:%M} IST (2:00 pm New York), "
                            f"inside tonight's trade window (to {self.next_expiry():%d %b %H:%M}). "
                            f"Source: {src}. Normal entry tomorrow.")
            return
        hot, rv24 = (False, None) if self.dampened() else self.hot_night()
        if hot:
            if self.state.get("skipped_day") != today:
                self.state["skipped_day"] = today
                self.save()
                self.notify(f"No trade tonight: HOT night - BTC 24h vol {rv24:.0f}% is above the "
                            f"{self.cfg.VOL_SKIP_RV:.0f}% limit (CONDOR_VOL_SKIP_RV). Normal entry tomorrow if it cools.")
            return
        why_in = ""
        if self.dampened():
            calm, why_in = self.damp_check(today)
            if not calm:
                return
        p, why = self.plan()
        if not p:
            self.note("skip", f"no entry yet: {why}", every_s=600)
            if self.state.get("last_skip") != today:
                self.state["last_skip"] = today
                self.save()
            return
        ok, why = self.enter(p, why_in)
        if not ok:
            self.note("enter-fail", f"entry not completed: {why}", every_s=300)

    def damp_check(self, night):
        """Dampened mode: (calm?, entry text). Checks on the CONDOR_DAMP_CHECK_MIN grid (e.g. :00 :15 :30 :45);
        once calm, stays calm until the entry is done."""
        cfg = self.cfg
        d = self.state.get("damp") or {}
        if d.get("night") != night:
            d = {"night": night, "slot": None, "calm": False, "told": False, "done": False, "last": None}
        slot = int(self.now() // (max(cfg.DAMP_CHECK_MIN, 1) * 60))
        if d.get("calm"):
            return True, d.get("why", "")
        if d.get("slot") == slot:
            return False, ""
        r1, r6, r24 = self.vol_set()
        d["slot"] = slot
        d["last"] = [None if x is None else round(x, 1) for x in (r1, r6, r24)]
        f = lambda x: "n/a" if x is None else f"{x:.0f}%"
        txt = f"1h {f(r1)}, 6h {f(r6)}, 24h {f(r24)}"
        need = f"{cfg.DAMP_RV1:.0f}/{cfg.DAMP_RV6:.0f}/{cfg.DAMP_RV24:.0f}%"
        calm = (None not in (r1, r6, r24) and r1 <= cfg.DAMP_RV1 and r6 <= cfg.DAMP_RV6 and r24 <= cfg.DAMP_RV24)
        if calm:
            d["calm"] = True
            d["why"] = f"DAMPENED ENTRY at {self.ist():%a %H:%M}: BTC vol calm on all three ({txt}; limits {need}). "
        else:
            self.note("damp", f"dampened check {self.ist():%H:%M}: not calm ({txt}; limits {need})", every_s=0)
            if not d.get("told"):
                d["told"] = True
                s = self.damp_session()
                end = s[2] if s else None
                self.notify(f"Waiting for calm before entering: BTC vol {txt} (need 1h/6h/24h at most {need}). "
                            f"Checking every {cfg.DAMP_CHECK_MIN} min until {end:%a %H:%M} IST; no trade if it never settles.")
        self.state["damp"] = d
        self.save()
        return calm, d.get("why", "")

    def damp_wrapup(self):
        """After a dampened window ends without an entry: say so once."""
        d = self.state.get("damp") or {}
        if not d.get("night") or d.get("done") or d.get("calm"):
            return
        s = self.damp_session()
        if s and s[0].isoformat() == d["night"]:
            return                                          # still inside that night's window
        d["done"] = True
        self.state["damp"] = d
        self.save()
        if self.state.get("last_entry_date") == d["night"]:
            return
        l = d.get("last") or [None, None, None]
        f = lambda x: "n/a" if x is None else f"{x:.0f}%"
        self.notify(f"No trade for the night of {d['night']}: BTC vol never settled by {self.cfg.DAMP_UNTIL_IST} IST "
                    f"(last check 1h {f(l[0])}, 6h {f(l[1])}, 24h {f(l[2])}). Normal checks again from the next night's window.")

    def run(self):
        kind = ("strangle, stop at" if self.cfg.STYLE == "strangle" else "condor, worst case")
        extra = ""
        if self.cfg.STYLE == "strangle":
            extra = (f", {self.cfg.FIXED_LOTS} lots" if self.cfg.FIXED_LOTS > 0 else "") + (
                f", vol switch: 24h vol > {self.cfg.VOL_ENTRY_RV:.0f}% -> take profit {self.cfg.VOL_TP:.0%}, "
                f"else hold with {self.cfg.DIP_WINDOW_MIN}-min vol exit > {self.cfg.DIP_RV:.0f}%"
                if self.cfg.VOL_SWITCH else
                f", take profit at {self.cfg.TAKE_PROFIT:.0%} decay" if self.cfg.TAKE_PROFIT > 0 else "")
        skip = f" No entry on: {self.cfg.SKIP_DAYS}." if self.skip_days() else ""
        if self.cfg.VOL_SKIP_RV > 0 and not self.dampened():
            skip += f" No entry when 24h vol > {self.cfg.VOL_SKIP_RV:.0f}%."
        if self.cfg.FED_SKIP:
            skip += " No entry on Fed decision nights (checked on federalreserve.gov)."
        if self.cfg.STYLE == "strangle" and self.cfg.DRIFT_EXIT_PCT > 0:
            skip += f" Exit early if BTC moves {self.cfg.DRIFT_EXIT_PCT:.1f}% from entry."
        if self.dampened():
            win = (f"Dampened entry: checks every {self.cfg.DAMP_CHECK_MIN} min from {self.cfg.ENTRY_IST} to "
                   f"{self.cfg.DAMP_UNTIL_IST} IST (Sunday nights from {self.cfg.DAMP_SUN_FROM_IST} Monday), enters when "
                   f"1h/6h/24h vol <= {self.cfg.DAMP_RV1:.0f}/{self.cfg.DAMP_RV6:.0f}/{self.cfg.DAMP_RV24:.0f}%.")
        else:
            win = f"Entry window {self.cfg.ENTRY_IST} IST +{self.cfg.ENTRY_WINDOW_MIN} min."
        self.notify(f"Nightly bot started ({self.cfg.MODE}, {kind} ${self.cfg.MAX_LOSS_USD:.0f}{extra}). "
                    f"{win}{skip}")
        while True:
            try:
                self.step()
            except DeltaAPIError as e:
                log.warning(f"API error: {e}")
            except Exception:
                log.exception("unexpected error")
            pos = self.state["open"]
            fast = pos and pos.get("style") == "strangle"
            self.sleep(self.cfg.WATCH_SECONDS if fast else self.cfg.LOOP_SECONDS)


def setup_logging(d):
    Path(d).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(Path(d) / "condor.log", maxBytes=3_000_000, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]
    log.setLevel(logging.INFO)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="show tonight's condor (never trades)")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    cfg = CondorConfig
    if cfg.MODE not in ("paper", "live"):
        raise SystemExit("CONDOR_MODE must be paper or live")
    if cfg.MODE == "live" and not (cfg.API_KEY and cfg.API_SECRET):
        raise SystemExit("live mode needs DELTA_API_KEY / DELTA_API_SECRET")
    setup_logging(cfg.DATA_DIR)
    bot = Condor(DeltaClient(cfg.API_KEY, cfg.API_SECRET, cfg.DELTA_ENV))
    if a.report:
        print(bot.summary_line() or "No trades yet.")
        if bot.trades_path.exists():
            print(bot.trades_path.read_text())
        return
    if a.once:
        if cfg.FED_SKIP:
            fed, src = bot.fed_in_trade()
            dates, _ = bot.fomc_dates()
            nxt = next((d for d in dates if datetime.strptime(d, "%Y-%m-%d").date() >= bot.ist().date()), None)
            print(f"Fed check ({src}): next decision {nxt or 'n/a'}; "
                  f"{'NO TRADE tonight - decision at ' + fed.strftime('%d %b %H:%M') + ' IST is inside the trade' if fed else 'no decision inside tonight' + chr(39) + 's trade'}")
        if bot.skip_days():
            print(f"Skipped entry days: {cfg.SKIP_DAYS}. Today ({bot.ist():%A}): "
                  f"{'NO TRADE tonight' if bot.skip_today() else 'trades as normal'}.")
        if bot.dampened():
            r1, r6, r24 = bot.vol_set()
            f = lambda x: "n/a" if x is None else f"{x:.1f}%"
            calm = None not in (r1, r6, r24) and r1 <= cfg.DAMP_RV1 and r6 <= cfg.DAMP_RV6 and r24 <= cfg.DAMP_RV24
            print(f"Dampened entry ({cfg.ENTRY_IST}-{cfg.DAMP_UNTIL_IST} IST, Sunday nights from "
                  f"{cfg.DAMP_SUN_FROM_IST} Mon; limits 1h/6h/24h {cfg.DAMP_RV1:.0f}/{cfg.DAMP_RV6:.0f}/"
                  f"{cfg.DAMP_RV24:.0f}%): now 1h {f(r1)}, 6h {f(r6)}, 24h {f(r24)} -> "
                  f"{'CALM - would enter at a check' if calm else 'not calm - would wait'}")
        p, why = bot.plan()
        if not p:
            print("Would not trade now:", why)
            return
        L = p["legs"]
        print(f"Expiry {p['expiry']:%d %b %H:%M} IST, BTC {p['spot']:,.0f}")
        for n in [x for x in ("put_wing", "put_short", "call_short", "call_wing") if x in L]:
            c = L[n]
            print(f"  {n:10} {c.symbol:22} delta {c.delta:+.2f}  bid {c.bid} / ask {c.ask}")
        print(f"Style {cfg.STYLE}. Credit {p['credit']:.1f}/BTC, width {p['width']:.0f}, lots {p['lots']}, "
              f"{'stop' if cfg.STYLE == 'strangle' else 'worst case'} ${p['max_loss_usd']}, credit ${p['credit'] * p['cv'] * p['lots']:.2f}")
        if cfg.STYLE == "strangle" and cfg.VOL_SWITCH:
            mode, tp, dip, rv24 = bot.night_mode()
            rv1 = bot.realised_vol(cfg.DIP_WINDOW_MIN)
            print(f"Volatility now: BTC 24h vol {'n/a' if rv24 is None else f'{rv24:.1f}%'}, "
                  f"{cfg.DIP_WINDOW_MIN}-min vol {'n/a' if rv1 is None else f'{rv1:.1f}%'}")
            print("If entered now: " + bot.mode_text(mode, tp, dip, rv24, p["credit"]))
            if cfg.VOL_SKIP_RV > 0:
                hot = rv24 is not None and rv24 > cfg.VOL_SKIP_RV
                print(f"Hot-night skip: limit {cfg.VOL_SKIP_RV:.0f}% -> "
                      f"{'NO TRADE if it stays like this at entry' if hot else 'would trade'}")
        elif cfg.STYLE == "strangle" and cfg.TAKE_PROFIT > 0:
            print(f"Take profit at {cfg.TAKE_PROFIT:.0%} decay: close when both legs cost <= "
                  f"{p['credit'] * (1 - cfg.TAKE_PROFIT):.1f}/BTC "
                  f"(about ${p['credit'] * cfg.TAKE_PROFIT * p['cv'] * p['lots']:.2f} before exit fees)")
        ok, why = bot.margin_ok() if cfg.API_KEY else (True, "no API key, margin not checked")
        print(f"Margin now: {why}")
        return
    bot.run()


if __name__ == "__main__":
    main()
