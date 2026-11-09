"""BTC market data recorder for Delta Exchange India — runs 24x7 next to the trading bots.

Every minute it records:
  * the BTCUSD perpetual: price, mark, spot index, funding rate, open interest, 24h volume,
    best bid/ask + sizes, and order-book depth within 0.1% / 0.5% of mid
  * 1-minute BTCUSD candles (backfilled 90 days on the first run)
  * indicators worked out from those candles: RSI(14) on 1m/5m/15m/1h, Bollinger z (5m, 20),
    EMA(240), 1h ATR %, plus funding and OI alongside
  * BTC options of EVERY expiry (daily, weekly, monthly, quarterly): bid/ask/sizes, mark, IVs,
    Greeks, OI, volume. The nearest COLLECT_NEAR_EXPIRIES expiries every minute, the rest
    every COLLECT_FAR_EVERY_MIN minutes.
  * settlement price of each expiry after it settles

Storage (DATA_DIR, default ~/btc_data):
  core.db                 candles, indicators, market snapshots, settlements, instruments (small)
  options_YYYY-MM-DD.db   one file per IST day of option snapshots; gzipped once the day is over

    python collector.py            # run forever (systemd: tppd-collector)
    python collector.py --status   # what has been recorded, disk space
    python collector.py --export 2026-10-23 2026-10-30   # one .db.gz for that range (+ core.db)
    python collector.py --send FILE                      # send a file to Telegram
"""
import argparse
import gzip
import json
import logging
import math
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import config  # noqa: F401  (loads .env)
from delta_client import DeltaAPIError, DeltaClient

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("collector")


def _env(name, default, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.lower() in ("1", "true", "yes", "on")
    return cast(v.split(" #", 1)[0].strip())


class CollectorConfig:
    DELTA_ENV = _env("DELTA_ENV", "prod")
    DATA_DIR = _env("COLLECT_DATA_DIR", str(Path.home() / "btc_data"))
    PERP = _env("COLLECT_PERP", "BTCUSD")
    UNDERLYING = _env("COLLECT_UNDERLYING", "BTC")
    NEAR_EXPIRIES = _env("COLLECT_NEAR_EXPIRIES", 3, int)       # recorded every minute
    FAR_EVERY_MIN = _env("COLLECT_FAR_EVERY_MIN", 5, int)       # all other expiries
    BACKFILL_DAYS = _env("COLLECT_BACKFILL_DAYS", 90, int)
    MIN_FREE_GB = _env("COLLECT_MIN_FREE_GB", 2.0, float)       # below: near expiries only
    STOP_FREE_GB = _env("COLLECT_STOP_FREE_GB", 1.0, float)     # below: no options at all
    TELEGRAM_BACKUP = _env("COLLECT_TELEGRAM_BACKUP", True, bool)  # yesterday's file to Telegram daily
    BACKUP_HOUR_IST = _env("COLLECT_BACKUP_HOUR_IST", 6, int)
    TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")


def f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ expiries
def expiry_of(symbol):
    """C-BTC-82000-231026 -> datetime 2026-10-23 12:00 UTC (17:30 IST)."""
    d = symbol.rsplit("-", 1)[-1]
    return datetime(2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]), 12, 0, tzinfo=timezone.utc)


def expiry_type(exp):
    if exp.weekday() != 4:
        return "daily"
    last_friday = (exp + timedelta(days=7)).month != exp.month
    if last_friday:
        return "quarterly" if exp.month in (3, 6, 9, 12) else "monthly"
    return "weekly"


