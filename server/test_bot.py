"""Offline tests: run the bot against a simulated Delta exchange (no network, no keys)."""
import csv
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

import numpy as np

import strategy as st
from bot import IST, Bot
from config import Config

# tests use fixed settings, independent of the live .env
Config.HEDGE_MAX_WIDTH = 1e9   # test strikes are 3,500 apart
Config.ORDER_SIZE = 1
Config.MAX_LOSSES_PER_DAY = 2      # tests still check the optional count limits
Config.MAX_TRADES_PER_DAY = 3
Config.COOLDOWN_MIN = 15
Config.MAX_DAILY_LOSS_USD = 20.0
Config.MAX_LOSS_PER_TRADE_USD = 40.0

NOW = datetime(2026, 10, 21, 20, 0, tzinfo=IST).timestamp()   # 8 PM IST; expiry tomorrow 17:30


def make_candles(n, step, end, start_px, drift, noise, tail=None, seed=1):
    rng = np.random.default_rng(seed)
    px, out = start_px, []
    for i in range(n):
        move = drift + rng.normal(0, noise)
        if tail and i >= n - len(tail):
            move = tail[i - (n - len(tail))]
        o, c = px, px + move
        out.append({"time": int(end - (n - i) * step), "open": o, "close": c,
                    "high": max(o, c) + abs(noise) / 2, "low": min(o, c) - abs(noise) / 2,
                    "volume": 100 + rng.random() * 50})
        px = c
    return out


PUTS = [(107000, -0.07, 80), (108500, -0.15, 200), (109500, -0.22, 350),
        (110500, -0.29, 500), (111500, -0.38, 650)]
CALLS = [(117000, 0.07, 80), (115500, 0.15, 200), (114500, 0.22, 350),
         (113500, 0.31, 500), (112500, 0.40, 650)]


class FakeClient:
    def __init__(self, trend="up", dip=True):
        drift = 40 if trend == "up" else -40 if trend == "down" else 0
        start = 104000 if trend == "up" else 120000 if trend == "down" else 112000
        self.c15 = make_candles(200, 900, NOW, start, drift, 60)
        tail = [-120] * 8 if (dip and trend == "up") else [120] * 8 if dip else None
        self.c5 = make_candles(120, 300, NOW, 111000, drift / 3, 25, tail=tail)
        self.spot = 112000.0
        self.sfx = (datetime.fromtimestamp(NOW, IST).date() + timedelta(days=1)).strftime("%d%m%y")
        self.chain, pid = [], 9000
        for kind, rows in (("put", PUTS), ("call", CALLS)):
            for strike, dl, prem in rows:
                self.chain.append({
                    "symbol": f"{'P' if kind == 'put' else 'C'}-BTC-{strike}-{self.sfx}",
                    "product_id": pid, "contract_type": f"{kind}_options",
                    "strike_price": str(strike), "mark_price": str(prem),
                    "greeks": {"delta": str(dl), "theta": str(-prem * 0.7)},
                    "quotes": {"best_bid": str(prem - 5), "best_ask": str(prem + 5),
                               "bid_iv": "0.40", "ask_iv": "0.44"},
                })
                pid += 1
        self.chain.append({**self.chain[3], "symbol": "P-BTC-110000-251026", "product_id": 1})
        self.premium, self.delta = {}, {}
        self.orders, self.pos, self.cancelled = [], {}, []
        self.unfillable = set()

    def _sym(self, pid):
        return next(c["symbol"] for c in self.chain if c["product_id"] == pid)

    # public
    def candles(self, symbol, res, start, end):
        return self.c15 if res == "15m" else self.c5

    def option_chain(self, und, exp):
        return self.chain

    def product(self, symbol):
        return {"contract_value": "0.001", "tick_size": "0.5", "settlement_price": None}

    def ticker(self, symbol):
        if symbol == "BTCUSD":
            return {"spot_price": str(self.spot), "mark_price": str(self.spot)}
        base = next(c for c in self.chain if c["symbol"] == symbol)
        m = self.premium.get(symbol, float(base["mark_price"]))
        return {"mark_price": str(m),
                "greeks": {"delta": str(self.delta.get(symbol, base["greeks"]["delta"]))},
                "quotes": {"best_bid": str(max(m - 3, 0)), "best_ask": str(m + 3),
                           "bid_iv": "0.40", "ask_iv": "0.44"}}

    # private (live mode)
    def place_order(self, **o):
        self.orders.append(o)
        sym = self._sym(o["product_id"])
        if o.get("stop_order_type"):
            return {"id": 777, "size": o["size"], "unfilled_size": o["size"]}
        if sym in self.unfillable:
            return {"id": len(self.orders), "size": o["size"], "unfilled_size": o["size"]}
        q = self.ticker(sym)["quotes"]
        px = float(o.get("limit_price") or (q["best_ask"] if o["side"] == "buy" else q["best_bid"]))
        self.pos[sym] = self.pos.get(sym, 0) + (o["size"] if o["side"] == "buy" else -o["size"])
        return {"id": len(self.orders), "size": o["size"], "unfilled_size": 0,
                "average_fill_price": str(px)}

    def cancel_order(self, oid, pid):
        self.cancelled.append(oid)

    def wallet(self):
        return [{"asset_symbol": "USD", "balance": "235", "available_balance": "200"}]

    def get_order(self, oid):
        return {"id": oid, "size": 1, "unfilled_size": 1}   # resting orders here never fill later

    def positions(self):
        return [{"product_symbol": s, "size": n} for s, n in self.pos.items() if n]


