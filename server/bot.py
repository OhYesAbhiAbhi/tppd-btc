"""TPPD BTC options-writing bot for Delta Exchange India.

Sells next-day-expiry BTC options in the direction of the score D, either naked
or as a credit spread (HEDGE), and buys back at TP_NET_PCT profit after fees.
On sideways days it sells both sides: a short strangle, or an iron condor with HEDGE.
A winning trade that still looks strong is upgraded to a "runner" and held to
expiry, so it can expire worthless with no buy-back fee.

    python bot.py             # run forever (paper mode unless MODE=live)
    python bot.py --once      # one dry-run scan: shows what it would trade, never trades
    python bot.py --report    # summary of data/trades.csv per variant
"""
import argparse
import csv
import json
import logging
import math
import sys
import time
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import strategy as st
from config import Config
from delta_client import DeltaAPIError, DeltaClient

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("tppd")

TRADE_COLS = ["mode", "variant", "kind", "opened_ist", "closed_ist", "legs", "bias", "size",
              "D", "rsi5", "delta", "credit", "close_cost", "reason", "gross_usd",
              "fees_usd_est", "net_usd", "net_pct_credit"]


def setup_logging(data_dir):
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(Path(data_dir) / "bot.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]
    log.setLevel(logging.INFO)


def round_tick(price, tick, mode):
    t = Decimal(str(tick))
    q = (Decimal(str(price)) / t).to_integral_value(rounding=mode) * t
    return format(q.normalize(), "f")


def intrinsic(kind, strike, spot):
    return max(strike - spot, 0.0) if kind == "put" else max(spot - strike, 0.0)


class Bot:
    def __init__(self, client, cfg=Config, now_fn=time.time, sleep_fn=time.sleep):
        self.c = client
        self.cfg = cfg
        self.now = now_fn
        self.sleep = sleep_fn
        self.dir = Path(cfg.DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.trades_path = self.dir / "trades.csv"
        self.products = {}
        self.last_note = {}
        self.state = self._load_state()

    # ------------------------------------------------------------ utilities
    def ist(self):
        return datetime.fromtimestamp(self.now(), IST)

    def _new_book(self):
        return {"trades": 0, "losses": 0, "pnl": 0.0, "last_exit_ts": 0, "open": [], "aborts": 0}

    def _load_state(self):
        s = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if "books" not in s:        # fresh, or written by the first version
            s = {"day": s.get("day"), "next_id": 1, "books": {}}
        for v in self.cfg.variants():
            s["books"].setdefault(v, self._new_book())
        for b in s["books"].values():
            for p in b["open"]:
                p.setdefault("structure", "vertical")
                p.setdefault("name", ("credit spread" if len(p["legs"]) > 1 else "naked option"))
                p.setdefault("stops", [[p["stop_id"], p["legs"][0]["product_id"]]] if p.get("stop_id") else [])
                for l in p["legs"]:
                    l.setdefault("open_qty", p["size"])
                    l.setdefault("delta", p.get("delta", 0.0))
        return s

    def _save_state(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    def notify(self, msg):
        log.info(msg)
        if self.cfg.TELEGRAM_BOT_TOKEN and self.cfg.TELEGRAM_CHAT_ID:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": self.cfg.TELEGRAM_CHAT_ID, "text": f"[{self.cfg.MODE}] {msg}"},
                    timeout=5)
            except requests.RequestException:
                pass

    def note(self, key, msg, every_s=900):
        if self.now() - self.last_note.get(key, 0) >= every_s:
            self.last_note[key] = self.now()
            log.info(msg)

    def product(self, symbol, fresh=False):
        if fresh or symbol not in self.products:
            self.products[symbol] = self.c.product(symbol)
        return self.products[symbol]

    def books(self):
        return self.state["books"]

    def all_open(self):
        return [p for b in self.books().values() for p in b["open"]]

    def roll_day(self):
        today = self.ist().date().isoformat()
        if self.state["day"] != today:
            if self.state["day"]:
                for v, b in self.books().items():
                    log.info(f"[{v}] yesterday: {b['trades']} trades, {b['losses']} losses, "
                             f"P&L {b['pnl']:+.2f} USD")
            for b in self.books().values():
                b.update(trades=0, losses=0, pnl=0.0, aborts=0)
            self.state["day"] = today
            self._save_state()

    def in_trade_hours(self):
        start, end = self.cfg.TRADE_HOURS_IST.split("-")
        now = self.ist().strftime("%H:%M")
        return start <= now <= end if start <= end else (now >= start or now <= end)

    def expiry(self):

        """Nearest daily expiry (17:30 IST) with at least MIN_HOURS_TO_EXPIRY left."""

        now = self.ist()

        for days in range(0, 4):

            d = now.date() + timedelta(days=days)

            dt = datetime(d.year, d.month, d.day, self.cfg.EXPIRY_HOUR_IST, self.cfg.EXPIRY_MIN_IST,

                          tzinfo=IST)

            if (dt - now).total_seconds() >= self.cfg.MIN_HOURS_TO_EXPIRY * 3600:

                return dt, d.strftime("%d-%m-%Y"), d.strftime("%d%m%y")

        raise RuntimeError("no expiry found")



    def direction(self):
        end = int(self.now())
        c15 = self.c.candles(self.cfg.PERP_SYMBOL, "15m", end - 200 * 900, end)
        return st.direction_score(c15, (self.cfg.W_TREND, self.cfg.W_MOMENTUM, self.cfg.W_STRETCH))

    def rsi5(self):
        end = int(self.now())
        return st.last_rsi(self.c.candles(self.cfg.PERP_SYMBOL, "5m", end - 120 * 300, end))

    def spot(self):
        t = self.c.ticker(self.cfg.PERP_SYMBOL)
        return st._f(t.get("spot_price")) or st._f(t.get("mark_price"))

    # ----------------------------------------------------------------- loop
    def run(self):
        self.notify(f"Bot started ({self.cfg.MODE}, {self.cfg.DELTA_ENV}, "
                    f"books: {', '.join(self.cfg.variants())}, runner {'on' if self.cfg.RUNNER else 'off'}).")
        self.reconcile()
        while True:
            try:
                self.step()
            except DeltaAPIError as e:
                log.warning(f"API error: {e}")
            except Exception:
                log.exception("Unexpected error in step")
            self.sleep(self.cfg.MANAGE_SECONDS if self.all_open() else self.cfg.SCAN_SECONDS)

    def step(self):
        self.roll_day()
        for book in self.books().values():
            for pos in list(book["open"]):
                self.manage(book, pos)
        if self.now() - self.last_note.get("_scan", 0) >= self.cfg.SCAN_SECONDS - 1:
            self.last_note["_scan"] = self.now()
            self.scan()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def shorts(pos):
        return [l for l in pos["legs"] if l["side"] == "sell"]

    def held_symbols(self):
        return {p.get("product_symbol") for p in self.c.positions() if float(p.get("size", 0))}

    def reconcile(self):
        if self.cfg.MODE != "live":
            return
        held = self.held_symbols()
        tracked = set()
        for book in self.books().values():
            for pos in list(book["open"]):
                tracked.update(l["symbol"] for l in pos["legs"])
                if any(l["symbol"] not in held for l in self.shorts(pos)):
                    self.notify(f"[{pos['variant']}] {pos['name']}: a sold leg is not on the exchange "
                                "at start-up; closing the rest and recording it.")
                    self._settle_or_gone(book, pos, held)
        for sym in held - tracked:
            if sym and sym.startswith(("C-BTC", "P-BTC")):
                self.notify(f"Untracked BTC option position {sym} found. The bot won't touch it.")

    # ------------------------------------------------------------ entries
    def blocked_reason(self, book):
        cfg = self.cfg
        if book["trades"] >= cfg.MAX_TRADES_PER_DAY:
            return f"daily trade limit reached ({book['trades']})"
        if book.get("aborts", 0) >= cfg.MAX_ABORTS_PER_DAY:
            return f"{book['aborts']} entries aborted after partial fills today; paused until tomorrow"
        if book["losses"] >= cfg.MAX_LOSSES_PER_DAY:
            return f"daily loss-count limit reached ({book['losses']})"
        if book["pnl"] <= -cfg.MAX_DAILY_LOSS_USD:
            return f"daily loss limit reached ({book['pnl']:+.2f} USD)"
        if self.now() - book["last_exit_ts"] < cfg.COOLDOWN_MIN * 60:
            return "cooling down after last exit"
        if len(book["open"]) >= cfg.MAX_OPEN_PER_BOOK:
            return "max open positions"
        if not self.in_trade_hours():
            return "outside trading hours"
        return None

    def scan(self, dry=False):
        cfg = self.cfg
        ready = {v: b for v, b in self.books().items() if dry or not self.blocked_reason(b)}
        if not ready:
            for v, b in self.books().items():
                self.note(f"blocked-{v}", f"[{v}] no new trades: {self.blocked_reason(b)}")
            return

        d = self.direction()
        bias = st.bias_from_D(d.D, cfg.D_THRESHOLD)
        if not bias:
            if not cfg.STRANGLE:
                self.note("flat", f"D={d.D:+.2f} (T={d.T:+.2f} M={d.M:+.2f} V={d.V:+.2f}) "
                                  f"BTC {d.price:,.0f}: sideways, no trade")
                return
            bias = "neutral"
        r5 = self.rsi5()
        lo, hi = ((cfg.STRANGLE_RSI_LOW, cfg.STRANGLE_RSI_HIGH) if bias == "neutral"
                  else (cfg.RSI_LOW, cfg.RSI_HIGH))
        if not st.entry_triggered(bias, r5, lo, hi):
            want = {"bull": f"<= {lo:.0f}", "bear": f">= {hi:.0f}",
                    "neutral": f"between {lo:.0f} and {hi:.0f}"}[bias]
            self.note("wait", f"D={d.D:+.2f} -> {bias}; waiting for 5m RSI {want} (now {r5:.1f})",
                      every_s=300)
            if not dry:
                return

        exp_dt, exp_api, exp_sfx = self.expiry()
        hours_left = (exp_dt - self.ist()).total_seconds() / 3600
        hold_h = min(cfg.MAX_HOLD_MIN / 60, hours_left - cfg.EXIT_BEFORE_EXPIRY_MIN / 60)
        if hold_h <= 0.25:
            self.note("expiry", "Too close to target expiry to open a trade")
            return

        chain = st.parse_chain(self.c.option_chain(cfg.UNDERLYING, exp_api), exp_sfx)
        if bias == "neutral":
            shorts, why = st.select_strangle(chain, cfg)
            structure = "strangle"
        else:
            main, why = st.select_main(chain, bias, cfg)
            shorts, structure = ([main] if main else None), "vertical"
        if not shorts:
            self.note("noshort", f"D={d.D:+.2f} {bias}: skip ({why})", every_s=300)
            return

        for variant, book in ready.items():
            open_syms = {l["symbol"] for p in book["open"] for l in p["legs"]}
            if any(c.symbol in open_syms for c in shorts):
                continue
            wings = []
            if variant == "spread":
                for s in shorts:
                    w, why = st.select_wing(chain, s, cfg)
                    if not w:
                        break
                    wings.append(w)
                if len(wings) != len(shorts):
                    self.note("nowing", f"[spread] skip ({why})", every_s=300)
                    continue
            plan = st.build_legs_plan(variant, structure, shorts, wings, d.price, cfg)
            ok, why = st.gate_plan(plan, cfg, hold_h)
            if dry:
                log.info(f"DRY RUN [{variant}] {plan.name}: {plan.describe()}: credit {plan.credit:.1f}, "
                         f"needs {plan.tp_gross:.1%} decay for {cfg.TP_NET_PCT:.0%} net -> "
                         f"{'WOULD TRADE' if ok else 'skip: ' + why}")
                continue
            if not ok:
                self.note(f"gate-{variant}", f"[{variant}] {plan.name} D={d.D:+.2f} RSI5={r5:.1f} "
                          f"{plan.describe()}: skip ({why})", every_s=300)
                continue
            size = self.size_for(plan)
            per_lot = self.lot_risk(plan)
            budget = cfg.MAX_OPEN_RISK_USD - self.open_risk(book)
            if per_lot:
                size = min(size, int(max(budget, 0) // per_lot))
            elif book["open"]:
                size = 0        # a naked trade has no defined worst case: only when nothing else is open
            if size < 1 and budget < cfg.MAX_OPEN_RISK_USD:
                self.note(f"budget-{variant}", f"[{variant}] {plan.name}: skip (open-risk budget used: "
                          f"{self.open_risk(book):.0f} of {cfg.MAX_OPEN_RISK_USD:.0f} USD)", every_s=300)
                continue
            if size >= 1 and cfg.MODE == "live":
                ok, why = self.margin_ok()
                if not ok:
                    self.note(f"margin-{variant}", f"[{variant}] {plan.name}: skip ({why})", every_s=300)
                    continue
            if size < 1:
                self.note(f"size-{variant}", f"[{variant}] {plan.name}: skip (worst case per lot "
                          f"exceeds MAX_LOSS_PER_TRADE_USD={cfg.MAX_LOSS_PER_TRADE_USD:.0f})", every_s=300)
                continue
            self.enter(book, plan, d, r5, bias, exp_dt, size)

    def lot_risk(self, plan):
        """Worst-case USD per lot (full width minus credit, plus fees); None for naked trades."""
        if not plan.wings:
            return None
        cv = float(self.product(plan.shorts[0].symbol).get("contract_value", 0.001))
        return (plan.max_loss + 2 * plan.fees_in) * cv

    def open_risk(self, book):
        """Worst-case USD loss of every open position in this book."""
        total = 0.0
        for p in book["open"]:
            r = p.get("max_loss_usd")
            if r is None:
                wings = [l for l in p["legs"] if l["side"] == "buy"]
                if wings:
                    width = max(abs(s["strike"] - w["strike"]) for s, w in zip(self.shorts(p), wings))
                    r = (width - p["credit"] + 2 * p["fees_in"]) * p["legs"][0]["cv"] * p["size"]
                else:
                    r = self.cfg.MAX_OPEN_RISK_USD
            total += max(r, 0.0)
        return total

    def margin_ok(self):
        """Cross margin: don't add positions once more than MAX_MARGIN_USE_PCT of the USD balance is in use."""
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
            return False, f"margin in use {used:.0%} > {self.cfg.MAX_MARGIN_USE_PCT:.0%} of balance"
        return True, f"margin in use {used:.0%}"

    def size_for(self, plan):
        """Lots to trade: ORDER_SIZE, cut down so a full-width loss (plus fees) stays within
        MAX_LOSS_PER_TRADE_USD. Naked trades have no defined worst case and use ORDER_SIZE."""
        cfg = self.cfg
        if not plan.wings:
            return cfg.ORDER_SIZE
        cv = float(self.product(plan.shorts[0].symbol).get("contract_value", 0.001))
        per_lot = (plan.max_loss + 2 * plan.fees_in) * cv
        return min(cfg.ORDER_SIZE, int(cfg.MAX_LOSS_PER_TRADE_USD // per_lot)) if per_lot > 0 else 0

    def _leg(self, c, side, px, size):
        prod = self.product(c.symbol)
        return {"symbol": c.symbol, "product_id": c.product_id, "side": side, "kind": c.kind,
                "strike": c.strike, "entry": px, "delta": c.delta, "open_qty": size,
                "cv": float(prod.get("contract_value", 0.001)),
                "tick": float(prod.get("tick_size", 0.1))}

    def enter(self, book, plan, d, r5, bias, exp_dt, size=None):
        cfg = self.cfg
        size = size or cfg.ORDER_SIZE
        stops = []

        if cfg.MODE == "paper":
            legs = [self._leg(c, "sell", c.bid, size) for c in plan.shorts] + \
                   [self._leg(c, "buy", c.ask, size) for c in plan.wings]
        else:
            # Wings first (so a sold leg is never left unhedged), then the sold legs. A leg that
            # fills only partly shrinks the trade; a leg that fills nothing aborts it.
            done, want, short_of = [], size, None
            all_legs = [(w, "buy") for w in plan.wings] + [(s, "sell") for s in plan.shorts]
            for c, side in all_legs:
                px, got = self._fill(c, side, want, c.ask if side == "buy" else c.bid)
                if got > 0:
                    done.append(self._leg(c, side, px, got))
                if got < want:
                    short_of = (c.symbol, got, want)
                    want = got
                if want <= 0:
                    break
            final = want if len(done) == len(all_legs) else 0
            if final <= 0:
                if done:
                    book["aborts"] = book.get("aborts", 0) + 1
                    self._save_state()
                for l in sorted(done, key=lambda l: l["side"] != "sell"):    # sold legs first
                    self._close_leg(l, l["open_qty"])
                sym, got, wanted = short_of or ("?", 0, size)
                msg = (f"[{plan.variant}] {plan.name} entry aborted: {sym} filled "
                       f"{got}/{wanted}. Anything that filled was closed again.")
                if self.now() - self.last_note.get("abort-alert", 0) >= 1800:
                    self.last_note["abort-alert"] = self.now()
                    self.notify(msg + " (further aborts in the next 30 min go to the log only)")
                else:
                    log.info(msg)
                return
            for l in sorted(done, key=lambda l: l["side"] != "sell"):        # trim extras to `final`
                extra = l["open_qty"] - final
                if extra > 0:
                    self._close_leg(l, extra)
                    l["open_qty"] = final
            if final < size:
                log.info(f"[{plan.variant}] {plan.name}: partial fill, trading {final}/{size} lots "
                         f"({short_of[0]} filled {short_of[1]})")
            size = final
            legs = [l for l in done if l["side"] == "sell"] + [l for l in done if l["side"] == "buy"]
            if not plan.wings:      # naked: exchange-side stops in case the bot dies
                pct = cfg.STRANGLE_BACKSTOP_PCT if plan.structure == "strangle" else cfg.BACKSTOP_PCT
                for l in legs:
                    stop_px = round_tick(l["entry"] * (1 + pct), l["tick"], ROUND_CEILING)
                    try:
                        so = self.c.place_order(product_id=l["product_id"], size=size, side="buy",
                                                order_type="market_order",
                                                stop_order_type="stop_loss_order",
                                                stop_price=stop_px, stop_trigger_method="mark_price",
                                                reduce_only=True,
                                                client_order_id=f"tppd-bs-{int(self.now() * 1000)}")
                        stops.append([so.get("id"), l["product_id"]])
                    except DeltaAPIError as e:
                        self.notify(f"WARNING: backstop stop on {l['symbol']} not placed ({e}); "
                                    "bot stop still active")

        credit = sum(l["entry"] for l in legs if l["side"] == "sell") - \
            sum(l["entry"] for l in legs if l["side"] == "buy")
        fees_in = sum(st.fee_abs(l["entry"], d.price, cfg) for l in legs)
        tp_gross = cfg.TP_NET_PCT + 2 * fees_in / credit
        net_delta = sum(l["delta"] * (-1 if l["side"] == "sell" else 1) for l in legs)
        pos = {
            "id": self.state["next_id"], "variant": plan.variant, "structure": plan.structure,
            "name": plan.name, "bias": bias, "mode": "scalp",
            "legs": legs, "size": size, "credit": credit, "fees_in": fees_in,
            "tp_cost": credit * (1 - tp_gross), "sl_cost": credit * cfg.SL_MULT,
            "breakeven_cost": credit - 2 * fees_in, "low_cost": None,
            "opened_ts": self.now(), "expiry_ts": exp_dt.timestamp(), "stops": stops,
            "D": round(d.D, 3), "rsi5": round(r5, 1), "spot": d.price, "delta": round(net_delta, 3),
            "max_loss_usd": (round(self.lot_risk(plan) * size, 2) if plan.wings else None),
            "last_d_check": self.now(), "settle_note": False,
        }
        self.state["next_id"] += 1
        book["open"].append(pos)
        book["trades"] += 1
        self._save_state()
        legtxt = ", ".join(f"{'SOLD' if l['side'] == 'sell' else 'BOUGHT'} {l['symbol']} @ {l['entry']:.1f}"
                           for l in legs)
        risk = f"max loss {plan.max_loss:.0f}/BTC" if plan.wings else "unhedged"
        self.notify(f"[{plan.variant}] {plan.name.upper()}: {legtxt} x{size}. Credit {credit:.1f} "
                    f"(D={d.D:+.2f}, RSI5={r5:.0f}). TP at cost {pos['tp_cost']:.1f}, "
                    f"SL {pos['sl_cost']:.1f}, {risk}")

    def _fill(self, cand, side, size, px):
        """Work a resting limit order: start at the mid-price and step every ENTRY_STEP_SECONDS
        toward, then up to ENTRY_SLIPPAGE_PCT past, the far side of the book, for up to
        ENTRY_WORK_SECONDS. Returns (average price, contracts filled)."""
        cfg = self.cfg
        tick = float(self.product(cand.symbol).get("tick_size", 0.1))
        steps = max(int(cfg.ENTRY_WORK_SECONDS // max(cfg.ENTRY_STEP_SECONDS, 1)), 1)
        remaining, fills = size, []
        for k in range(steps + 1):
            q = self.c.ticker(cand.symbol).get("quotes") or {}
            bid, ask = st._f(q.get("best_bid")), st._f(q.get("best_ask"))
            if side == "buy":
                far = ask or px
                start = (bid + far) / 2 if 0 < bid < far else far
                raw = start + (far * (1 + cfg.ENTRY_SLIPPAGE_PCT) - start) * k / steps
                lp = round_tick(raw, tick, ROUND_CEILING)
            else:
                far = bid or px
                start = (far + ask) / 2 if ask > far > 0 else far
                raw = start + (max(far * (1 - cfg.ENTRY_SLIPPAGE_PCT), tick) - start) * k / steps
                lp = round_tick(max(raw, tick), tick, ROUND_FLOOR)
            o = self.c.place_order(product_id=cand.product_id, size=remaining, side=side,
                                   order_type="limit_order", limit_price=lp, time_in_force="gtc",
                                   client_order_id=f"tppd-{side[0]}-{int(self.now() * 1000)}-{k}")
            got, avg = self._settle_order(o, cand, remaining, lp)
            if got > 0:
                fills.append((got, avg))
                remaining -= got
            log.info(f"{side} {cand.symbol}: step {k + 1}/{steps + 1} at {lp} "
                     f"(bid {bid} / ask {ask}) filled {size - remaining}/{size}")
            if remaining <= 0:
                break
        filled = size - remaining
        avg = sum(n * p for n, p in fills) / filled if filled else px
        return avg, filled

    def _settle_order(self, o, cand, qty, lp):
        """Give a resting order ENTRY_STEP_SECONDS, cancel what's left, read the final fill."""
        filled = int(o.get("size", 0)) - int(o.get("unfilled_size", 0))
        if filled >= qty or not o.get("id"):
            return filled, float(o.get("average_fill_price") or lp)
        self.sleep(self.cfg.ENTRY_STEP_SECONDS)
        try:
            self.c.cancel_order(o["id"], cand.product_id)
        except DeltaAPIError:
            pass                      # usually means it filled completely in the meantime
        try:
            final = self.c.get_order(o["id"]) or o
        except DeltaAPIError as e:
            log.warning(f"could not read order {o['id']} after cancel ({e}); assuming its last state")
            final = o
        filled = int(final.get("size", 0)) - int(final.get("unfilled_size", 0))
        return filled, float(final.get("average_fill_price") or lp)

    # -------------------------------------------------------------- quotes
    def quote_position(self, pos):
        """(cost to close at ask/bid, cost to close at mark, {symbol: ticker})."""
        cost_ask = cost_mark = 0.0
        tickers = {}
        for l in pos["legs"]:
            t = self.c.ticker(l["symbol"])
            tickers[l["symbol"]] = t
            q = t.get("quotes") or {}
            bid, ask, mark = st._f(q.get("best_bid")), st._f(q.get("best_ask")), st._f(t.get("mark_price"))
            if l["side"] == "sell":
                cost_ask += ask if ask > 0 else mark * 1.05
                cost_mark += mark
            else:
                cost_ask -= bid
                cost_mark -= mark
        return cost_ask, cost_mark, tickers

    def _sigma(self, d, pos, tickers):
        ivs = [st.parse_iv((tickers.get(l["symbol"]) or {}).get("quotes") or {}) for l in self.shorts(pos)]
        return max(ivs + [d.rvol, 0.2])

    def _odds(self, pos, d, tickers):
        """(z, P) that every sold leg expires worthless."""
        hours_left = (pos["expiry_ts"] - self.now()) / 3600
        sig = self._sigma(d, pos, tickers)
        sh = self.shorts(pos)
        if pos.get("structure") == "strangle":
            put = next(l for l in sh if l["kind"] == "put")
            call = next(l for l in sh if l["kind"] == "call")
            return st.prob_range(d.price, put["strike"], call["strike"], sig, hours_left)
        return st.prob_otm(sh[0]["kind"], d.price, sh[0]["strike"], sig, hours_left)

    def _trend_against(self, pos, D, flip):
        if pos["bias"] == "neutral":
            return abs(D) >= self.cfg.STRANGLE_EXIT_D
        return (pos["bias"] == "bull" and D <= -flip) or (pos["bias"] == "bear" and D >= flip)

    # -------------------------------------------------------------- manage
    def manage(self, book, pos):
        cfg, now = self.cfg, self.now()

        if cfg.MODE == "live":
            held = self.held_symbols()
            if any(l["symbol"] not in held for l in self.shorts(pos)):
                self._settle_or_gone(book, pos, held)
                return
        elif now >= pos["expiry_ts"]:
            self._settle_or_gone(book, pos)
            return

        cost_ask, cost_mark, tickers = self.quote_position(pos)
        if pos["mode"] == "runner":
            return self._manage_runner(book, pos, cost_ask, cost_mark, tickers)

        reason = None
        if cost_ask <= pos["tp_cost"]:
            if cfg.RUNNER and self._try_upgrade(pos, cost_mark, tickers):
                return
            reason = "take_profit"
        elif cost_mark >= pos["sl_cost"] or cost_ask >= pos["sl_cost"] * 1.25:
            reason = "stop_loss"
        elif (guard := self._spot_guard(pos, tickers)):
            reason = guard
        elif now - pos["opened_ts"] >= cfg.MAX_HOLD_MIN * 60:
            reason = "time_stop"
        elif pos["expiry_ts"] - now <= cfg.EXIT_BEFORE_EXPIRY_MIN * 60:
            reason = "expiry_guard"
        elif cfg.EXIT_ON_D_FLIP and now - pos["last_d_check"] >= 300:
            pos["last_d_check"] = now
            d = self.direction()
            if self._trend_against(pos, d.D, 0.0):
                reason = ("trend_started" if pos["bias"] == "neutral" else "direction_flip") + \
                         f" (D={d.D:+.2f})"
            self._save_state()

        if reason:
            self.exit(book, pos, reason)
        else:
            self.note(f"hold-{pos['id']}", f"[{pos['variant']}] holding {pos['name']}: credit "
                      f"{pos['credit']:.1f}, cost {cost_ask:.1f}, TP {pos['tp_cost']:.1f}, "
                      f"SL {pos['sl_cost']:.1f}", every_s=300)

    def _spot_guard(self, pos, tickers):
        """Exit when BTC itself has moved so close to a sold strike that P(it expires worthless)
        drops below STOP_POTM. Judged on BTC's price, so option-quote noise can't trigger it."""
        spot = self.spot()
        if spot <= 0:
            return None
        hours = max((pos["expiry_ts"] - self.now()) / 3600, 0.01)
        for l in self.shorts(pos):
            iv = st.parse_iv((tickers.get(l["symbol"]) or {}).get("quotes") or {})
            _, p = st.prob_otm(l["kind"], spot, l["strike"], max(iv, 0.2), hours)
            if p < self.cfg.STOP_POTM:
                return f"spot_guard (BTC {spot:,.0f} near {l['kind']} {l['strike']:.0f}: P worthless {p:.0%})"
        return None

    def _try_upgrade(self, pos, cost_mark, tickers):
        d = self.direction()
        hours_left = (pos["expiry_ts"] - self.now()) / 3600
        deltas = {l["symbol"]: st._f(((tickers.get(l["symbol"]) or {}).get("greeks") or {}).get("delta"))
                  for l in self.shorts(pos)}
        sig = self._sigma(d, pos, tickers)
        if pos.get("structure") == "strangle":
            sh = self.shorts(pos)
            put = next(l for l in sh if l["kind"] == "put")
            call = next(l for l in sh if l["kind"] == "call")
            ok, z, p, why = st.runner_check_strangle(d.D, d.price, put["strike"], call["strike"], sig,
                                                     hours_left, max(abs(v) for v in deltas.values()),
                                                     self.cfg)
        else:
            main = self.shorts(pos)[0]
            ok, z, p, why = st.runner_check(pos["bias"], d.D, main["kind"], d.price, main["strike"],
                                            sig, hours_left, deltas[main["symbol"]], self.cfg)
        if not ok:
            log.info(f"[{pos['variant']}] 3% reached on {pos['name']}; not a runner ({why}). Taking profit.")
            return False
        pos["mode"] = "runner"
        pos["low_cost"] = cost_mark
        pos["last_d_check"] = self.now()
        self._save_state()
        self.notify(f"[{pos['variant']}] RUNNER: holding {pos['name']} towards expiry. "
                    f"D={d.D:+.2f}, P(expires worthless)={p:.0%}, z={z:.1f}. "
                    f"Worst case now: exit near breakeven.")
        return True

    def _manage_runner(self, book, pos, cost_ask, cost_mark, tickers):
        cfg, now = self.cfg, self.now()
        pos["low_cost"] = min(pos["low_cost"], cost_mark)
        floor = min(pos["breakeven_cost"], max(pos["low_cost"], 0.0) * (1 + cfg.RUNNER_TRAIL))
        floor = max(floor, pos["legs"][0]["tick"] * 4)   # don't get shaken out by a few ticks near zero

        reason = None
        if cost_mark >= floor:
            reason = f"runner_trail (cost {cost_mark:.1f} >= floor {floor:.1f})"
        elif now - pos["last_d_check"] >= 300 or pos["expiry_ts"] - now <= cfg.EXIT_BEFORE_EXPIRY_MIN * 60:
            pos["last_d_check"] = now
            d = self.direction()
            z, p = self._odds(pos, d, tickers)
            if z < cfg.RUNNER_EXIT_Z:
                reason = f"runner_spot_guard (z={z:.2f})"
            elif self._trend_against(pos, d.D, cfg.RUNNER_FLIP_D):
                reason = f"runner_direction_flip (D={d.D:+.2f})"
            elif pos["expiry_ts"] - now <= cfg.EXIT_BEFORE_EXPIRY_MIN * 60:
                if p < cfg.RUNNER_SETTLE_POTM:
                    reason = f"runner_expiry_guard (P worthless {p:.0%})"
                elif not pos["settle_note"]:
                    pos["settle_note"] = True
                    self.notify(f"[{pos['variant']}] {pos['name']} held into settlement "
                                f"(P worthless {p:.1%}).")
            self._save_state()

        if reason:
            self.exit(book, pos, reason)
        else:
            self.note(f"hold-{pos['id']}", f"[{pos['variant']}] runner {pos['name']}: credit "
                      f"{pos['credit']:.1f}, cost {cost_mark:.1f}, low {pos['low_cost']:.1f}, "
                      f"floor {floor:.1f}", every_s=600)

    # --------------------------------------------------------------- exits
    def _close_leg(self, leg, size):
        """Close one leg with IOC limits stepping 1% per try, then a market order. Returns (avg, filled)."""
        side = "buy" if leg["side"] == "sell" else "sell"
        remaining, fills = size, []
        for attempt in range(4):
            t = self.c.ticker(leg["symbol"])
            q = t.get("quotes") or {}
            mark = st._f(t.get("mark_price"))
            if attempt < 3:
                if side == "buy":
                    ref = st._f(q.get("best_ask")) or mark * 1.02
                    px = round_tick(ref * (1 + 0.01 * attempt), leg["tick"], ROUND_CEILING)
                else:
                    ref = st._f(q.get("best_bid"))
                    if ref <= 0:
                        return 0.0, 0          # no bid for a bought wing: let it expire
                    px = round_tick(ref * (1 - 0.01 * attempt), leg["tick"], ROUND_FLOOR)
                o = self.c.place_order(product_id=leg["product_id"], size=remaining, side=side,
                                       order_type="limit_order", limit_price=px,
                                       time_in_force="ioc", reduce_only=True,
                                       client_order_id=f"tppd-x-{int(self.now() * 1000)}-{attempt}")
            else:
                o = self.c.place_order(product_id=leg["product_id"], size=remaining, side=side,
                                       order_type="market_order", reduce_only=True,
                                       client_order_id=f"tppd-x-{int(self.now() * 1000)}-mkt")
                px = mark
            got = int(o.get("size", 0)) - int(o.get("unfilled_size", 0))
            if got > 0:
                fills.append((got, float(o.get("average_fill_price") or px)))
                remaining -= got
            if remaining <= 0:
                break
            self.sleep(1)
        filled = size - remaining
        avg = sum(n * p for n, p in fills) / filled if filled else 0.0
        return avg, filled

    def _cancel_stops(self, pos):
        for sid, pid in pos.get("stops") or []:
            try:
                self.c.cancel_order(sid, pid)
            except DeltaAPIError as e:
                log.warning(f"Could not cancel backstop stop {sid}: {e}")

    def exit(self, book, pos, reason):
        if self.cfg.MODE == "paper":
            exit_px = {}
            for l in pos["legs"]:
                t = self.c.ticker(l["symbol"])
                q = t.get("quotes") or {}
                exit_px[l["symbol"]] = (st._f(q.get("best_ask")) or st._f(t.get("mark_price")) * 1.02) \
                    if l["side"] == "sell" else st._f(q.get("best_bid"))
            return self._record(book, pos, exit_px, reason)

        # Sold legs first, then wings. A leg that won't fully close stops the exit; the next
        # cycle retries only what is still open.
        for l in sorted(pos["legs"], key=lambda l: l["side"] != "sell"):
            if l.get("open_qty", pos["size"]) <= 0:
                continue
            qty = l.get("open_qty", pos["size"])
            avg, got = self._close_leg(l, qty)
            if got > 0:
                prev = pos["size"] - qty
                l["exit_px"] = ((l.get("exit_px") or 0) * prev + avg * got) / (prev + got)
            l["open_qty"] = qty - got
            if l["side"] == "sell" and l["open_qty"] > 0:
                self._save_state()
                self.notify(f"[{pos['variant']}] EXIT INCOMPLETE on {l['symbol']}: {l['open_qty']} "
                            "still open; retrying next cycle. Check the exchange.")
                return
            if got == 0 and l["side"] == "buy":
                l["exit_px"] = 0.0         # no bid for the wing: it expires on its own
        self._cancel_stops(pos)
        self._record(book, pos, {l["symbol"]: l.get("exit_px", 0.0) for l in pos["legs"]}, reason)

    def _settled_value(self, leg, spot):
        """Option value at settlement. Uses the product's settlement_price when available
        (treated as the BTC index if it's large, else as the option's value), else intrinsic at spot."""
        s = 0.0
        if self.cfg.MODE == "live":
            try:
                s = st._f(self.product(leg["symbol"], fresh=True).get("settlement_price"))
            except DeltaAPIError:
                s = 0.0
        if s > 0.3 * leg["strike"]:
            return intrinsic(leg["kind"], leg["strike"], s)
        if s > 0:
            return s
        return intrinsic(leg["kind"], leg["strike"], spot)

    def _settle_or_gone(self, book, pos, held=None):
        """A sold leg no longer exists: settled at expiry, or closed by a backstop / by hand."""
        if self.now() >= pos["expiry_ts"]:
            spot = self.spot()
            px = {l["symbol"]: self._settled_value(l, spot) for l in pos["legs"]}
            self._cancel_stops(pos)
            return self._record(book, pos, px, "settled_at_expiry")

        held = held if held is not None else set()
        gone = [l["symbol"] for l in pos["legs"] if l["symbol"] not in held]
        self.notify(f"[{pos['variant']}] {pos['name']}: {', '.join(gone)} gone before expiry "
                    "(backstop stop or manual close). Closing the remaining legs.")
        px = {}
        for l in sorted(pos["legs"], key=lambda l: l["side"] != "sell"):
            if self.cfg.MODE == "live" and l["symbol"] in held:
                avg, got = self._close_leg(l, l.get("open_qty", pos["size"]))
                px[l["symbol"]] = avg if got else st._f(self.c.ticker(l["symbol"]).get("mark_price"))
            else:
                px[l["symbol"]] = st._f(self.c.ticker(l["symbol"]).get("mark_price"))
        self._cancel_stops(pos)
        self._record(book, pos, px, "closed_on_exchange")

    def _record(self, book, pos, exit_px, reason):
        cfg = self.cfg
        units = pos["size"] * pos["legs"][0]["cv"]
        close_cost = sum(exit_px[l["symbol"]] * (1 if l["side"] == "sell" else -1) for l in pos["legs"])
        fees_out = sum(st.fee_abs(exit_px[l["symbol"]], pos["spot"], cfg) for l in pos["legs"])
        gross = (pos["credit"] - close_cost) * units
        fees = (pos["fees_in"] + fees_out) * units
        net = gross - fees
        net_pct = net / (pos["credit"] * units)

        book["pnl"] += net
        if net < 0:
            book["losses"] += 1
        book["open"] = [p for p in book["open"] if p["id"] != pos["id"]]
        book["last_exit_ts"] = self.now()
        self._save_state()

        new = not self.trades_path.exists()
        fmt = lambda ts: datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M")
        with self.trades_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(TRADE_COLS)
            w.writerow([cfg.MODE, pos["variant"], pos["mode"], fmt(pos["opened_ts"]), fmt(self.now()),
                        " / ".join(f"{l['side']} {l['symbol']}" for l in pos["legs"]), pos["bias"],
                        pos["size"], pos["D"], pos["rsi5"], pos["delta"], round(pos["credit"], 2),
                        round(close_cost, 2), reason, round(gross, 4), round(fees, 4),
                        round(net, 4), round(net_pct, 4)])
        self.notify(f"[{pos['variant']}] CLOSED {pos['name']} ({pos['mode']}, {reason}): "
                    f"credit {pos['credit']:.1f} -> cost {close_cost:.1f}, net {net:+.3f} USD "
                    f"({net_pct:+.1%}). Today: {book['trades']} trades, P&L {book['pnl']:+.2f} USD")


# --------------------------------------------------------------- reporting
def report(path):
    p = Path(path)
    if not p.exists():
        print("No trades yet.")
        return
    with p.open() as f:
        rows = list(csv.DictReader(f))
    groups = {}
    for r in rows:
        trade = "sideways" if r["bias"] == "neutral" else "trend"
        groups.setdefault((r["variant"], "all"), []).append(r)
        groups.setdefault((r["variant"], trade), []).append(r)
    print(f"{'book':8} {'trades':9} {'n':>4} {'win%':>6} {'avg net%':>9} {'total USD':>10} "
          f"{'runners':>8} {'runner avg%':>12} {'worst%':>8}")
    for (v, kind), rs in sorted(groups.items()):
        nets = [float(r["net_pct_credit"]) for r in rs]
        usd = sum(float(r["net_usd"]) for r in rs)
        run = [float(r["net_pct_credit"]) for r in rs if r["kind"] == "runner"]
        print(f"{v:8} {kind:9} {len(rs):>4} {100 * sum(n > 0 for n in nets) / len(nets):>5.0f}% "
              f"{100 * sum(nets) / len(nets):>8.1f}% {usd:>10.2f} {len(run):>8} "
              f"{(100 * sum(run) / len(run)) if run else 0:>11.1f}% {100 * min(nets):>7.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one dry-run scan (never trades), then exit")
    ap.add_argument("--report", action="store_true", help="summarise data/trades.csv per book")
    args = ap.parse_args()

    if args.report:
        report(Path(Config.DATA_DIR) / "trades.csv")
        return
    Config.validate()
    setup_logging(Config.DATA_DIR)
    bot = Bot(DeltaClient(Config.API_KEY, Config.API_SECRET, Config.DELTA_ENV))
    if args.once:
        bot.roll_day()
        d = bot.direction()
        log.info(f"D={d.D:+.3f} T={d.T:+.2f} M={d.M:+.2f} V={d.V:+.2f} BTC={d.price:,.1f} "
                 f"RSI5={bot.rsi5():.1f} realised vol {d.rvol:.0%}")
        for p in bot.all_open():
            log.info(f"Open [{p['variant']}] {p['legs'][0]['symbol']} ({p['mode']})")
        bot.scan(dry=True)
        return
    bot.run()


if __name__ == "__main__":
    main()
