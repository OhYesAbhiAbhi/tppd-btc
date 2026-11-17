"""Option-flow bias trade on BTC futures: LIVE on Thursdays (small size), PAPER on every other night.

Rule (btc-putcall-bias-nights, btc-bias-entry-filters, Thursday check of 16 Nov 2026):
  At BIAS_ENTRY_IST (22:30 IST), once a night:
  * Signal: in the last 3 h, Deribit options expiring within 48 h with strikes within 4% of BTC:
      puts >= BIAS_PUT_SHARE (75%) of volume and Supertrend (10,3, 1 h) down  -> SHORT
      puts <= 1 - BIAS_PUT_SHARE (25%)       and Supertrend up                -> LONG
  * Nights: Thursdays (the strangle never trades them), and other nights only when the strangle's calm
    test fails at 22:30 (1 h <= 30%, 6 h <= 30%, 24 h <= 40% realised vol = calm = strangle night).
  * Filters (recorded every time; the crowd filter decides the live Thursday trade):
      crowd:  Binance all-account long/short ratio >= BIAS_CROWD_MIN (1.3)
      spent:  Binance taker-buy share of the last 3 h is no more than BIAS_SPOTFLOW_MAX (1.5) points past
              50% in our direction (the move is not already spent)
  * LIVE only when: BIAS_MODE=live, the night is in BIAS_LIVE_DAYS (Thu), signal, crowd filter passes.
    Everything else with a signal is a PAPER trade (no orders), so the rule is tracked on all nights.
Exit (btc-reversal-exit-signals):
  * expected move EM = 24 h realised vol x sqrt(hours to the next 17:30 IST / 8760)
    target BIAS_TP_EM (1.5) x EM, stop BIAS_SL_EM (0.75) x EM, measured from the entry price;
  * early exit, checked every 15 min: Binance open interest down >= 1.5% in 1 h while price moved >= 0.75%
    our way (leverage flushed), or 15-min RSI back from an extreme (short: was < 25, now > 30; long mirrored);
  * time exit at the next 17:30 IST.
  Live trades also rest a reduce-only stop order on Delta at the stop price, in case this program stops.
Costs: Delta futures taker fee 0.05% + 18% GST each side.

Also PAPER ONLY (17 Nov 2026, btc-who-wins-volatile-nights): on every night the strangle skips for volatility
(not calm at 22:30, not Thursday), "sell" a wide ~BIAS_WIDE_DELTA (0.10) strangle for the next 17:30 expiry at
Delta's REAL best bids, BIAS_WIDE_LOTS (200) lots. Every 5 min it marks the cost to buy back at Delta's real best
asks (plus fees); if the loss reaches the premium collected (BIAS_WIDE_STOP x credit) it records a stop, else it
settles at Delta's settlement price. No orders are ever sent for this. Results in data_bias/wide.csv.

    python bias_trader.py            # run forever (service tppd-bias)
    python bias_trader.py --once     # readings and what it would do right now (never trades)
    python bias_trader.py --report   # results: live and paper, by filter
"""
import argparse
import csv
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import condor as cd
from condor import IST, CondorConfig, DeltaAPIError, DeltaClient, log, setup_logging


def _env(name, default, cast=str):
    return cd._env(name, default, cast)