def cfg_for(tmp, **kw):
    return type("Cfg", (Config,), {"DATA_DIR": tmp, "TELEGRAM_BOT_TOKEN": "", **kw})


class Base(unittest.TestCase):
    settings = {"MODE": "paper", "HEDGE": "off", "RUNNER": False}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfg_for(self.tmp, **self.settings)
        self.clock = [NOW]
        self.fc = FakeClient()
        self.bot = Bot(self.fc, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def advance(self, minutes):
        self.clock[0] += minutes * 60

    def book(self, v="naked"):
        return self.bot.state["books"][v]

    def pos(self, v="naked"):
        o = self.book(v)["open"]
        return o[0] if o else None

    def trades(self):
        with open(f"{self.tmp}/trades.csv") as f:
            return list(csv.DictReader(f))

    def set_cost(self, pos, cost):
        """Move the sold leg's mark so that the position's close cost (by mark) ~= cost."""
        wing = sum(float(self.fc.ticker(l["symbol"])["mark_price"]) for l in pos["legs"][1:])
        self.fc.premium[pos["legs"][0]["symbol"]] = cost + wing


# ------------------------------------------------------------------ signals
class TestSignals(unittest.TestCase):
    def test_D_sign_follows_trend(self):
        up, dn = st.direction_score(FakeClient("up").c15), st.direction_score(FakeClient("down").c15)
        self.assertGreater(up.D, 0.4)
        self.assertLess(dn.D, -0.4)

    def test_rsi_dip_triggers_put_entry(self):
        r = st.last_rsi(FakeClient("up").c5)
        self.assertLessEqual(r, 40)
        self.assertTrue(st.entry_triggered("bull", r))
        self.assertFalse(st.entry_triggered("bear", r))

    def test_fee_model(self):
        self.assertAlmostEqual(st.fee_pct_side(200, 112000, Config), 0.035 * 1.18)
        self.assertAlmostEqual(st.fee_pct_side(2000, 112000, Config), Config.FEE_RATE_NOTIONAL * 112000 / 2000 * 1.18)
        self.assertEqual(st.fee_abs(0, 112000, Config), 0)        # worthless at expiry: no fee

    def test_select_main_and_wing(self):
        fc = FakeClient()
        chain = st.parse_chain(fc.chain, fc.sfx)
        self.assertNotIn("P-BTC-110000-251026", [c.symbol for c in chain])
        main, _ = st.select_main(chain, "bull", Config)
        self.assertEqual((main.kind, main.strike), ("put", 110500))
        wing, _ = st.select_wing(chain, main, Config)
        self.assertEqual(wing.strike, 107000)               # further OTM, delta ~0.08
        cmain, _ = st.select_main(chain, "bear", Config)
        cwing, _ = st.select_wing(chain, cmain, Config)
        self.assertGreater(cwing.strike, cmain.strike)

    def test_plans_and_gates(self):
        fc = FakeClient()
        chain = st.parse_chain(fc.chain, fc.sfx)
        main, _ = st.select_main(chain, "bull", Config)
        wing, _ = st.select_wing(chain, main, Config)
        naked = st.build_plan("naked", main, None, 112000, Config)
        spread = st.build_plan("spread", main, wing, 112000, Config)
        self.assertEqual(spread.credit, main.bid - wing.ask)
        self.assertAlmostEqual(spread.max_loss, 3500 - spread.credit)
        self.assertGreater(spread.tp_gross, naked.tp_gross)  # fees bite harder on a spread
        self.assertTrue(st.gate_plan(naked, Config, 4)[0])
        self.assertTrue(st.gate_plan(spread, Config, 4)[0])
        slow = st.build_plan("naked", st.Candidate(**{**main.__dict__, "theta_day": -5}), None,
                             112000, Config)
        ok, why = st.gate_plan(slow, Config, 4)
        self.assertFalse(ok)
        self.assertIn("decay gate", why)

    def test_prob_otm(self):
        z, p = st.prob_otm("put", 112000, 100000, 0.5, 2)
        self.assertGreater(p, 0.99)
        z, p = st.prob_otm("call", 112000, 112000, 0.5, 10)
        self.assertAlmostEqual(p, 0.5)

    def test_runner_check(self):
        ok, *_ = st.runner_check("bull", 0.6, "put", 112000, 110500, 0.4, 20, -0.2, Config)
        self.assertTrue(ok)
        ok, *_, why = st.runner_check("bull", 0.2, "put", 112000, 110500, 0.4, 20, -0.2, Config)
        self.assertFalse(ok)
        self.assertIn("D=", why)
        ok, *_ = st.runner_check("bull", 0.6, "put", 111000, 110500, 0.6, 20, -0.2, Config)
        self.assertFalse(ok)                                 # too close to strike


# ---------------------------------------------------------------- paper flow
class TestPaperNaked(Base):
    def test_entry_then_take_profit_is_3pct_net(self):
        self.bot.step()
        p = self.pos()
        self.assertEqual(p["legs"][0]["symbol"][:2], "P-")
        self.fc.premium[p["legs"][0]["symbol"]] = p["tp_cost"] - 4
        self.advance(30)
        self.bot.step()
        self.assertIsNone(self.pos())
        t = self.trades()[-1]
        self.assertEqual(t["reason"], "take_profit")
        self.assertGreaterEqual(float(t["net_pct_credit"]), 0.029)

    def test_stop_loss_and_loss_limit(self):
        for _ in range(2):
            self.bot.step()
            p = self.pos()
            self.fc.premium[p["legs"][0]["symbol"]] = p["sl_cost"] + 1
            self.advance(10)
            self.bot.step()
            self.assertIsNone(self.pos())
            self.fc.premium.clear()
            self.advance(20)
        self.assertEqual(self.book()["losses"], 2)
        self.bot.step()
        self.assertIsNone(self.pos())
        self.assertIn("loss-count", self.bot.blocked_reason(self.book()))

    def test_time_stop(self):
        self.bot.step()
        self.advance(self.cfg.MAX_HOLD_MIN + 1)
        self.bot.step()
        self.assertEqual(self.trades()[-1]["reason"], "time_stop")

    def test_direction_flip_exit(self):
        self.bot.step()
        self.fc.c15 = FakeClient("down").c15
        self.advance(6)
        self.bot.step()
        self.assertTrue(self.trades()[-1]["reason"].startswith("direction_flip"))

    def test_sideways_no_trade_when_strangles_off(self):
        self.bot.cfg = cfg_for(self.tmp, MODE="paper", HEDGE="off", RUNNER=False, STRANGLE=False)
        self.bot.c = FakeClient("flat", dip=False)
        self.bot.step()
        self.assertIsNone(self.pos())

    def test_state_survives_restart(self):
        self.bot.step()
        bot2 = Bot(self.fc, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)
        self.assertEqual(bot2.state["books"]["naked"]["open"][0]["id"], self.pos()["id"])


class TestRunner(Base):
    settings = {"MODE": "paper", "HEDGE": "off", "RUNNER": True}

    def _to_runner(self):
        self.bot.step()
        p = self.pos()
        sym = p["legs"][0]["symbol"]
        self.fc.premium[sym] = p["tp_cost"] - 4
        self.fc.delta[sym] = -0.20
        self.fc.c15 = make_candles(200, 900, NOW, 106000, 40, 60)   # BTC moved further our way
        self.advance(30)
        self.bot.step()
        self.assertEqual(self.pos()["mode"], "runner")
        return self.pos()

    def test_weak_trend_takes_3pct_instead(self):
        self.bot.step()
        p = self.pos()
        self.fc.premium[p["legs"][0]["symbol"]] = p["tp_cost"] - 4
        self.fc.delta[p["legs"][0]["symbol"]] = -0.28
        self.fc.c15 = make_candles(200, 900, NOW, 111000, 4, 60)     # trend faded, D small
        self.advance(30)
        self.bot.step()
        self.assertEqual(self.trades()[-1]["reason"], "take_profit")

    def test_runner_trails_and_locks_bigger_profit(self):
        p = self._to_runner()
        sym = p["legs"][0]["symbol"]
        for prem in (300, 150, 60):            # premium keeps melting
            self.fc.premium[sym] = prem
            self.advance(30)
            self.bot.step()
            self.assertIsNotNone(self.pos())
        self.fc.premium[sym] = 130             # rebounds > 100% off its low of 60
        self.advance(10)
        self.bot.step()
        t = self.trades()[-1]
        self.assertEqual(t["kind"], "runner")
        self.assertTrue(t["reason"].startswith("runner_trail"))
        self.assertGreater(float(t["net_pct_credit"]), 0.6)

    def test_runner_floor_is_breakeven(self):
        p = self._to_runner()
        self.fc.premium[p["legs"][0]["symbol"]] = p["breakeven_cost"] + 1
        self.advance(5)
        self.bot.step()
        t = self.trades()[-1]
        self.assertTrue(t["reason"].startswith("runner_trail"))
        self.assertGreater(float(t["net_pct_credit"]), -0.04)  # ~breakeven, not a stop-loss

    def test_runner_expires_worthless(self):
        p = self._to_runner()
        self.fc.premium[p["legs"][0]["symbol"]] = 5
        self.clock[0] = p["expiry_ts"] + 120
        self.bot.step()
        t = self.trades()[-1]
        self.assertEqual(t["reason"], "settled_at_expiry")
        self.assertEqual(float(t["close_cost"]), 0.0)
        self.assertGreater(float(t["net_pct_credit"]), 0.95)   # only the entry fee paid

    def test_runner_allows_a_new_scalp(self):
        self._to_runner()
        self.advance(20)
        self.fc.premium.clear()
        self.assertIsNone(self.bot.blocked_reason(self.book()))

    def test_runner_spot_guard(self):
        p = self._to_runner()
        self.fc.c15 = make_candles(200, 900, NOW, 104000, 40, 60)
        last = self.fc.c15[-1]
        last["close"] = last["high"] = 110600.0   # spot almost at the 110500 strike
        self.fc.premium[p["legs"][0]["symbol"]] = p["tp_cost"] - 10
        self.advance(6)
        self.bot.step()
        self.assertIn(self.trades()[-1]["reason"].split(" ")[0],
                      ("runner_spot_guard", "runner_direction_flip"))


class TestCompare(Base):
    settings = {"MODE": "paper", "HEDGE": "compare", "RUNNER": False}

    def test_both_books_trade_same_signal(self):
        self.bot.step()
        n, s = self.pos("naked"), self.pos("spread")
        self.assertEqual(len(n["legs"]), 1)
        self.assertEqual(len(s["legs"]), 2)
        self.assertEqual(n["legs"][0]["symbol"], s["legs"][0]["symbol"])
        self.assertLess(s["credit"], n["credit"])
        # same market move closes both
        self.fc.premium[n["legs"][0]["symbol"]] = 10
        self.advance(30)
        self.bot.step()
        self.assertEqual({t["variant"] for t in self.trades()}, {"naked", "spread"})


# ----------------------------------------------------------------- live flow
class TestLiveNaked(Base):
    settings = {"MODE": "live", "HEDGE": "off", "RUNNER": False}

    def test_orders_backstop_and_cancel(self):
        self.bot.step()
        p = self.pos()
        entry, stop = self.fc.orders[0], self.fc.orders[1]
        self.assertEqual((entry["side"], entry["time_in_force"]), ("sell", "gtc"))
        self.assertEqual(stop["stop_order_type"], "stop_loss_order")
        self.assertTrue(stop["reduce_only"])
        self.fc.premium[p["legs"][0]["symbol"]] = p["tp_cost"] - 4
        self.advance(20)
        self.bot.step()
        self.assertIsNone(self.pos())
        self.assertEqual(self.fc.cancelled, [777])
        self.assertEqual(self.fc.positions(), [])

    def test_backstop_hit_while_away(self):
        self.bot.step()
        p = self.pos()
        self.fc.pos.clear()
        self.fc.premium[p["legs"][0]["symbol"]] = p["credit"] * 1.3
        self.advance(5)
        self.bot.step()
        self.assertIsNone(self.pos())
        self.assertEqual(self.book()["losses"], 1)


class TestLiveSpread(Base):
    settings = {"MODE": "live", "HEDGE": "on", "RUNNER": False}

    def test_wing_bought_first_and_closed_last(self):
        self.bot.step()
        p = self.pos("spread")
        self.assertEqual([o["side"] for o in self.fc.orders[:2]], ["buy", "sell"])
        self.assertFalse(any(o.get("stop_order_type") for o in self.fc.orders))   # hedge, no backstop
        self.set_cost(p, p["tp_cost"] - 10)
        self.advance(20)
        self.bot.step()
        closing = self.fc.orders[2:]
        self.assertEqual([o["side"] for o in closing], ["buy", "sell"])  # buy back main, sell wing
        self.assertTrue(all(o["reduce_only"] for o in closing))
        self.assertEqual(self.fc.positions(), [])
        self.assertEqual(self.trades()[-1]["reason"], "take_profit")

    def test_main_unfilled_unwinds_wing(self):
        self.fc.unfillable.add(f"P-BTC-110500-{self.fc.sfx}")
        self.bot.step()
        self.assertIsNone(self.pos("spread"))
        self.assertEqual(self.fc.positions(), [])            # wing sold back


# ------------------------------------------------------- sideways strangles
def flat(fc):
    f = FakeClient("flat", dip=False)
    fc.c15, fc.c5 = f.c15, f.c5
    return fc


class TestStranglePaper(Base):
    settings = {"MODE": "paper", "HEDGE": "compare", "RUNNER": True}

    def setUp(self):
        super().setUp()
        flat(self.fc)

    def test_sideways_opens_strangle_and_condor(self):
        self.assertEqual(st.bias_from_D(st.direction_score(self.fc.c15).D, 0.3), None)
        self.bot.step()
        n, s = self.pos("naked"), self.pos("spread")
        self.assertEqual((n["name"], len(n["legs"])), ("short strangle", 2))
        self.assertEqual((s["name"], len(s["legs"])), ("iron condor", 4))
        self.assertEqual({l["kind"] for l in n["legs"]}, {"put", "call"})
        self.assertTrue(all(l["side"] == "sell" for l in n["legs"]))
        self.assertEqual(sorted(l["side"] for l in s["legs"]), ["buy", "buy", "sell", "sell"])
        for l in n["legs"]:
            self.assertAlmostEqual(abs(l["delta"]), 0.22)          # nearest to 0.20
        self.assertEqual(n["bias"], "neutral")

    def test_strangle_take_profit_both_legs_counted(self):
        self.bot.cfg = cfg_for(self.tmp, MODE="paper", HEDGE="off", RUNNER=False)
        self.bot.state["books"].pop("spread", None)
        self.bot.step()
        p = self.pos()
        for l in p["legs"]:
            self.fc.premium[l["symbol"]] = (p["tp_cost"] - 10) / 2
        self.advance(30)
        self.bot.step()
        t = self.trades()[-1]
        self.assertEqual((t["reason"], t["bias"]), ("take_profit", "neutral"))
        self.assertEqual(t["legs"].count("sell"), 2)
        self.assertGreaterEqual(float(t["net_pct_credit"]), 0.029)

    def test_trend_starting_closes_strangle(self):
        self.bot.step()
        self.fc.c15 = FakeClient("up").c15                   # |D| jumps above 0.45
        self.advance(6)
        self.bot.step()
        reasons = {r["reason"].split(" ")[0] for r in self.trades()}
        self.assertEqual(reasons, {"trend_started"})

    def test_quiet_market_strangle_runs_to_expiry(self):
        self.bot.cfg = cfg_for(self.tmp, MODE="paper", HEDGE="compare", RUNNER=True,
                               RUNNER_MIN_POTM=0.65)
        self.bot.step()
        p = self.pos("naked")
        for l in p["legs"]:
            self.fc.premium[l["symbol"]] = (p["tp_cost"] - 20) / 2
            self.fc.delta[l["symbol"]] = 0.12 if l["kind"] == "call" else -0.12
        self.advance(30)
        self.bot.step()
        self.assertEqual(self.pos("naked")["mode"], "runner")
        for l in p["legs"]:
            self.fc.premium[l["symbol"]] = 2
        self.clock[0] = p["expiry_ts"] + 60
        self.fc.spot = 112000                                 # settles between the strikes
        self.bot.step()
        t = [r for r in self.trades() if r["variant"] == "naked"][-1]
        self.assertEqual((t["kind"], t["reason"]), ("runner", "settled_at_expiry"))
        self.assertEqual(float(t["close_cost"]), 0.0)
        self.assertGreater(float(t["net_pct_credit"]), 0.95)

    def test_report_splits_trend_and_sideways(self):
        import io, contextlib, bot as botmod
        self.bot.step()
        self.advance(self.cfg.MAX_HOLD_MIN + 1)
        self.bot.step()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            botmod.report(f"{self.tmp}/trades.csv")
        self.assertIn("sideways", buf.getvalue())


class TestStrangleLive(Base):
    settings = {"MODE": "live", "HEDGE": "on", "RUNNER": False}

    def setUp(self):
        super().setUp()
        flat(self.fc)

    def test_condor_wings_first_then_shorts_and_reverse_on_exit(self):
        self.bot.step()
        p = self.pos("spread")
        self.assertEqual([o["side"] for o in self.fc.orders], ["buy", "buy", "sell", "sell"])
        self.assertFalse(any(o.get("stop_order_type") for o in self.fc.orders))
        self.assertEqual(len(self.fc.positions()), 4)
        for l in p["legs"]:
            if l["side"] == "sell":
                self.fc.premium[l["symbol"]] = 60
        self.advance(20)
        self.bot.step()
        closing = self.fc.orders[4:]
        self.assertEqual([o["side"] for o in closing], ["buy", "buy", "sell", "sell"])
        self.assertTrue(all(o["reduce_only"] for o in closing))
        self.assertEqual(self.fc.positions(), [])
        self.assertEqual(self.trades()[-1]["reason"], "take_profit")

    def test_failed_short_unwinds_everything(self):
        call = next(c["symbol"] for c in self.fc.chain if c["symbol"].startswith("C-BTC-114500"))
        self.fc.unfillable.add(call)
        self.bot.step()
        self.assertIsNone(self.pos("spread"))
        self.assertEqual(self.fc.positions(), [])            # wings and the filled put closed again


class TestNakedStrangleLive(Base):
    settings = {"MODE": "live", "HEDGE": "off", "RUNNER": False}

    def setUp(self):
        super().setUp()
        flat(self.fc)

    def test_backstop_per_leg_and_one_leg_stopped_out(self):
        self.bot.step()
        p = self.pos()
        stops = [o for o in self.fc.orders if o.get("stop_order_type")]
        self.assertEqual(len(stops), 2)
        for o, l in zip(stops, p["legs"]):
            self.assertGreaterEqual(float(o["stop_price"]), l["entry"] * 2)   # leg doubles
        put = next(l for l in p["legs"] if l["kind"] == "put")
        self.fc.pos.pop(put["symbol"])                         # exchange stop fired on the put
        self.fc.premium[put["symbol"]] = put["entry"] * 2.1
        self.advance(5)
        self.bot.step()
        self.assertIsNone(self.pos())
        self.assertEqual(self.fc.positions(), [])              # the call was bought back too
        self.assertEqual(self.trades()[-1]["reason"], "closed_on_exchange")


if __name__ == "__main__":
    unittest.main(verbosity=1)