# ---------------------------------------------------------------- indicators
def rsi(closes, n=14):
    """Wilder RSI of the last value; None if too short."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def bucket(rows, minutes):
    """rows: [(unix_s, o, h, l, c)] oldest-first 1m candles -> list of (o, h, l, c) per bucket."""
    out, key = [], None
    for t, o, h, l, c in rows:
        k = t // (minutes * 60)
        if k != key:
            out.append([o, h, l, c])
            key = k
        else:
            b = out[-1]
            b[1], b[2], b[3] = max(b[1], h), min(b[2], l), c
    return out


def indicators(rows):
    """rows: 1m candles oldest-first, ~4 days. Returns dict for the last minute."""
    c1 = [r[4] for r in rows]
    out = {"close": c1[-1] if c1 else None, "rsi_1m": rsi(c1[-500:])}
    for m, name in ((5, "rsi_5m"), (15, "rsi_15m"), (60, "rsi_1h")):
        out[name] = rsi([b[3] for b in bucket(rows, m)][-300:])
    b5 = [b[3] for b in bucket(rows, 5)][-20:]
    if len(b5) == 20:
        mu = sum(b5) / 20
        sd = math.sqrt(sum((x - mu) ** 2 for x in b5) / 19)
        out["bb_z_5m"] = (b5[-1] - mu) / sd if sd > 0 else 0.0
    else:
        out["bb_z_5m"] = None
    ema, k = None, 2 / 241
    for x in c1[-1500:]:
        ema = x if ema is None else ema + k * (x - ema)
    out["ema_240"] = ema
    h1 = bucket(rows, 60)[-15:]
    if len(h1) >= 15:
        trs = [max(h1[i][1], h1[i - 1][3]) - min(h1[i][2], h1[i - 1][3]) for i in range(1, len(h1))]
        out["atr_1h_pct"] = sum(trs) / len(trs) / h1[-1][3] * 100
    else:
        out["atr_1h_pct"] = None
    return out


# -------------------------------------------------------------------- schema
CORE_SCHEMA = """
create table if not exists candles_1m (time integer primary key, open real, high real, low real, close real, volume real);
create table if not exists market (ts integer primary key, spot real, mark real, last real, funding_rate real,
  oi real, oi_usd real, volume_24h real, turnover_usd real, best_bid real, best_ask real, bid_size real,
  ask_size real, mark_basis real, depth_bid_01 real, depth_ask_01 real, depth_bid_05 real, depth_ask_05 real);
create table if not exists indicators (ts integer primary key, close real, rsi_1m real, rsi_5m real, rsi_15m real,
  rsi_1h real, bb_z_5m real, ema_240 real, atr_1h_pct real, funding_rate real, oi real);
create table if not exists instruments (id integer primary key, symbol text unique, product_id integer, kind text,
  strike real, expiry_utc text, expiry_type text);
create table if not exists settlements (expiry_utc text primary key, expiry_type text, settlement_price real,
  btc_close_at_expiry real, recorded_ts integer);
"""
OPT_SCHEMA = """
create table if not exists option_snaps (ts integer, iid integer, bid real, ask real, bid_size real, ask_size real,
  mark real, bid_iv real, ask_iv real, mark_iv real, delta real, gamma real, theta real, vega real, oi real,
  volume real, spot real, primary key (ts, iid));
