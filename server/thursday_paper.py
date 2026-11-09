"""PAPER-ONLY Thursday option-buying tracker. Never places orders; needs no API key.

Every Thursday in the entry window (CONDOR_ENTRY_IST, default 21:30 IST, +60 min) it "buys" three
positions for Friday's 17:30 expiry at Delta's real ask prices, THU_LOTS lots each (default 100):
  A  straddle_hold   ATM put + ATM call (same strike nearest BTC), held to settlement
  B  strangle30_hold ~0.30-delta put + ~0.30-delta call, held to settlement
  C  straddle_2x     ATM straddle, sold at Delta's bids once both legs together are worth 2x what they
                     cost (fees included), otherwise held to settlement
Fees as the live bot (0.01% notional capped at 3.5% of premium, +18% GST). Results:
~/btc_bot/data_thursday/thursday.csv, Telegram tag [thursday-paper]. Based on
btc-thursday-long-options-2026-10-31 (backtest: hold versions made money in both halves but depend on a few
big nights; 2x exit was weak). Run alongside CONDOR_SKIP_DAYS=Thu on the live bot; pick one later.

    python thursday_paper.py            # run forever (service tppd-thursday)
    python thursday_paper.py --report   # paper results by strategy
    python thursday_paper.py --status   # what it is doing now
    python thursday_paper.py --once     # what it would buy right now (records nothing)
"""
import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

import requests

import condor as cd
import strategy as st
from condor import IST, Condor, CondorConfig, DeltaAPIError, DeltaClient, intrinsic, log, setup_logging


def _env(name, default, cast=str):
    return cd._env(name, default, cast)


class ThursdayConfig(CondorConfig):
    MODE = "paper"                                    # never live, whatever .env says
    API_KEY = ""
    API_SECRET = ""
    DATA_DIR = _env("THU_DATA_DIR", str(Path(__file__).with_name("data_thursday")))
    LOTS = _env("THU_LOTS", 100, int)
    DAYS = _env("THU_DAYS", "Thu")                    # entry days (IST) to paper-trade
    TP_MULT = _env("THU_TP_MULT", 2.0, float)         # strategy C: sell at this multiple of cost
    WING_DELTA_TARGET = _env("THU_DELTA", 0.30, float)
    DELTA_TOL = _env("THU_DELTA_TOL", 0.08, float)
    MAX_SPREAD_PCT = _env("THU_MAX_SPREAD_PCT", 0.25, float)
    CHECK_SECONDS = _env("THU_CHECK_SECONDS", 30, int)


STRATS = {"straddle_hold": "A: ATM straddle, hold to 17:30",
          "strangle30_hold": "B: 0.30-delta strangle, hold to 17:30",
          "straddle_2x": "C: ATM straddle, sell at 2x cost"}
COLS = ["strategy", "entry_ist", "expiry", "lots", "put", "call", "spot_entry", "cost", "fees_in",
        "exit_ist", "exit_value", "settle_price", "fees_out", "net_usd", "net_pct_cost", "note"]