class BiasConfig:
    MODE = _env("BIAS_MODE", "paper")                       # paper | live (live only on BIAS_LIVE_DAYS)
    LIVE_DAYS = _env("BIAS_LIVE_DAYS", "Thu")
    LOTS = _env("BIAS_LOTS", 50, int)                       # BTCUSD perpetual contracts (0.001 BTC each)
    SYMBOL = _env("BIAS_SYMBOL", "BTCUSD")
    ENTRY_IST = _env("BIAS_ENTRY_IST", "22:30")
    WINDOW_MIN = _env("BIAS_WINDOW_MIN", 20, int)
    PUT_SHARE = _env("BIAS_PUT_SHARE", 0.75, float)
    CROWD_MIN = _env("BIAS_CROWD_MIN", 1.3, float)
    SPOTFLOW_MAX = _env("BIAS_SPOTFLOW_MAX", 1.5, float)
    TP_EM = _env("BIAS_TP_EM", 1.5, float)
    SL_EM = _env("BIAS_SL_EM", 0.75, float)
    CALM_RV1 = _env("CONDOR_DAMP_RV1", 30.0, float)
    CALM_RV6 = _env("CONDOR_DAMP_RV6", 30.0, float)
    CALM_RV24 = _env("CONDOR_DAMP_RV24", 40.0, float)
    FEE = 0.0005 * 1.18
    CHECK_SECONDS = _env("BIAS_CHECK_SECONDS", 30, int)
    DATA_DIR = _env("BIAS_DATA_DIR", str(Path(__file__).with_name("data_bias")))
    DELTA_ENV = CondorConfig.DELTA_ENV
    API_KEY = CondorConfig.API_KEY
    API_SECRET = CondorConfig.API_SECRET
    TELEGRAM_BOT_TOKEN = CondorConfig.TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID = CondorConfig.TELEGRAM_CHAT_ID
    WIDE = _env("BIAS_WIDE", True, bool)                    # paper wide strangle on skipped nights
    WIDE_DELTA = _env("BIAS_WIDE_DELTA", 0.10, float)
    WIDE_LOTS = _env("BIAS_WIDE_LOTS", 200, int)
    WIDE_STOP = _env("BIAS_WIDE_STOP", 1.0, float)          # stop when the loss reaches this x the credit
    DERIBIT = "https://www.deribit.com/api/v2/public/"
    BINANCE = "https://fapi.binance.com"


COLS = ["night", "day", "mode", "side", "lots", "entry_ist", "entry_px", "put_share", "crowd_ls", "spot_flow",
        "crowd_ok", "not_spent", "calm", "tp_pct", "sl_pct", "exit_ist", "exit_px", "reason", "net_usd"]
WIDE_COLS = ["night", "day", "expiry", "lots", "spot", "put", "call", "put_bid", "call_bid", "put_bid_iv",
             "call_bid_iv", "credit_usd", "v1", "v6", "v24", "exit_ist", "reason", "settle", "net_usd"]
MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                   "DEC"], 1)}


def realised(closes, minutes):
    x = closes[-(minutes + 1):]
    if len(x) < 0.8 * minutes:
        return None
    r = [math.log(b / a) for a, b in zip(x[:-1], x[1:])]
    m = sum(r) / len(r)
    return math.sqrt(sum((y - m) ** 2 for y in r) / max(len(r) - 1, 1)) * math.sqrt(525600) * 100


def supertrend(highs, lows, closes, period=10, mult=3.0):
    """Direction (+1 up / -1 down) of Supertrend on completed bars."""
    n = len(closes)
    if n < period + 2:
        return None
    tr = [highs[0] - lows[0]] + [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                                 for i in range(1, n)]
    atr = tr[0]
    d, fu, fl = 1, None, None
    for i in range(n):
        atr = tr[i] if i == 0 else atr + (tr[i] - atr) / period
        mid = (highs[i] + lows[i]) / 2
        ub, lb = mid + mult * atr, mid - mult * atr
        if i == 0:
            fu, fl = ub, lb
            continue
        fu = ub if (ub < fu or closes[i - 1] > fu) else fu
        fl = lb if (lb > fl or closes[i - 1] < fl) else fl
        if d == 1 and closes[i] < fl:
            d = -1
        elif d == -1 and closes[i] > fu:
            d = 1
    return d


def rsi(closes, period=14):
    if len(closes) < period + 2:
        return None
    up = dn = None
    for a, b in zip(closes[:-1], closes[1:]):
        g, l = max(b - a, 0), max(a - b, 0)
        up = g if up is None else up + (g - up) / period
        dn = l if dn is None else dn + (l - dn) / period
    return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)


def put_share(trades, now_ts):
    """Share of puts in near-dated (<= 48 h), near-the-money (+-4%) Deribit option volume."""
    put = tot = 0.0
    for t in trades:
        m = re.match(r"BTC-(\d+)([A-Z]{3})(\d+)-(\d+)-([CP])", t.get("instrument_name", ""))
        if not m:
            continue
        exp = datetime(2000 + int(m[3]), MON[m[2]], int(m[1]), 8, tzinfo=timezone.utc).timestamp()
        idx = float(t.get("index_price") or 0)
        if not (0 < exp - now_ts <= 48 * 3600) or idx <= 0 or abs(int(m[4]) / idx - 1) > 0.04:
            continue
        amt = float(t.get("amount") or 0)
        tot += amt
        put += amt if m[5] == "P" else 0
    return (put / tot if tot else None), tot