"""
OPT_COLS = "ts,iid,bid,ask,bid_size,ask_size,mark,bid_iv,ask_iv,mark_iv,delta,gamma,theta,vega,oi,volume,spot"


class Collector:
    def __init__(self, client, cfg=CollectorConfig, now_fn=time.time, sleep_fn=time.sleep):
        self.c, self.cfg, self.now, self.sleep = client, cfg, now_fn, sleep_fn
        self.dir = Path(cfg.DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.core = sqlite3.connect(self.dir / "core.db")
        self.core.executescript(CORE_SCHEMA)
        self.core.execute("pragma journal_mode=wal")
        self.iids = dict(self.core.execute("select symbol, id from instruments"))
        self.opt_day, self.opt = None, None
        self.last_alert, self.last_backup_day, self.errors = {}, None, 0
        self.state_path = self.dir / "collector_state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    # ------------------------------------------------------------ helpers
    def notify(self, msg, key=None, every_s=3600):
        if key:
            if self.now() - self.last_alert.get(key, 0) < every_s:
                log.info(msg)
                return
            self.last_alert[key] = self.now()
        log.info(msg)
        if self.cfg.TELEGRAM_BOT_TOKEN and self.cfg.TELEGRAM_CHAT_ID:
            try:
                requests.post(f"https://api.telegram.org/bot{self.cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
                              json={"chat_id": self.cfg.TELEGRAM_CHAT_ID, "text": f"[data] {msg}"}, timeout=5)
            except requests.RequestException:
                pass

    def send_file(self, path, caption=""):
        p = Path(path)
        if not (self.cfg.TELEGRAM_BOT_TOKEN and self.cfg.TELEGRAM_CHAT_ID):
            return False, "Telegram not configured"
        if p.stat().st_size > 48 * 1024 * 1024:
            return False, f"{p.name} is {p.stat().st_size / 1e6:.0f} MB (Telegram limit 50 MB)"
        with p.open("rb") as fh:
            r = requests.post(f"https://api.telegram.org/bot{self.cfg.TELEGRAM_BOT_TOKEN}/sendDocument",
                              data={"chat_id": self.cfg.TELEGRAM_CHAT_ID, "caption": caption[:900]},
                              files={"document": (p.name, fh)}, timeout=300)
        return r.ok, r.text[:200]

    def free_gb(self):
        return shutil.disk_usage(self.dir).free / 1e9

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def opt_db(self, ts):
        day = datetime.fromtimestamp(ts, IST).date().isoformat()
        if day != self.opt_day:
            if self.opt is not None:
                self.opt.close()
            self.opt = sqlite3.connect(self.dir / f"options_{day}.db")
            self.opt.executescript(OPT_SCHEMA)
            self.opt_day = day
        return self.opt

    # ------------------------------------------------------------ candles
    def fetch_candles(self, start, end):
        rows = []
        while start < end:
            stop = min(end, start + 1900 * 60)
            for k in self.c.candles(self.cfg.PERP, "1m", start, stop):
                rows.append((int(k["time"]), f(k["open"]), f(k["high"]), f(k["low"]), f(k["close"]),
                             f(k.get("volume"))))
            start = stop
        self.core.executemany("insert or replace into candles_1m values (?,?,?,?,?,?)",
                              [r for r in rows if None not in r[:5]])
        self.core.commit()
        return len(rows)

    def backfill(self):
        if self.state.get("backfilled"):
            return
        end = int(self.now()) // 60 * 60
        n = self.fetch_candles(end - self.cfg.BACKFILL_DAYS * 86400, end)
        self.state["backfilled"] = True
        self.save_state()
        log.info(f"backfilled {n} 1-minute candles ({self.cfg.BACKFILL_DAYS} days)")

    # ------------------------------------------------------------- market
    def depth(self, ob, mid):
        def side(levels, pct):
            tot = 0.0
            for lv in levels or []:
                px, sz = f(lv.get("price") or lv.get("limit_price")), f(lv.get("size"))
                if px and sz and abs(px / mid - 1) <= pct:
                    tot += sz
            return tot
        buys = ob.get("buy") or ob.get("bids") or []
        sells = ob.get("sell") or ob.get("asks") or []
        return side(buys, 0.001), side(sells, 0.001), side(buys, 0.005), side(sells, 0.005)

    def record_market(self, ts):
        t = self.c.ticker(self.cfg.PERP) or {}
        q = t.get("quotes") or {}
        bb, ba = f(q.get("best_bid")), f(q.get("best_ask"))
        mid = (bb + ba) / 2 if bb and ba else f(t.get("mark_price"))
        try:
            d = self.depth(self.c.orderbook(self.cfg.PERP) or {}, mid) if mid else (None,) * 4
        except DeltaAPIError:
            d = (None,) * 4
        row = (ts, f(t.get("spot_price")), f(t.get("mark_price")), f(t.get("close")), f(t.get("funding_rate")),
               f(t.get("oi")), f(t.get("oi_value_usd")), f(t.get("volume")), f(t.get("turnover_usd")),
               bb, ba, f(q.get("bid_size")), f(q.get("ask_size")), f(t.get("mark_basis")), *d)
        self.core.execute(f"insert or replace into market values ({','.join('?' * 18)})", row)
        return row

    def record_indicators(self, ts, market_row):
        rows = self.core.execute("select time, open, high, low, close from candles_1m where time >= ? order by time",
                                 (ts - 4 * 86400,)).fetchall()
        if len(rows) < 30:
            return None
        ind = indicators(rows)
        self.core.execute("insert or replace into indicators values (?,?,?,?,?,?,?,?,?,?,?)",
                          (ts, ind["close"], ind["rsi_1m"], ind["rsi_5m"], ind["rsi_15m"], ind["rsi_1h"],
                           ind["bb_z_5m"], ind["ema_240"], ind["atr_1h_pct"], market_row[4], market_row[5]))
        return ind

    # ------------------------------------------------------------ options
    def iid(self, t):
        sym = t["symbol"]
        if sym not in self.iids:
            exp = expiry_of(sym)
            kind = "put" if t.get("contract_type") == "put_options" else "call"
            cur = self.core.execute("insert or ignore into instruments (symbol, product_id, kind, strike, expiry_utc, "
                                    "expiry_type) values (?,?,?,?,?,?)",
                                    (sym, int(t.get("product_id") or 0), kind, f(t.get("strike_price")),
                                     exp.isoformat(), expiry_type(exp)))
            self.iids[sym] = cur.lastrowid or self.core.execute(
                "select id from instruments where symbol=?", (sym,)).fetchone()[0]
        return self.iids[sym]

    def record_options(self, ts):
        free = self.free_gb()
        if free < self.cfg.STOP_FREE_GB:
            self.notify(f"disk nearly full ({free:.1f} GB free): option recording paused", "disk", 6 * 3600)
            return 0
        chain = [t for t in (self.c.all_options(self.cfg.UNDERLYING) or [])
                 if t.get("contract_type") in ("call_options", "put_options")]
        now = datetime.fromtimestamp(ts, timezone.utc)
        exps = sorted({expiry_of(t["symbol"]) for t in chain if expiry_of(t["symbol"]) > now})
        near = set(exps[:self.cfg.NEAR_EXPIRIES])
        far_now = (ts // 60) % self.cfg.FAR_EVERY_MIN == 0 and free >= self.cfg.MIN_FREE_GB
        if free < self.cfg.MIN_FREE_GB:
            self.notify(f"disk low ({free:.1f} GB free): recording near expiries only", "disk", 6 * 3600)
        rows = []
        for t in chain:
            e = expiry_of(t["symbol"])
            if e <= now or (e not in near and not far_now):
                continue
            q, g = t.get("quotes") or {}, t.get("greeks") or {}
            bid, ask, oi = f(q.get("best_bid")), f(q.get("best_ask")), f(t.get("oi"))
            if not (bid or ask or oi):
                continue
            rows.append((ts, self.iid(t), bid, ask, f(q.get("bid_size")), f(q.get("ask_size")), f(t.get("mark_price")),
                         f(q.get("bid_iv")), f(q.get("ask_iv")), f(q.get("mark_iv")), f(g.get("delta")),
                         f(g.get("gamma")), f(g.get("theta")), f(g.get("vega")), oi, f(t.get("volume")),
                         f(t.get("spot_price"))))
        self.core.commit()
        db = self.opt_db(ts)
        db.executemany(f"insert or replace into option_snaps ({OPT_COLS}) values ({','.join('?' * 17)})", rows)
        db.commit()
        return len(rows)

    # -------------------------------------------------------- settlements
    def record_settlements(self, ts):
        """For expiries that passed in the last 2 days and aren't recorded yet."""
        done = {r[0] for r in self.core.execute("select expiry_utc from settlements")}
        now = datetime.fromtimestamp(ts, timezone.utc)
        rows = self.core.execute("select expiry_utc, expiry_type, min(symbol) from instruments group by expiry_utc").fetchall()
        for exp_s, etype, sym in rows:
            exp = datetime.fromisoformat(exp_s)
            if exp_s in done or not (exp + timedelta(minutes=15) <= now <= exp + timedelta(days=2)):
                continue
            try:
                sp = f((self.c.product(sym) or {}).get("settlement_price"))
            except DeltaAPIError:
                sp = None
            ref = self.core.execute("select close from candles_1m where time <= ? order by time desc limit 1",
                                    (int(exp.timestamp()),)).fetchone()
            self.core.execute("insert or replace into settlements values (?,?,?,?,?)",
                              (exp_s, etype, sp, ref[0] if ref else None, ts))
        self.core.commit()

    # ------------------------------------------------------- maintenance
    def maintain(self, ts):
        """Gzip finished option days; send yesterday's file to Telegram once a day."""
        today = datetime.fromtimestamp(ts, IST).date()
        for p in sorted(self.dir.glob("options_*.db")):
            day = p.stem.split("_", 1)[1]
            if day >= today.isoformat():
                continue
            if self.opt_day == day and self.opt is not None:
                self.opt.close()
                self.opt, self.opt_day = None, None
            gz = p.with_suffix(".db.gz")
            with p.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            p.unlink()
            for extra in (p.with_suffix(".db-wal"), p.with_suffix(".db-shm")):
                extra.unlink(missing_ok=True)
            log.info(f"compressed {p.name} -> {gz.name} ({gz.stat().st_size / 1e6:.1f} MB)")
        hour = datetime.fromtimestamp(ts, IST).hour
        y = (today - timedelta(days=1)).isoformat()
        if (self.cfg.TELEGRAM_BACKUP and hour >= self.cfg.BACKUP_HOUR_IST and self.state.get("last_backup") != y
                and (self.dir / f"options_{y}.db.gz").exists()):
            core_gz = self.dir / "core_latest.db.gz"
            self.core.commit()
            snap = self.dir / "core_snapshot.db"
            bk = sqlite3.connect(snap)
            self.core.backup(bk)
            bk.close()
            with snap.open("rb") as src, gzip.open(core_gz, "wb") as dst:
                shutil.copyfileobj(src, dst)
            snap.unlink()
            ok1, why1 = self.send_file(self.dir / f"options_{y}.db.gz", f"BTC options data {y}")
            ok2, why2 = self.send_file(core_gz, f"BTC candles/indicators/market up to {y}")
            self.state["last_backup"] = y
            self.save_state()
            if not (ok1 and ok2):
                self.notify(f"daily backup to Telegram incomplete: {why1 if not ok1 else why2}", "backup", 86400)

    # -------------------------------------------------------------- loop
    def step(self):
        ts = int(self.now()) // 60 * 60
        self.fetch_candles(ts - 10 * 60, ts + 60)
        m = self.record_market(ts)
        self.record_indicators(ts, m)
        self.core.commit()
        n = self.record_options(ts)
        if ts % 900 == 0:
            self.record_settlements(ts)
        if ts % 3600 == 0:
            self.maintain(ts)
            log.info(f"ok: {n} option rows this minute, {self.free_gb():.1f} GB free")
        return n

    def run(self):
        self.backfill()
        self.maintain(int(self.now()))
        self.notify(f"Data recorder started: BTC perp, indicators and all option expiries every minute. "
                    f"{self.free_gb():.1f} GB free.")
        while True:
            try:
                self.step()
                self.errors = 0
            except DeltaAPIError as e:
                self.errors += 1
                log.warning(f"API error: {e}")
            except Exception:
                self.errors += 1
                log.exception("unexpected error")
            if self.errors >= 10:
                self.notify("data recorder: 10 failures in a row, check the log", "errs", 3600)
            nxt = (int(self.now()) // 60 + 1) * 60 + 5        # 5 s past each minute
            self.sleep(max(1, nxt - self.now()))

    # ----------------------------------------------------------- reports
    def status(self):
        q = lambda sql: self.core.execute(sql).fetchone()
        lines = []
        c = q("select count(*), min(time), max(time) from candles_1m")
        fmt = lambda t: datetime.fromtimestamp(t, IST).strftime("%d %b %Y %H:%M") if t else "-"
        lines.append(f"candles 1m: {c[0]:,} ({fmt(c[1])} -> {fmt(c[2])})")
        m = q("select count(*), max(ts) from market")
        lines.append(f"market snapshots: {m[0]:,} (last {fmt(m[1])})")
        i = q("select ts, close, rsi_1m, rsi_5m, rsi_1h, bb_z_5m, funding_rate from indicators order by ts desc limit 1")
        if i:
            lines.append(f"latest indicators {fmt(i[0])}: BTC {i[1]:,.0f}  RSI 1m {i[2] or 0:.0f} / 5m {i[3] or 0:.0f} / "
                         f"1h {i[4] or 0:.0f}  BB z {i[5] or 0:+.2f}  funding {i[6] or 0:.5f}")
        for et, n in self.core.execute("select expiry_type, count(distinct expiry_utc) from instruments group by 1"):
            lines.append(f"expiries seen, {et}: {n}")
        lines.append(f"settlements recorded: {q('select count(*) from settlements')[0]}")
        files = sorted(self.dir.glob("options_*"))
        size = sum(p.stat().st_size for p in self.dir.iterdir() if p.is_file()) / 1e6
        lines.append(f"option day files: {len(files)} ({files[0].name if files else '-'} .. "
                     f"{files[-1].name if files else '-'}); total data {size:,.0f} MB; {self.free_gb():.1f} GB free")
        return "\n".join(lines)

    def export(self, start, end):
        """Merge option days start..end (IST dates) into one gzipped sqlite with core tables included."""
        out = self.dir / f"export_{start}_to_{end}.db"
        out.unlink(missing_ok=True)
        dst = sqlite3.connect(out)
        self.core.commit()
        self.core.backup(dst)
        dst.executescript(OPT_SCHEMA)
        d = datetime.fromisoformat(start).date()
        while d <= datetime.fromisoformat(end).date():
            for p in (self.dir / f"options_{d}.db", self.dir / f"options_{d}.db.gz"):
                if not p.exists():
                    continue
                src = p
                if p.suffix == ".gz":
                    src = self.dir / f"_tmp_{d}.db"
                    with gzip.open(p, "rb") as a, src.open("wb") as b:
                        shutil.copyfileobj(a, b)
                dst.execute(f"attach database '{src}' as s")
                dst.execute("insert or ignore into option_snaps select * from s.option_snaps")
                dst.commit()
                dst.execute("detach database s")
                if src != p:
                    src.unlink()
            d += timedelta(days=1)
        dst.close()
        gz = out.with_suffix(".db.gz")
        with out.open("rb") as a, gzip.open(gz, "wb") as b:
            shutil.copyfileobj(a, b)
        out.unlink()
        return gz


def setup_logging(d):
    Path(d).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(Path(d) / "collector.log", maxBytes=3_000_000, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]
    log.setLevel(logging.INFO)


class PublicClient(DeltaClient):
    """Public endpoints only — the recorder needs no API key."""
    def all_options(self, underlying):
        return self._request("GET", "/v2/tickers", {"contract_types": "call_options,put_options",
                                                     "underlying_asset_symbols": underlying}) or []

    def orderbook(self, symbol, depth=50):
        return self._request("GET", f"/v2/l2orderbook/{symbol}", {"depth": depth})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--export", nargs=2, metavar=("START", "END"))
    ap.add_argument("--send", metavar="FILE")
    a = ap.parse_args()
    cfg = CollectorConfig
    setup_logging(cfg.DATA_DIR)
    col = Collector(PublicClient(env=cfg.DELTA_ENV), cfg)
    if a.status:
        print(col.status())
    elif a.export:
        gz = col.export(*a.export)
        print(f"{gz}  ({gz.stat().st_size / 1e6:.1f} MB)")
    elif a.send:
        print(col.send_file(a.send, Path(a.send).name))
    else:
        col.run()


if __name__ == "__main__":
    main()