class ThursdayPaper(Condor):
    def __init__(self, client, cfg=ThursdayConfig, **kw):
        super().__init__(client, cfg, **kw)
        self.state.setdefault("paper", {})
        self.state.setdefault("last_thu", None)

    def notify(self, msg):
        log.info(msg)
        if self.cfg.TELEGRAM_BOT_TOKEN and self.cfg.TELEGRAM_CHAT_ID:
            try:
                requests.post(f"https://api.telegram.org/bot{self.cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
                              json={"chat_id": self.cfg.TELEGRAM_CHAT_ID, "text": f"[thursday-paper] {msg}"},
                              timeout=5)
            except requests.RequestException:
                pass

    def is_day(self):
        days = [d.strip()[:3].lower() for d in self.cfg.DAYS.split(",") if d.strip()]
        return self.ist().strftime("%a").lower() in days

    # ------------------------------------------------------------ selection
    def pick(self):
        """(dict strategy -> {put, call} option objects, expiry, spot) or (None, reason)."""
        cfg = self.cfg
        exp = self.next_expiry()
        chain = st.parse_chain(self.c.option_chain("BTC", exp.strftime("%d-%m-%Y")), exp.strftime("%d%m%y"))
        if not chain:
            return None, f"no options listed for {exp:%d %b} 17:30 expiry"
        spot = self.spot()
        ok = [c for c in chain if c.bid > 0 and c.ask > 0 and c.spread_pct <= cfg.MAX_SPREAD_PCT]
        strikes = sorted({c.strike for c in ok if c.kind == "put"} & {c.strike for c in ok if c.kind == "call"},
                         key=lambda k: abs(k - spot))
        if not strikes:
            return None, "no ATM strike with both a put and a call quoted"
        K = strikes[0]
        atm = {k: next(c for c in ok if c.kind == k and c.strike == K) for k in ("put", "call")}

        def wing(kind):
            otm = [c for c in ok if c.kind == kind and (c.strike < spot if kind == "put" else c.strike > spot)
                   and abs(abs(c.delta) - cfg.WING_DELTA_TARGET) <= cfg.DELTA_TOL]
            return min(otm, key=lambda c: abs(abs(c.delta) - cfg.WING_DELTA_TARGET)) if otm else None
        w = {k: wing(k) for k in ("put", "call")}
        picks = {"straddle_hold": atm, "straddle_2x": atm}
        if w["put"] and w["call"]:
            picks["strangle30_hold"] = w
        return {"picks": picks, "expiry": exp, "spot": spot}, "ok"

    # ---------------------------------------------------------------- entry
    def enter_all(self, plan):
        cfg, spot, exp = self.cfg, plan["spot"], plan["expiry"]
        cv = 0.001
        lines = []
        for name, legs in plan["picks"].items():
            cv = float(self.product(legs["put"].symbol).get("contract_value", 0.001))
            cost = legs["put"].ask + legs["call"].ask
            fees_in = self.fee(legs["put"].ask, spot) + self.fee(legs["call"].ask, spot)
            self.state["paper"][name] = {
                "strategy": name, "entry_ts": self.now(), "expiry": exp.strftime("%Y-%m-%d %H:%M"),
                "expiry_ts": exp.timestamp(), "lots": cfg.LOTS, "cv": cv, "spot": spot,
                "legs": {k: {"symbol": c.symbol, "strike": c.strike, "kind": k, "entry": c.ask}
                         for k, c in legs.items()},
                "cost": cost, "fees_in": fees_in, "tp": cfg.TP_MULT if name == "straddle_2x" else 0.0,
                "best": 0.0}
            usd = (cost + fees_in) * cv * cfg.LOTS
            lines.append(f"{STRATS[name][:1]}: {legs['put'].strike:.0f}P/{legs['call'].strike:.0f}C "
                         f"cost ${usd:.2f}")
        self.state["last_thu"] = self.ist().date().isoformat()
        self.save()
        self.notify(f"Paper BUY for {exp:%d %b} 17:30 expiry, BTC {spot:,.0f}, x{cfg.LOTS} each (no real "
                    f"orders): " + "; ".join(lines) + f". C sells at {cfg.TP_MULT:g}x; A and B hold to 17:30.")

    # ---------------------------------------------------------------- watch
    def value(self, pos):
        """What both legs fetch now at the bid, after exit fees (per BTC)."""
        bids = {}
        for k, l in pos["legs"].items():
            q = (self.c.ticker(l["symbol"]) or {}).get("quotes") or {}
            bids[k] = st._f(q.get("best_bid"))
        return sum(b - self.fee(b, pos["spot"]) for b in bids.values()), bids

    def watch(self, name, pos):
        if self.now() - pos.get("checked", 0) < self.cfg.CHECK_SECONDS:
            return
        pos["checked"] = self.now()
        val, bids = self.value(pos)
        paid = pos["cost"] + pos["fees_in"]
        mult = val / paid if paid > 0 else 0
        if mult > pos.get("best", 0):
            pos["best"] = round(mult, 3)
            pos["best_ist"] = self.ist().strftime("%H:%M")
            self.save()
        if pos["tp"] and val >= pos["tp"] * paid:
            units = pos["lots"] * pos["cv"]
            fees_out = sum(self.fee(b, pos["spot"]) for b in bids.values())
            net = (sum(bids.values()) - pos["cost"] - pos["fees_in"] - fees_out) * units
            self.record(pos, exit_value=sum(bids.values()), settle="", fees_out=fees_out, net=net,
                        note=f"sold at {mult:.2f}x at {self.ist():%H:%M}")
            del self.state["paper"][name]
            self.save()
            self.notify(f"{STRATS[name]}: SOLD at {mult:.2f}x cost at {self.ist():%H:%M}, BTC "
                        f"{self.spot():,.0f}. Net {net:+.2f} USD (paper). " + self.summary_text())

    # ----------------------------------------------------------- settlement
    def settle_price(self, pos):
        sym = pos["legs"]["put"]["symbol"]
        try:
            s = st._f(self.product(sym, fresh=True).get("settlement_price"))
        except DeltaAPIError:
            s = 0.0
        if s > 0.3 * pos["legs"]["put"]["strike"]:
            return s, "exchange settlement"
        return self.spot(), "spot at settlement (exchange price not available)"

    def settle_one(self, name, pos):
        S, how = self.settle_price(pos)
        units = pos["lots"] * pos["cv"]
        vals = [intrinsic(l["kind"], l["strike"], S) for l in pos["legs"].values()]
        fees_out = sum(self.fee(v, pos["spot"]) for v in vals)
        net = (sum(vals) - pos["cost"] - pos["fees_in"] - fees_out) * units
        best = pos.get("best", 0)
        self.record(pos, exit_value=sum(vals), settle=S, fees_out=fees_out, net=net,
                    note=f"settled ({how}); best seen {best:.2f}x at {pos.get('best_ist', '-')}")
        del self.state["paper"][name]
        self.save()
        return net, S

    def record(self, pos, exit_value, settle, fees_out, net, note):
        p = Path(self.cfg.DATA_DIR) / "thursday.csv"
        new = not p.exists()
        units = pos["lots"] * pos["cv"]
        with p.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(COLS)
            w.writerow([pos["strategy"], datetime.fromtimestamp(pos["entry_ts"], IST).strftime("%Y-%m-%d %H:%M"),
                        pos["expiry"], pos["lots"], pos["legs"]["put"]["strike"], pos["legs"]["call"]["strike"],
                        round(pos["spot"], 1), round(pos["cost"], 2), round(pos["fees_in"], 4),
                        self.ist().strftime("%Y-%m-%d %H:%M"), round(exit_value, 2), settle and round(settle, 1),
                        round(fees_out, 4), round(net, 4),
                        round(net / ((pos["cost"] + pos["fees_in"]) * units), 4), note])

    def rows(self):
        p = Path(self.cfg.DATA_DIR) / "thursday.csv"
        if not p.exists():
            return []
        with p.open() as f:
            return list(csv.DictReader(f))

    def summary_text(self):
        rows = self.rows()
        if not rows:
            return ""
        out = []
        for name in STRATS:
            r = [float(x["net_usd"]) for x in rows if x["strategy"] == name]
            if r:
                out.append(f"{STRATS[name][:1]} {sum(r):+.2f} ({len(r)} Thu, "
                           f"{sum(v > 0 for v in r)} wins)")
        return "Paper totals: " + ", ".join(out) + "."

    # ------------------------------------------------------------------ loop
    def step(self):
        paper = self.state["paper"]
        for name, pos in list(paper.items()):
            if self.now() >= pos["expiry_ts"] + self.cfg.SETTLE_WAIT_MIN * 60:
                self.settle_one(name, pos)
                if not self.state["paper"]:
                    self.notify(f"SETTLED {pos['expiry']} (paper). " + self.report_lines(last=True)
                                + " " + self.summary_text())
            elif self.now() < pos["expiry_ts"]:
                self.watch(name, pos)
        if paper:
            return
        today = self.ist().date().isoformat()
        if not self.is_day() or not self.in_entry_window() or self.state.get("last_thu") == today:
            return
        plan, why = self.pick()
        if not plan:
            self.note("skip", f"no paper entry yet: {why}", every_s=600)
            return
        self.enter_all(plan)

    def report_lines(self, last=False):
        rows = self.rows()
        if last and rows:
            exp = rows[-1]["expiry"]
            rows = [r for r in rows if r["expiry"] == exp]
            return "This Thursday: " + "; ".join(f"{STRATS[r['strategy']][:1]} {float(r['net_usd']):+.2f}"
                                                 for r in rows) + "."
        return ""

    def run(self):
        self.notify(f"Thursday paper tracker started (no real orders): every {self.cfg.DAYS} "
                    f"{self.cfg.ENTRY_IST} IST, x{self.cfg.LOTS} each — " + "; ".join(STRATS.values()) + ".")
        while True:
            try:
                self.step()
            except DeltaAPIError as e:
                log.warning(f"API error: {e}")
            except Exception:
                log.exception("unexpected error")
            self.sleep(10 if self.state["paper"] else self.cfg.LOOP_SECONDS)

    def status(self):
        if self.state["paper"]:
            out = []
            for name, pos in self.state["paper"].items():
                val, _ = self.value(pos)
                paid = pos["cost"] + pos["fees_in"]
                out.append(f"{STRATS[name]}: {pos['legs']['put']['strike']:.0f}P/{pos['legs']['call']['strike']:.0f}C, "
                           f"worth {val / paid:.2f}x cost now (best {pos.get('best', 0):.2f}x), open P&L "
                           f"{(val - paid) * pos['lots'] * pos['cv']:+.2f} USD")
            return "\n".join(out)
        return f"Idle: next paper buy on {self.cfg.DAYS} at {self.cfg.ENTRY_IST} IST. " + self.summary_text()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    cfg = ThursdayConfig
    setup_logging(cfg.DATA_DIR)
    bot = ThursdayPaper(DeltaClient(env=cfg.DELTA_ENV), cfg)
    if a.report:
        rows = bot.rows()
        if not rows:
            print("No paper Thursdays yet.")
            return
        for name, lab in STRATS.items():
            r = [float(x["net_usd"]) for x in rows if x["strategy"] == name]
            if r:
                print(f"{lab:40} {len(r):3} Thursdays  total {sum(r):+8.2f} USD  wins {sum(v > 0 for v in r)}  "
                      f"best {max(r):+.2f}  worst {min(r):+.2f}")
        print()
        print((Path(cfg.DATA_DIR) / "thursday.csv").read_text())
        return
    if a.status:
        print(bot.status())
        return
    if a.once:
        plan, why = bot.pick()
        if not plan:
            print("Would not buy now:", why)
            return
        print(f"Expiry {plan['expiry']:%d %b %H:%M} IST, BTC {plan['spot']:,.0f}, x{cfg.LOTS} each")
        for name, legs in plan["picks"].items():
            cost = legs["put"].ask + legs["call"].ask
            print(f"  {STRATS[name]:40} {legs['put'].symbol} ask {legs['put'].ask} + {legs['call'].symbol} ask "
                  f"{legs['call'].ask} = {cost:.1f}/BTC (${cost * 0.001 * cfg.LOTS:.2f})")
        print(f"Today ({bot.ist():%A}) is {'a' if bot.is_day() else 'not a'} paper-trading day.")
        return
    bot.run()


if __name__ == "__main__":
    main()