class BiasTrader:
    def __init__(self, client, cfg=BiasConfig, now_fn=time.time, sleep_fn=time.sleep, http=None):
        self.c, self.cfg, self.now, self.sleep = client, cfg, now_fn, sleep_fn
        self.http = http or requests.Session()
        self.dir = Path(cfg.DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path, self.trades_path = self.dir / "state.json", self.dir / "trades.csv"
        self.state = (json.loads(self.state_path.read_text()) if self.state_path.exists()
                      else {"last_night": None, "open": []})
        self.state.setdefault("wide", [])
        self.wide_path = self.dir / "wide.csv"
        self.pid = None

    # ------------------------------------------------------------- basics
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
                              json={"chat_id": self.cfg.TELEGRAM_CHAT_ID, "text": f"[bias] {msg}"}, timeout=5)
            except requests.RequestException:
                pass

    def get_json(self, url, timeout=20):
        r = self.http.get(url, timeout=timeout, headers={"User-Agent": "tppd-bias/1.0"})
        r.raise_for_status()
        return r.json()

    def live_day(self):
        days = [d.strip()[:3].lower() for d in self.cfg.LIVE_DAYS.split(",") if d.strip()]
        return self.ist().strftime("%a").lower() in days

    def in_window(self):
        now = self.ist()
        h, m = (int(x) for x in self.cfg.ENTRY_IST.split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return start <= now < start + timedelta(minutes=self.cfg.WINDOW_MIN)

    def expiry_after(self, ts):
        t = datetime.fromtimestamp(ts, IST)
        e = t.replace(hour=17, minute=30, second=0, microsecond=0)
        return e if e > t + timedelta(hours=1) else e + timedelta(days=1)

    # ---------------------------------------------------------- readings
    def price(self):
        t = self.c.ticker(self.cfg.SYMBOL)
        return float(t.get("mark_price") or t.get("spot_price") or 0)

    def candles(self, res, minutes_back):
        end = int(self.now())
        return self.c.candles(self.cfg.SYMBOL, res, end - minutes_back * 60, end)

    def vols(self):
        cs = self.candles("1m", 1443)
        cl = [float(c["close"]) for c in cs if float(c.get("close") or 0) > 0]
        return realised(cl, 60), realised(cl, 360), realised(cl, 1440)

    def st_dir(self):
        cs = self.candles("1h", 14 * 24 * 60)[:-1]               # completed hours only
        return supertrend([float(c["high"]) for c in cs], [float(c["low"]) for c in cs],
                          [float(c["close"]) for c in cs])

    def rsi15(self):
        cs = self.candles("15m", 2 * 24 * 60)
        return rsi([float(c["close"]) for c in cs])

    def deribit_put_share(self):
        end = int(self.now() * 1000)
        t, trades = end - 3 * 3600 * 1000, []
        for _ in range(60):
            r = self.get_json(f"{self.cfg.DERIBIT}get_last_trades_by_currency_and_time?currency=BTC&kind=option"
                              f"&start_timestamp={t}&end_timestamp={end}&count=1000&sorting=asc")["result"]
            trades += r["trades"]
            if not r.get("has_more") or not r["trades"]:
                break
            t = r["trades"][-1]["timestamp"] + 1
        return put_share(trades, self.now())

    def crowd(self):
        r = self.get_json(f"{self.cfg.BINANCE}/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1")
        return float(r[-1]["longShortRatio"])

    def taker_share(self):
        k = self.get_json(f"{self.cfg.BINANCE}/fapi/v1/klines?symbol=BTCUSDT&interval=1m&limit=180")
        v = sum(float(x[5]) for x in k)
        return sum(float(x[9]) for x in k) / v if v else None

    def oi_change_1h(self):
        r = self.get_json(f"{self.cfg.BINANCE}/futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=13")
        return (float(r[-1]["sumOpenInterest"]) / float(r[0]["sumOpenInterest"]) - 1) * 100

    def readings(self):
        """Everything the rule needs. Missing pieces are None (never raise)."""
        out = {}
        for k, fn in (("vols", self.vols), ("st", self.st_dir), ("ps", self.deribit_put_share),
                      ("crowd", self.crowd), ("taker", self.taker_share), ("px", self.price)):
            try:
                out[k] = fn()
            except Exception as e:                                # network / API / layout problems
                out[k] = None
                log.warning(f"bias reading {k} failed: {e}")
        return out

    # -------------------------------------------------------------- entry
    def decide(self, r):
        """(side or 0, info dict, why)."""
        v1, v6, v24 = r["vols"] or (None, None, None)
        calm = None not in (v1, v6, v24) and v1 <= self.cfg.CALM_RV1 and v6 <= self.cfg.CALM_RV6 \
            and v24 <= self.cfg.CALM_RV24
        thursday = self.ist().strftime("%a") == "Thu"
        ps, vol = r["ps"] if r["ps"] else (None, 0)
        info = {"calm": calm, "thursday": thursday, "put_share": ps, "opt_vol": vol, "st": r["st"],
                "crowd": r["crowd"], "v1": v1, "v6": v6, "v24": v24}
        if calm and not thursday:
            return 0, info, "calm night: the strangle trades it"
        if ps is None or r["st"] is None or r["px"] is None:
            return 0, info, "readings missing (option flow, Supertrend or price)"
        if ps >= self.cfg.PUT_SHARE and r["st"] < 0:
            side = -1
        elif ps <= 1 - self.cfg.PUT_SHARE and r["st"] > 0:
            side = 1
        else:
            return 0, info, f"no signal (puts {ps:.0%} of option volume, Supertrend {'up' if r['st'] > 0 else 'down'})"
        flow = None if r["taker"] is None else (r["taker"] - 0.5) * 100 * side
        info.update(spot_flow=flow, crowd_ok=(r["crowd"] is not None and r["crowd"] >= self.cfg.CROWD_MIN),
                    not_spent=(flow is not None and flow <= self.cfg.SPOTFLOW_MAX))
        return side, info, "signal"

    def enter(self, side, info, r):
        cfg = self.cfg
        live = cfg.MODE == "live" and self.live_day() and info["crowd_ok"]
        px = r["px"]
        exp = self.expiry_after(self.now())
        hours = (exp.timestamp() - self.now()) / 3600
        em = (info["v24"] or 40.0) / 100 * math.sqrt(hours / 8760) * 100
        pos = {"night": self.ist().date().isoformat(), "day": self.ist().strftime("%a"), "side": side,
               "mode": "live" if live else "paper", "lots": cfg.LOTS, "entry_ts": self.now(), "entry_px": px,
               "expiry_ts": exp.timestamp(), "tp_pct": round(cfg.TP_EM * em, 3), "sl_pct": round(cfg.SL_EM * em, 3),
               "put_share": info["put_share"], "crowd": info["crowd"], "spot_flow": info["spot_flow"],
               "crowd_ok": info["crowd_ok"], "not_spent": info["not_spent"], "calm": info["calm"],
               "rsi_ext": False, "last_check": 0}
        if live:
            try:
                pid = self.product_id()
                o = self.c.place_order(product_id=pid, size=cfg.LOTS, side="sell" if side < 0 else "buy",
                                       order_type="market_order",
                                       client_order_id=f"bias-in-{int(self.now() * 1000)}")
                pos["entry_px"] = float(o.get("average_fill_price") or px)
                filled = int(o.get("size", cfg.LOTS)) - int(o.get("unfilled_size", 0))
                if filled <= 0:
                    raise DeltaAPIError("entry order not filled")
                pos["lots"] = filled
            except DeltaAPIError as e:
                self.notify(f"Thursday LIVE entry FAILED ({e}); tracking it as paper instead.")
                pos["mode"] = "paper"
            if pos["mode"] == "live":
                stop = pos["entry_px"] * (1 - side * pos["sl_pct"] / 100)
                try:
                    so = self.c.place_order(product_id=pid, size=pos["lots"], side="buy" if side < 0 else "sell",
                                            order_type="market_order", stop_order_type="stop_loss_order",
                                            stop_price=f"{stop:.1f}", reduce_only=True,
                                            client_order_id=f"bias-sl-{int(self.now() * 1000)}")
                    pos["stop_order_id"] = so.get("id")
                except DeltaAPIError as e:
                    self.notify(f"WARNING: could not place the exchange stop order ({e}); the program's own stop "
                                f"still applies while it runs.")
        self.state["open"].append(pos)
        self.save()
        f = lambda x, s="": "n/a" if x is None else f"{x:.2f}{s}"
        self.notify(f"{'LIVE' if pos['mode'] == 'live' else 'PAPER'} {'SHORT' if side < 0 else 'LONG'} "
                    f"x{pos['lots']} BTCUSD at {pos['entry_px']:,.0f} ({pos['day']}). Puts {info['put_share']:.0%} of "
                    f"option volume, Supertrend {'down' if side < 0 else 'up'}. Crowd L/S {f(info['crowd'])} "
                    f"({'pass' if info['crowd_ok'] else 'fail'}), flow our way {f(info['spot_flow'], ' pts')} "
                    f"({'pass' if info['not_spent'] else 'fail'}). Target {pos['tp_pct']:.2f}%, stop "
                    f"{pos['sl_pct']:.2f}%, out by {datetime.fromtimestamp(pos['expiry_ts'], IST):%a %H:%M}.")

    def product_id(self):
        if self.pid is None:
            self.pid = int(self.c.product(self.cfg.SYMBOL)["id"])
        return self.pid

    # --------------------------------------------------------------- exit
    def manage(self, pos):
        px = self.price()
        if not px:
            return
        side = pos["side"]
        move = (px / pos["entry_px"] - 1) * 100 * side
        reason = None
        if move <= -pos["sl_pct"]:
            reason = "stop"
        elif move >= pos["tp_pct"]:
            reason = "target"
        elif self.now() >= pos["expiry_ts"]:
            reason = "time (17:30)"
        elif self.now() - pos.get("last_check", 0) >= 15 * 60 - 5:
            pos["last_check"] = self.now()
            r = None
            try:
                r = self.rsi15()
            except Exception as e:
                log.warning(f"rsi failed: {e}")
            if r is not None:
                if (side < 0 and r < 25) or (side > 0 and r > 75):
                    pos["rsi_ext"] = True
                elif pos["rsi_ext"] and ((side < 0 and r > 30) or (side > 0 and r < 70)):
                    reason = f"RSI back from extreme ({r:.0f})"
            if reason is None:
                try:
                    oi = self.oi_change_1h()
                    hour_ago = [float(c["close"]) for c in self.candles("1m", 62)]
                    px_1h = (px / hour_ago[0] - 1) * 100 * side if hour_ago else 0
                    if oi <= -1.5 and px_1h >= 0.75:
                        reason = f"OI flush (open interest {oi:.1f}% in 1 h)"
                except Exception as e:
                    log.warning(f"OI check failed: {e}")
            self.save()
        if reason:
            self.close(pos, px, reason)

    def close(self, pos, px, reason):
        if pos["mode"] == "live":
            pid = self.product_id()
            if pos.get("stop_order_id"):
                try:
                    self.c.cancel_order(pos["stop_order_id"], pid)
                except DeltaAPIError:
                    pass
            try:
                held = [p for p in self.c.positions() if int(p.get("product_id", -1)) == pid]
                size = abs(int(float(held[0].get("size", 0)))) if held else 0
            except DeltaAPIError:
                size = pos["lots"]
            if size:
                try:
                    o = self.c.place_order(product_id=pid, size=min(size, pos["lots"]),
                                           side="buy" if pos["side"] < 0 else "sell", order_type="market_order",
                                           reduce_only=True, client_order_id=f"bias-out-{int(self.now() * 1000)}")
                    px = float(o.get("average_fill_price") or px)
                except DeltaAPIError as e:
                    self.notify(f"WARNING: could not close the LIVE BTCUSD position ({e}). Check Delta NOW.")
                    return
            else:
                reason += " (exchange stop had already closed it)"
        units = pos["lots"] * 0.001
        net = (px - pos["entry_px"]) * pos["side"] * units - self.cfg.FEE * units * (px + pos["entry_px"])
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(COLS)
            w.writerow([pos["night"], pos["day"], pos["mode"], "short" if pos["side"] < 0 else "long", pos["lots"],
                        datetime.fromtimestamp(pos["entry_ts"], IST).strftime("%Y-%m-%d %H:%M"), round(pos["entry_px"], 1),
                        pos["put_share"], pos["crowd"], pos["spot_flow"], pos["crowd_ok"], pos["not_spent"], pos["calm"],
                        pos["tp_pct"], pos["sl_pct"], self.ist().strftime("%Y-%m-%d %H:%M"), round(px, 1), reason,
                        round(net, 2)])
        self.state["open"] = [p for p in self.state["open"] if p is not pos]
        self.save()
        self.notify(f"{pos['mode'].upper()} {'short' if pos['side'] < 0 else 'long'} closed at {px:,.0f}: {reason}. "
                    f"Net {net:+.2f} USD (x{pos['lots']}). {self.summary()}")

    def summary(self):
        if not self.trades_path.exists():
            return ""
        with self.trades_path.open() as f:
            rows = list(csv.DictReader(f))
        live = [float(r["net_usd"]) for r in rows if r["mode"] == "live"]
        paper = [float(r["net_usd"]) for r in rows if r["mode"] == "paper"]
        return f"Live {len(live)} trades {sum(live):+.2f}; paper {len(paper)} trades {sum(paper):+.2f} USD."

    # ------------------------------------------------- paper wide strangle
    def wide_chain(self, exp):
        return cd.st.parse_chain(self.c.option_chain("BTC", exp.strftime("%d-%m-%Y")), exp.strftime("%d%m%y"))

    def opt_quote(self, symbol):
        q = (self.c.ticker(symbol) or {}).get("quotes") or {}
        return cd.st._f(q.get("best_bid")), cd.st._f(q.get("best_ask"))

    def opt_settle(self, symbol):
        s = cd.st._f((self.c.product(symbol) or {}).get("settlement_price"))
        return s if s > 1000 else None

    def ofee(self, price, spot):
        return min(0.0001 * spot, 0.035 * price) * 1.18 if price > 0 else 0.0

    def wide_open(self, info, r):
        cfg = self.cfg
        exp = self.expiry_after(self.now())
        try:
            chain = self.wide_chain(exp)
        except Exception as e:
            log.warning(f"wide chain failed: {e}")
            return
        spot = r.get("px") or 0
        legs = {}
        for kind in ("put", "call"):
            pool = [c for c in chain if c.kind == kind and c.bid > 0 and c.ask > 0 and c.delta
                    and ((c.strike < spot) if kind == "put" else (c.strike > spot))]
            if not pool:
                return
            legs[kind] = min(pool, key=lambda c: abs(abs(c.delta) - cfg.WIDE_DELTA))
        p, c = legs["put"], legs["call"]
        u = cfg.WIDE_LOTS * 0.001
        credit = p.bid + c.bid
        pos = {"night": self.ist().date().isoformat(), "day": self.ist().strftime("%a"), "expiry_ts": exp.timestamp(),
               "expiry": exp.strftime("%Y-%m-%d %H:%M"), "lots": cfg.WIDE_LOTS, "spot": spot,
               "put": {"symbol": p.symbol, "strike": p.strike, "bid": p.bid, "iv": getattr(p, "iv", None)},
               "call": {"symbol": c.symbol, "strike": c.strike, "bid": c.bid, "iv": getattr(c, "iv", None)},
               "credit": credit, "fees_in": self.ofee(p.bid, spot) + self.ofee(c.bid, spot),
               "v": [info.get("v1"), info.get("v6"), info.get("v24")], "last_mark": 0}
        self.state["wide"].append(pos)
        self.save()
        self.notify(f"PAPER wide strangle (skipped night): sold put {p.strike:.0f} @ {p.bid:.1f} + call {c.strike:.0f} @ "
                    f"{c.bid:.1f} at Delta's real bids, x{cfg.WIDE_LOTS} = ${credit * u:.2f} premium. Stop if the loss "
                    f"reaches the premium; else held to {exp:%d %b %H:%M}. No orders.")

    def wide_manage(self, pos):
        u = pos["lots"] * 0.001
        if self.now() >= pos["expiry_ts"] + 10 * 60:
            S = None
            try:
                S = self.opt_settle(pos["put"]["symbol"])
            except Exception:
                pass
            if S is None:
                try:
                    S = self.price()
                except Exception:
                    return
            iv = max(pos["put"]["strike"] - S, 0) + max(S - pos["call"]["strike"], 0)
            net = (pos["credit"] - iv - pos["fees_in"] - self.ofee(iv, pos["spot"])) * u
            return self.wide_close(pos, "expiry", S, net)
        if self.now() >= pos["expiry_ts"] or self.now() - pos.get("last_mark", 0) < 5 * 60 - 5:
            return
        pos["last_mark"] = self.now()
        try:
            _, pa = self.opt_quote(pos["put"]["symbol"])
            _, ca = self.opt_quote(pos["call"]["symbol"])
        except Exception as e:
            log.warning(f"wide mark failed: {e}")
            return
        if pa <= 0 or ca <= 0:
            return
        pnl = (pos["credit"] - pa - ca - pos["fees_in"] - self.ofee(pa, pos["spot"]) - self.ofee(ca, pos["spot"])) * u
        pos["worst"] = min(pos.get("worst", 0.0), pnl)
        self.save()
        if pnl <= -self.cfg.WIDE_STOP * pos["credit"] * u:
            self.wide_close(pos, f"stop (asks {pa:.1f} + {ca:.1f})", None, pnl)

    def wide_close(self, pos, reason, settle, net):
        new = not self.wide_path.exists()
        with self.wide_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(WIDE_COLS)
            v = pos.get("v") or [None, None, None]
            w.writerow([pos["night"], pos["day"], pos["expiry"], pos["lots"], round(pos["spot"], 1), pos["put"]["strike"],
                        pos["call"]["strike"], pos["put"]["bid"], pos["call"]["bid"], pos["put"]["iv"], pos["call"]["iv"],
                        round(pos["credit"] * pos["lots"] * 0.001, 2), *[None if x is None else round(x, 1) for x in v],
                        self.ist().strftime("%Y-%m-%d %H:%M"), reason, settle, round(net, 2)])
        self.state["wide"] = [p for p in self.state["wide"] if p is not pos]
        self.save()
        with self.wide_path.open() as f:
            tot = [float(r["net_usd"]) for r in csv.DictReader(f)]
        self.notify(f"PAPER wide strangle {pos['night']} closed: {reason}. Net {net:+.2f} USD. "
                    f"Wide paper total: {len(tot)} nights, {sum(tot):+.2f} USD.")

    # --------------------------------------------------------------- loop
    def step(self):
        for pos in list(self.state["open"]):
            self.manage(pos)
        for pos in list(self.state["wide"]):
            try:
                self.wide_manage(pos)
            except Exception:
                log.exception("wide paper error")
        night = self.ist().date().isoformat()
        if not self.in_window() or self.state.get("last_night") == night:
            return
        self.state["last_night"] = night
        self.save()
        r = self.readings()
        side, info, why = self.decide(r)
        if (self.cfg.WIDE and info.get("calm") is False and info.get("v24") is not None and not info.get("thursday")
                and not any(p["night"] == night for p in self.state["wide"])):
            self.wide_open(info, r)
        if side:
            self.enter(side, info, r)
        elif info["thursday"]:
            self.notify(f"Thursday: no bias trade tonight - {why}.")
        else:
            log.info(f"bias {night}: {why}")

    def run(self):
        self.notify(f"Bias trader started ({self.cfg.MODE}; live on {self.cfg.LIVE_DAYS} with crowd filter, "
                    f"{self.cfg.LOTS} lots; paper on other nights). Check at {self.cfg.ENTRY_IST} IST.")
        while True:
            try:
                self.step()
            except Exception:
                log.exception("unexpected error")
            self.sleep(self.cfg.CHECK_SECONDS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    cfg = BiasConfig
    setup_logging(cfg.DATA_DIR)
    bot = BiasTrader(DeltaClient(cfg.API_KEY, cfg.API_SECRET, cfg.DELTA_ENV))
    if a.report:
        print(bot.summary() or "No trades yet.")
        if bot.trades_path.exists():
            with bot.trades_path.open() as f:
                rows = list(csv.DictReader(f))
            for lab, keep in (("all signals", lambda r: True), ("crowd filter", lambda r: r["crowd_ok"] == "True"),
                              ("not-spent filter", lambda r: r["not_spent"] == "True"),
                              ("Thursdays", lambda r: r["day"] == "Thu")):
                x = [float(r["net_usd"]) for r in rows if keep(r)]
                print(f"{lab:18s} {len(x):3d} trades  {sum(x):+9.2f} USD")
        if bot.wide_path.exists():
            with bot.wide_path.open() as f:
                w = [float(r["net_usd"]) for r in csv.DictReader(f)]
            print(f"{'wide strangle':18s} {len(w):3d} nights  {sum(w):+9.2f} USD (paper, real Delta bids/asks)")
        return
    if a.once:
        r = bot.readings()
        side, info, why = bot.decide(r)
        print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in info.items()}, default=str))
        print("Would:", ("SHORT" if side < 0 else "LONG") if side else "not trade", "-", why,
              f"| mode {cfg.MODE}, live day today: {bot.live_day()}")
        return
    bot.run()


if __name__ == "__main__":
    main()
