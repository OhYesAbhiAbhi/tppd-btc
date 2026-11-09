"""Offline tests for the nightly condor (no network, no keys)."""
import csv
import math
import shutil
import tempfile
import unittest
from datetime import datetime

import condor as cd
from condor import IST, Condor, CondorConfig

EVENING = datetime(2026, 10, 21, 21, 40, tzinfo=IST).timestamp()    # inside 21:30 + 60 min
EXPIRY = datetime(2026, 10, 22, 17, 30, tzinfo=IST).timestamp()


def ndelta(kind, K, S, sigma=0.35, hours=20):
    t = hours / 8760
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * t) / (sigma * math.sqrt(t))
    nd = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return nd if kind == "call" else nd - 1


class FakeDelta:
    """Daily-expiry BTC chain around spot; GTC orders fill when cancelled (i.e. after resting),
    except for symbols in `never_fill`."""

    def __init__(self, spot=82000.0, sfx="221026", used=0.1):
        self.spot, self.sfx, self.used = spot, sfx, used
        self.orders, self.pos, self.never_fill, self.n, self.done = [], {}, set(), 0, {}
        self.settlement = None
        self.chain = []
        pid = 100
        for K in range(78000, 86001, 200):
            for kind in ("put", "call"):
                d = ndelta(kind, K, spot)
                prem = max(abs(d) * 900, 3)
                self.chain.append({"symbol": f"{'P' if kind == 'put' else 'C'}-BTC-{K}-{sfx}", "product_id": pid,
                                   "contract_type": f"{kind}_options", "strike_price": str(K),
                                   "mark_price": str(prem), "greeks": {"delta": str(round(d, 3)), "theta": "-50"},
                                   "quotes": {"best_bid": str(round(prem * 0.97, 1)),
                                              "best_ask": str(round(prem * 1.03, 1))}})
                pid += 1

    def _c(self, sym=None, pid=None):
        return next(c for c in self.chain if c["symbol"] == sym or c["product_id"] == pid)

    def option_chain(self, und, exp):
        return self.chain

    def ticker(self, symbol):
        if symbol == "BTCUSD":
            return {"spot_price": str(self.spot), "mark_price": str(self.spot)}
        return self._c(symbol)

    def product(self, symbol):
        return {"contract_value": "0.001", "tick_size": "0.1", "settlement_price": self.settlement}

    def wallet(self):
        return [{"asset_symbol": "USD", "balance": "235", "available_balance": str(235 * (1 - self.used))}]

    def positions(self):
        return [{"product_symbol": s, "size": n} for s, n in self.pos.items() if n]

    def place_order(self, **o):
        self.orders.append(o)
        self.n += 1
        sym = self._c(pid=o["product_id"])["symbol"]
        if o.get("time_in_force") == "gtc":
            self.done[self.n] = (o, sym)
            return {"id": self.n, "size": o["size"], "unfilled_size": o["size"]}
        self.pos[sym] = self.pos.get(sym, 0) + (o["size"] if o["side"] == "buy" else -o["size"])
        return {"id": self.n, "size": o["size"], "unfilled_size": 0, "average_fill_price": o.get("limit_price")}

    def cancel_order(self, oid, pid):
        o, sym = self.done[oid]
        got = 0 if sym in self.never_fill else o["size"]
        self.pos[sym] = self.pos.get(sym, 0) + (got if o["side"] == "buy" else -got)
        self.done[oid] = ({**o, "_got": got}, sym)

    def get_order(self, oid):
        o, sym = self.done[oid]
        got = o.get("_got", 0)
        return {"id": oid, "size": o["size"], "unfilled_size": o["size"] - got,
                "average_fill_price": o["limit_price"]}


class Base(unittest.TestCase):
    mode = "paper"
    style = "condor"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = type("C", (CondorConfig,), {"DATA_DIR": self.tmp, "MODE": self.mode,
                                               "TELEGRAM_BOT_TOKEN": "", "MAX_LOSS_USD": 40.0,
                                               "STYLE": self.style,
                                               "MAX_LOTS": 100})
        self.clock = [EVENING]
        self.fx = FakeDelta()
        self.b = Condor(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def rows(self):
        with open(f"{self.tmp}/trades.csv") as f:
            return list(csv.DictReader(f))


class TestPlanAndSchedule(Base):
    def test_window_and_expiry(self):
        self.assertTrue(self.b.in_entry_window())
        self.assertEqual(self.b.next_expiry().timestamp(), EXPIRY)
        self.clock[0] = datetime(2026, 10, 21, 20, 0, tzinfo=IST).timestamp()
        self.assertFalse(self.b.in_entry_window())
        self.b.step()
        self.assertIsNone(self.b.state["open"])

    def test_after_midnight_targets_same_day_expiry(self):
        self.clock[0] = datetime(2026, 10, 22, 0, 20, tzinfo=IST).timestamp()
        self.assertEqual(self.b.next_expiry().timestamp(), EXPIRY)

    def test_plan_strikes_and_size(self):
        p, why = self.b.plan()
        self.assertEqual(why, "ok")
        L = p["legs"]
        self.assertLess(L["put_short"].strike, 82000)
        self.assertGreater(L["call_short"].strike, 82000)
        self.assertLess(L["put_wing"].strike, L["put_short"].strike)
        self.assertGreater(L["call_wing"].strike, L["call_short"].strike)
        for s in ("put_short", "call_short"):
            self.assertAlmostEqual(abs(L[s].delta), 0.20, delta=0.05)
        self.assertLessEqual(p["width"], 1500)
        self.assertLessEqual(p["max_loss_usd"], 40.0)
        self.assertGreaterEqual(p["lots"], 1)


class TestPaper(Base):
    def test_enter_once_and_settle_win(self):
        self.b.step()
        pos = self.b.state["open"]
        self.assertIsNotNone(pos)
        self.clock[0] += 120
        self.b.step()                                   # no second entry the same night
        self.assertEqual(self.b.state["last_entry_date"], "2026-10-21")
        self.clock[0] = EXPIRY + 5 * 60
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])      # waits SETTLE_WAIT_MIN
        self.clock[0] = EXPIRY + 11 * 60                # BTC still at 82,000: all legs expire worthless
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        r = self.rows()[-1]
        self.assertEqual(float(r["payoff"]), 0.0)
        self.assertGreater(float(r["net_usd"]), 0)
        self.assertGreater(float(r["net_pct_credit"]), 0.85)

    def test_big_move_loss_is_capped(self):
        self.b.step()
        pos = self.b.state["open"]
        self.fx.spot = 70000                           # crash far through both put strikes
        self.clock[0] = EXPIRY + 11 * 60
        self.b.step()
        r = self.rows()[-1]
        self.assertLess(float(r["net_usd"]), 0)
        self.assertGreaterEqual(float(r["net_usd"]), -40.0 - 0.01)
        self.assertAlmostEqual(-float(r["net_usd"]), pos["max_loss_usd"], delta=0.5)

    def test_exchange_settlement_price_used(self):
        self.b.step()
        self.fx.settlement = "82100"
        self.clock[0] = EXPIRY + 11 * 60
        self.b.step()
        r = self.rows()[-1]
        self.assertEqual(float(r["settle_price"]), 82100.0)
        self.assertEqual(r["note"], "exchange settlement price")

    def test_restart_keeps_position(self):
        self.b.step()
        b2 = Condor(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)
        self.assertEqual(b2.state["open"]["lots"], self.b.state["open"]["lots"])

    def test_next_night_trades_again(self):
        self.b.step()
        self.clock[0] = EXPIRY + 11 * 60
        self.b.step()
        self.clock[0] = datetime(2026, 10, 22, 21, 35, tzinfo=IST).timestamp()
        self.fx.sfx = "231026"
        self.fx.__init__(sfx="231026")
        self.b.c = self.fx
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])
        self.assertEqual(self.b.state["last_entry_date"], "2026-10-22")


class TestLive(Base):
    mode = "live"

    def test_resting_orders_wings_first_positions_match(self):
        self.b.step()
        pos = self.b.state["open"]
        self.assertIsNotNone(pos)
        sides = [o["side"] for o in self.fx.orders]
        self.assertEqual(sides[:2], ["buy", "buy"])
        self.assertTrue(all(o["time_in_force"] == "gtc" for o in self.fx.orders))
        n = pos["lots"]
        held = {p["product_symbol"]: p["size"] for p in self.fx.positions()}
        L = pos["legs"]
        self.assertEqual(held[L["put_wing"]["symbol"]], n)
        self.assertEqual(held[L["call_wing"]["symbol"]], n)
        self.assertEqual(held[L["put_short"]["symbol"]], -n)
        self.assertEqual(held[L["call_short"]["symbol"]], -n)
        mid = float(self.fx._c(L["put_short"]["symbol"])["mark_price"])
        self.assertAlmostEqual(L["put_short"]["entry"], mid, delta=mid * 0.02)   # rested at ~mid

    def test_unfillable_short_unwinds_everything(self):
        p, _ = self.b.plan()
        self.fx.never_fill.add(p["legs"]["call_short"].symbol)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.positions(), [])

    def test_margin_check_blocks_entry(self):
        self.fx.used = 0.6
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.orders, [])

    def test_waits_for_exchange_to_settle(self):
        self.b.step()
        self.clock[0] = EXPIRY + 11 * 60
        self.b.step()                                   # positions still shown -> wait
        self.assertIsNotNone(self.b.state["open"])
        self.fx.pos.clear()                             # Delta settles them
        self.clock[0] += 60
        self.b.step()
        self.assertIsNone(self.b.state["open"])


def bump(fx, symbol, mult):
    c = fx._c(symbol)
    for k in ("best_bid", "best_ask"):
        c["quotes"][k] = str(round(float(c["quotes"][k]) * mult, 1))
    c["mark_price"] = str(float(c["mark_price"]) * mult)


class TestStranglePaper(Base):
    style = "strangle"

    def test_sells_two_legs_same_lots_as_condor(self):
        cfg_c = type("C", (self.cfg,), {"STYLE": "condor"})
        pc, _ = Condor(self.fx, cfg_c, now_fn=lambda: self.clock[0]).plan()
        p, why = self.b.plan()
        self.assertEqual(why, "ok")
        self.assertEqual(set(p["legs"]), {"put_short", "call_short"})
        self.assertEqual(p["lots"], pc["lots"])
        self.assertEqual(p["legs"]["put_short"].symbol, pc["legs"]["put_short"].symbol)

    def test_quiet_night_holds_and_settles_full_credit(self):
        self.b.step()
        pos = self.b.state["open"]
        self.clock[0] += 3600
        self.b.step()                                     # small open loss from the spread: no stop
        self.assertIsNotNone(self.b.state["open"])
        self.clock[0] = EXPIRY + 11 * 60
        self.b.step()
        r = self.rows()[-1]
        self.assertEqual(r["style"], "strangle")
        self.assertGreater(float(r["net_usd"]), 0.9 * pos["credit"] * pos["lots"] * pos["cv"] - 1)

    def test_stop_needs_two_readings_and_caps_near_40(self):
        self.b.step()
        pos = self.b.state["open"]
        sym = pos["legs"]["call_short"]["symbol"]
        units = pos["lots"] * pos["cv"]
        # raise the call price until the open loss is just past $40
        need = (40.5 / units + pos["credit"] + pos["fees_in"]) - float(self.fx._c(pos["legs"]["put_short"]["symbol"])["quotes"]["best_ask"])
        c = self.fx._c(sym)
        c["quotes"]["best_ask"] = str(round(need * 1.01, 1))
        self.clock[0] += 60
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])        # first reading: confirm only
        self.clock[0] += 10
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        r = self.rows()[-1]
        self.assertIn("stopped", r["note"])
        self.assertLess(float(r["net_usd"]), -40)
        self.assertGreater(float(r["net_usd"]), -46)

    def test_one_bad_tick_does_not_stop(self):
        self.b.step()
        pos = self.b.state["open"]
        c = self.fx._c(pos["legs"]["call_short"]["symbol"])
        good = c["quotes"]["best_ask"]
        c["quotes"]["best_ask"] = "99999"
        self.b.step()
        c["quotes"]["best_ask"] = good
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])
        self.assertEqual(self.b.state["open"]["below"], 0)

    def test_old_trades_file_gets_style_column(self):
        with open(f"{self.tmp}/trades.csv", "w") as f:
            f.write("mode,entry_ist,net_usd\nlive,2026-10-22 21:31,5.0\n")
        Condor(self.fx, self.cfg, now_fn=lambda: self.clock[0])
        r = self.rows()[0]
        self.assertEqual(r["style"], "condor")
        self.assertEqual(r["net_usd"], "5.0")


class TestFixedLotsTakeProfit(Base):
    style = "strangle"

    def setUp(self):
        super().setUp()
        self.cfg.FIXED_LOTS, self.cfg.TAKE_PROFIT, self.cfg.MAX_LOSS_USD = 100, 0.75, 90.0

    def test_fixed_100_lots_two_legs(self):
        p, why = self.b.plan()
        self.assertEqual(why, "ok")
        self.assertEqual(p["lots"], 100)
        self.assertEqual(set(p["legs"]), {"put_short", "call_short"})
        self.assertEqual(p["max_loss_usd"], 90.0)

    def test_no_take_profit_before_75pct_decay(self):
        self.b.step()
        pos = self.b.state["open"]
        for n in ("put_short", "call_short"):
            bump(self.fx, pos["legs"][n]["symbol"], 0.30)       # ~70% decayed: not yet
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_take_profit_at_75pct_decay(self):
        self.b.step()
        pos = self.b.state["open"]
        for n in ("put_short", "call_short"):
            bump(self.fx, pos["legs"][n]["symbol"], 0.22)
        self.clock[0] += 8 * 3600
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        r = self.rows()[-1]
        self.assertIn("take profit", r["note"])
        units = pos["lots"] * pos["cv"]
        self.assertGreater(float(r["net_usd"]), 0.6 * pos["credit"] * units)

    def test_stop_at_90_still_works(self):
        self.b.step()
        pos = self.b.state["open"]
        bump(self.fx, pos["legs"]["call_short"]["symbol"], 6)
        self.b.step()
        self.b.step()
        r = self.rows()[-1]
        self.assertIn("stopped", r["note"])
        self.assertLess(float(r["net_usd"]), -90)

    def test_off_by_default(self):
        self.assertEqual(CondorConfig.FIXED_LOTS, 0)
        self.assertEqual(CondorConfig.TAKE_PROFIT, 0.0)


class TestFixedLotsLive(TestFixedLotsTakeProfit):
    mode = "live"

    def test_live_take_profit_buys_back_100(self):
        self.b.step()
        pos = self.b.state["open"]
        self.assertEqual(pos["lots"], 100)
        for n in ("put_short", "call_short"):
            bump(self.fx, pos["legs"][n]["symbol"], 0.2)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.positions(), [])
        self.assertTrue(all(o.get("reduce_only") for o in self.fx.orders[2:]))


class TestMarginReject(TestFixedLotsTakeProfit):
    mode = "live"

    def test_rejected_second_leg_unwinds_first(self):
        p, _ = self.b.plan()
        bad = p["legs"]["call_short"].product_id
        real = self.fx.place_order

        def po(**o):
            if o["product_id"] == bad and o["side"] == "sell":
                raise cd.DeltaAPIError("insufficient_margin")
            return real(**o)
        self.fx.place_order = po
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.positions(), [])


def give_candles(fx, rv24, rv_recent=None, recent_min=60):
    """Attach 1-min candles whose realised vol is rv24 (%), with the last `recent_min` minutes at rv_recent."""
    def candles(symbol, res, start, end):
        assert symbol == "BTCUSD" and res == "1m"
        n = int((end - start) // 60)
        out, px = [], 82000.0
        for i in range(n):
            recent = rv_recent is not None and i >= n - recent_min
            sig = (rv_recent if recent else rv24) / 100 / math.sqrt(525600)
            px *= math.exp(sig if i % 2 else -sig)
            out.append({"time": start + 60 * i, "close": str(px)})
        return out
    fx.candles = candles


class TestVolSwitch(Base):
    style = "strangle"

    def setUp(self):
        super().setUp()
        self.cfg.FIXED_LOTS, self.cfg.MAX_LOSS_USD, self.cfg.VOL_SWITCH = 100, 90.0, True
        self.cfg.TAKE_PROFIT = 0.75                     # ignored while the switch is on

    def test_realised_vol_reading(self):
        give_candles(self.fx, 33.0)
        self.assertAlmostEqual(self.b.realised_vol(1440), 33.0, delta=0.5)
        give_candles(self.fx, 30.0, rv_recent=90.0)
        self.assertAlmostEqual(self.b.realised_vol(60), 90.0, delta=2)

    def test_calm_night_holds_past_75pct(self):
        give_candles(self.fx, 30.0)
        self.b.step()
        pos = self.b.state["open"]
        self.assertEqual((pos["mode"], pos["tp"], pos["dip"]), ("calm", 0.0, True))
        for n in ("put_short", "call_short"):
            bump(self.fx, pos["legs"][n]["symbol"], 0.15)
        self.clock[0] += 2 * 3600
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])      # no take profit on a calm night
        self.clock[0] = EXPIRY + 11 * 60
        self.fx.pos.clear()                             # live: Delta has settled the legs
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertIn("settlement", self.rows()[-1]["note"])

    def test_calm_night_dipstick_exit(self):
        give_candles(self.fx, 30.0)
        self.b.step()
        give_candles(self.fx, 30.0, rv_recent=75.0)
        self.clock[0] += 30 * 60
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])      # dipstick only from 60 min after entry
        self.clock[0] += 31 * 60
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])      # first reading above: alert, wait 10 min
        self.assertIsNotNone(self.b.state["open"].get("dip_over_since"))
        self.clock[0] += 5 * 60
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])      # 5 min above: still waiting
        self.clock[0] += 5 * 60
        self.b.step()
        self.assertIsNone(self.b.state["open"])          # 10 min above: exit
        self.assertIn("vol exit", self.rows()[-1]["note"])
        self.assertIn("for 10 min", self.rows()[-1]["note"])

    def test_dipstick_spike_that_fades_is_held(self):
        give_candles(self.fx, 30.0)
        self.b.step()
        self.clock[0] += 61 * 60
        give_candles(self.fx, 30.0, rv_recent=75.0)
        self.b.step()                                   # alert starts
        self.clock[0] += 6 * 60
        give_candles(self.fx, 30.0, rv_recent=45.0)
        self.b.step()                                   # back below: wait resets
        self.assertIsNone(self.b.state["open"].get("dip_over_since"))
        self.clock[0] += 6 * 60
        give_candles(self.fx, 30.0, rv_recent=75.0)
        self.b.step()                                   # above again: a fresh 10-minute wait
        self.clock[0] += 6 * 60
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])      # only 6 min into the new wait
        self.clock[0] += 5 * 60
        self.b.step()
        self.assertIsNone(self.b.state["open"])

    def test_stop_still_works_during_the_wait(self):
        give_candles(self.fx, 30.0)
        self.b.step()
        pos = self.b.state["open"]
        self.clock[0] += 61 * 60
        give_candles(self.fx, 30.0, rv_recent=75.0)
        self.b.step()
        bump(self.fx, pos["legs"]["put_short"]["symbol"], 8)
        self.clock[0] += 60
        self.b.step()
        self.clock[0] += 10
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertIn("stop", self.rows()[-1]["note"].lower())

    def test_confirm_zero_exits_at_once(self):
        self.cfg.DIP_CONFIRM_MIN = 0
        give_candles(self.fx, 30.0)
        self.b.step()
        self.clock[0] += 61 * 60
        give_candles(self.fx, 30.0, rv_recent=75.0)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertIn("vol exit", self.rows()[-1]["note"])

    def test_calm_night_quiet_dipstick_stays(self):
        give_candles(self.fx, 30.0, rv_recent=45.0)
        self.b.step()
        self.clock[0] += 2 * 3600
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_volatile_night_takes_25(self):
        give_candles(self.fx, 50.0)
        self.b.step()
        pos = self.b.state["open"]
        self.assertEqual((pos["mode"], pos["tp"], pos["dip"]), ("volatile", 0.25, False))
        give_candles(self.fx, 50.0, rv_recent=120.0)     # no dipstick on volatile nights
        self.clock[0] += 2 * 3600
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])
        for n in ("put_short", "call_short"):
            bump(self.fx, pos["legs"][n]["symbol"], 0.70)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertIn("take profit", self.rows()[-1]["note"])

    def test_no_reading_counts_as_volatile(self):
        def broken(*a, **k):
            raise cd.DeltaAPIError("candles down")
        self.fx.candles = broken
        self.b.step()
        self.assertEqual(self.b.state["open"]["mode"], "volatile")

    def test_stop_still_applies_on_calm_night(self):
        give_candles(self.fx, 30.0)
        self.b.step()
        pos = self.b.state["open"]
        bump(self.fx, pos["legs"]["call_short"]["symbol"], 6)
        self.b.step()
        self.b.step()
        self.assertIn("stopped", self.rows()[-1]["note"])

    def test_switch_off_by_default(self):
        self.assertFalse(CondorConfig.VOL_SWITCH)


class TestVolSwitchLive(TestVolSwitch):
    mode = "live"


class TestStrangleLive(Base):
    mode = "live"
    style = "strangle"

    def test_live_sells_two_and_stop_buys_back(self):
        self.b.step()
        pos = self.b.state["open"]
        self.assertEqual([o["side"] for o in self.fx.orders], ["sell", "sell"])
        held = {p["product_symbol"]: p["size"] for p in self.fx.positions()}
        self.assertEqual(sorted(held.values()), [-pos["lots"], -pos["lots"]])
        bump(self.fx, pos["legs"]["put_short"]["symbol"], 8)
        self.b.step()
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.positions(), [])
        self.assertTrue(all(o.get("reduce_only") for o in self.fx.orders[2:]))




class TestSkipDays(Base):
    style = "strangle"

    def setUp(self):
        super().setUp()
        self.cfg.FIXED_LOTS, self.cfg.MAX_LOTS, self.cfg.MAX_LOSS_USD = 200, 200, 180.0
        self.cfg.SKIP_DAYS = "Thu"

    def test_wednesday_still_trades(self):                 # EVENING is Wed 21 Oct 2026
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_thursday_evening_skipped_all_window(self):
        thu = datetime(2026, 10, 22, 21, 31, tzinfo=IST).timestamp()
        self.fx = FakeDelta(sfx="231026")
        self.b = Condor(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)
        for k in range(0, 60, 5):
            self.clock[0] = thu + k * 60
            self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.orders, [])
        self.assertEqual(self.b.state["skipped_day"], "2026-10-22")

    def test_friday_trades_after_thursday_skip(self):
        self.fx = FakeDelta(sfx="241026")
        self.b = Condor(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)
        self.clock[0] = datetime(2026, 10, 23, 21, 40, tzinfo=IST).timestamp()
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_open_position_still_managed_on_thursday(self):
        self.b.step()                                       # Wed entry, expires Thu 17:30
        self.assertIsNotNone(self.b.state["open"])
        self.clock[0] = EXPIRY + 11 * 60                    # Thu after settlement wait
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(len(self.rows()), 1)

    def test_empty_setting_skips_nothing(self):
        self.cfg.SKIP_DAYS = ""
        self.assertFalse(self.b.skip_today())
        self.cfg.SKIP_DAYS = "Wed"
        self.assertTrue(self.b.skip_today())


class TestHotNightSkip(Base):
    style = "strangle"

    def setUp(self):
        super().setUp()
        self.cfg.FIXED_LOTS, self.cfg.MAX_LOTS, self.cfg.MAX_LOSS_USD, self.cfg.VOL_SWITCH = 200, 200, 180.0, True
        self.cfg.VOL_SKIP_RV = 50.0

    def test_hot_night_skipped_no_orders(self):
        give_candles(self.fx, 62.0)
        for k in range(0, 50, 5):
            self.clock[0] = EVENING + k * 60
            self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.orders, [])
        self.assertTrue(self.b.state["hot_check"]["skip"])
        self.assertEqual(self.b.state["skipped_day"], "2026-10-21")

    def test_volatile_but_below_limit_trades_with_take_profit(self):
        give_candles(self.fx, 45.0)
        self.b.step()
        pos = self.b.state["open"]
        self.assertIsNotNone(pos)
        self.assertEqual(pos["mode"], "volatile")

    def test_calm_night_trades(self):
        give_candles(self.fx, 30.0)
        self.b.step()
        self.assertEqual(self.b.state["open"]["mode"], "calm")

    def test_decision_kept_for_the_night(self):
        give_candles(self.fx, 62.0)
        self.b.step()
        give_candles(self.fx, 30.0)                         # cools inside the window: still no trade tonight
        self.clock[0] += 600
        self.b.step()
        self.assertIsNone(self.b.state["open"])

    def test_next_night_trades_if_cooled(self):
        give_candles(self.fx, 62.0)
        self.b.step()
        self.fx = FakeDelta(sfx="231026")
        give_candles(self.fx, 30.0)
        self.b = Condor(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)
        self.clock[0] = datetime(2026, 10, 22, 21, 40, tzinfo=IST).timestamp()
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_no_reading_still_trades(self):
        self.fx.candles = lambda *a: []
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_off_when_zero(self):
        self.cfg.VOL_SKIP_RV = 0.0
        give_candles(self.fx, 80.0)
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_open_position_still_managed(self):
        give_candles(self.fx, 30.0)
        self.b.step()
        give_candles(self.fx, 70.0)
        self.clock[0] = EXPIRY + 11 * 60
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(len(self.rows()), 1)


FED_HTML = """<p>Future Year: <a href="#45694">2027</a></p>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="42828">2026 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting" "=""><div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>January</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">27-28</div><div>Statement</div></div>
<div class="fomc-meeting--shaded row fomc-meeting" "=""><div class="fomc-meeting--shaded fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>September</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">15-16*</div></div>
<div class="row fomc-meeting" "=""><div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>October</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">27-28</div></div>
<div class="row fomc-meeting" "=""><div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>December</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">8-9*</div></div></div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="1">2025 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>August</strong></div>
<div class="fomc-meeting__date col-xs-4">22 (notation vote)</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>December</strong></div>
<div class="fomc-meeting__date col-xs-4">9-10*</div></div></div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="2">2024 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-xs-4">30-1</div></div></div>"""


class TestFedSkip(Base):
    style = "strangle"

    def setUp(self):
        super().setUp()
        self.cfg.FIXED_LOTS, self.cfg.MAX_LOTS, self.cfg.MAX_LOSS_USD, self.cfg.VOL_SWITCH = 200, 200, 180.0, True
        self.cfg.FED_SKIP = True
        self.calls = []
        self.html = FED_HTML

    def bot_at(self, when, sfx):
        self.fx = FakeDelta(sfx=sfx)
        give_candles(self.fx, 30.0)
        self.clock[0] = when.timestamp()
        self.b = Condor(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)

        def get(url):
            self.calls.append(url)
            if self.html is None:
                raise OSError("network down")
            return self.html
        self.b.http_get = get

    def test_parse_real_layout(self):
        d = cd.parse_fomc(FED_HTML)
        for x in ("2026-01-28", "2026-09-16", "2026-10-28", "2026-12-09", "2025-08-22", "2025-12-10", "2024-05-01"):
            self.assertIn(x, d)

    def test_winter_fed_night_skipped(self):            # decision 9 Dec 14:00 New York = 10 Dec 00:30 IST
        self.bot_at(datetime(2026, 12, 9, 21, 40, tzinfo=IST), "101226")
        for k in range(0, 40, 5):
            self.clock[0] += 300
            self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.orders, [])
        self.assertEqual(self.b.state["skipped_day"], "2026-12-09")
        dec, src = self.b.fed_in_trade()
        self.assertEqual(dec.strftime("%d %H:%M"), "10 00:30")
        self.assertIn("federalreserve.gov", src)

    def test_summer_fed_night_skipped(self):            # 16 Sep 14:00 EDT = 23:30 IST, same evening
        self.bot_at(datetime(2026, 9, 16, 21, 40, tzinfo=IST), "170926")
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.b.fed_in_trade()[0].strftime("%d %H:%M"), "16 23:30")

    def test_night_before_decision_trades(self):        # 8 Dec entry expires 9 Dec 17:30 IST, before the decision
        self.bot_at(datetime(2026, 12, 8, 21, 40, tzinfo=IST), "091226")
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_night_after_decision_trades(self):
        self.bot_at(datetime(2026, 12, 10, 21, 40, tzinfo=IST), "111226")
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_page_down_uses_built_in_list(self):
        self.html = None
        self.bot_at(datetime(2026, 12, 9, 21, 40, tzinfo=IST), "101226")
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertIn("built-in", self.b.fed_in_trade()[1])

    def test_page_read_once_and_cached(self):
        self.bot_at(datetime(2026, 12, 8, 21, 40, tzinfo=IST), "091226")
        self.b.fed_in_trade()
        self.b.fed_in_trade()
        self.clock[0] += 3600
        self.b.fed_in_trade()
        self.assertEqual(len(self.calls), 1)
        self.clock[0] += 13 * 3600
        self.b.fed_in_trade()
        self.assertEqual(len(self.calls), 2)

    def test_bad_page_keeps_cache(self):
        self.bot_at(datetime(2026, 12, 8, 21, 40, tzinfo=IST), "091226")
        self.b.fed_in_trade()
        self.html = "<html>redesigned page</html>"
        self.clock[0] += 13 * 3600
        dates, src = self.b.fomc_dates()
        self.assertIn("2026-12-09", dates)
        self.assertIn("federalreserve.gov", src)

    def test_off_by_default(self):
        self.cfg.FED_SKIP = False
        self.bot_at(datetime(2026, 12, 9, 21, 40, tzinfo=IST), "101226")
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])
        self.assertEqual(self.calls, [])

    def test_manual_skip_date(self):
        self.cfg.FED_SKIP = False
        self.cfg.SKIP_DATES = "2026-12-24, 2026-12-31"
        self.bot_at(datetime(2026, 12, 31, 21, 40, tzinfo=IST), "010127")
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.b.state["skipped_day"], "2026-12-31")


def give_vols(fx, rv1, rv6, rv24, calls=None):
    """1-min candles: last 60 min at rv1, the 300 min before at rv6, the rest at rv24 (approximate windows)."""
    def candles(symbol, res, start, end):
        if calls is not None:
            calls.append(end)
        n = int((end - start) // 60)
        out, px = [], 82000.0
        for i in range(n):
            back = n - i
            v = rv1 if back <= 60 else (rv6 if back <= 360 else rv24)
            sig = v / 100 / math.sqrt(525600)
            px *= math.exp(sig if i % 2 else -sig)
            out.append({"time": start + 60 * i, "close": str(px)})
        return out
    fx.candles = candles


class TestDampenedEntry(Base):
    style = "strangle"

    def setUp(self):
        super().setUp()
        self.cfg.FIXED_LOTS, self.cfg.MAX_LOTS, self.cfg.MAX_LOSS_USD, self.cfg.VOL_SWITCH = 200, 200, 180.0, True
        self.cfg.ENTRY_MODE, self.cfg.ENTRY_IST, self.cfg.SKIP_DAYS, self.cfg.VOL_SKIP_RV = "dampened", "22:30", "Thu", 50.0
        self.msgs = []
        self.b.notify = lambda m: self.msgs.append(m)
        self.calls = []

    def at(self, y, mo, d, h, mi):
        self.clock[0] = datetime(y, mo, d, h, mi, tzinfo=IST).timestamp()

    def test_vol_set_reads_three_windows(self):
        give_vols(self.fx, 20, 45, 30)
        self.at(2026, 10, 21, 22, 31)
        r1, r6, r24 = self.b.vol_set()
        self.assertAlmostEqual(r1, 20, delta=2)
        self.assertGreater(r6, 35)
        self.assertLess(r24, 40)

    def test_calm_at_2230_enters_at_once(self):
        give_vols(self.fx, 20, 20, 25)
        self.at(2026, 10, 21, 22, 31)
        self.b.step()
        pos = self.b.state["open"]
        self.assertIsNotNone(pos)
        self.assertEqual(pos["mode"], "calm")
        self.assertEqual(pos["expiry"], "2026-10-22 17:30")
        self.assertEqual(self.b.state["last_entry_date"], "2026-10-21")
        self.assertTrue(any("DAMPENED ENTRY" in m for m in self.msgs))

    def test_not_before_2230(self):
        give_vols(self.fx, 20, 20, 25)
        self.at(2026, 10, 21, 22, 20)
        self.b.step()
        self.assertIsNone(self.b.state["open"])

    def test_waits_then_enters_when_calm_in_the_morning(self):
        give_vols(self.fx, 45, 45, 38)
        self.at(2026, 10, 21, 22, 31)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertTrue(any("Waiting for calm" in m for m in self.msgs))
        self.at(2026, 10, 22, 3, 1)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        give_vols(self.fx, 22, 28, 38)
        self.at(2026, 10, 22, 3, 16)
        self.b.step()
        pos = self.b.state["open"]
        self.assertIsNotNone(pos)
        self.assertEqual(pos["expiry"], "2026-10-22 17:30")        # same morning's expiry
        self.assertEqual(self.b.state["last_entry_date"], "2026-10-21")
        self.assertEqual(sum("Waiting for calm" in m for m in self.msgs), 1)

    def test_checks_only_every_15_min(self):
        give_vols(self.fx, 45, 45, 38, self.calls)
        self.at(2026, 10, 21, 22, 31)
        for k in range(0, 14):
            self.clock[0] += 60
            self.b.step()
        self.assertLessEqual(len(self.calls), 2)

    def test_24h_above_40_not_calm(self):
        give_vols(self.fx, 20, 25, 46)
        self.at(2026, 10, 21, 22, 31)
        self.b.step()
        self.assertIsNone(self.b.state["open"])

    def test_never_calm_no_trade_and_told_once(self):
        give_vols(self.fx, 45, 45, 38)
        for h, mi in ((22, 31), (23, 46), (2, 1), (11, 16)):
            self.at(2026, 10, 21 if h >= 22 else 22, h, mi)
            self.b.step()
        self.assertEqual(self.fx.orders, [])
        self.at(2026, 10, 22, 11, 31)
        self.b.step()
        self.at(2026, 10, 22, 12, 0)
        self.b.step()
        told = [m for m in self.msgs if "never settled" in m]
        self.assertEqual(len(told), 1)
        self.assertIn("2026-10-21", told[0])
        self.assertIsNone(self.b.state["open"])

    def test_thursday_night_skipped_including_friday_morning(self):
        self.fx = self.b.c = FakeDelta(sfx="231026")
        give_vols(self.fx, 20, 20, 25)
        self.at(2026, 10, 22, 22, 31)
        self.b.step()
        self.at(2026, 10, 23, 3, 1)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.orders, [])
        self.assertEqual(sum("No trade tonight" in m for m in self.msgs), 1)

    def test_sunday_night_waits_for_monday_0500(self):
        self.fx = self.b.c = FakeDelta(sfx="261026")
        give_vols(self.fx, 20, 20, 25, self.calls)
        self.at(2026, 10, 25, 22, 31)                       # Sunday
        self.b.step()
        self.at(2026, 10, 26, 4, 50)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.calls, [])
        self.at(2026, 10, 26, 5, 1)
        self.b.step()
        pos = self.b.state["open"]
        self.assertIsNotNone(pos)
        self.assertEqual(pos["expiry"], "2026-10-26 17:30")
        self.assertEqual(self.b.state["last_entry_date"], "2026-10-25")

    def test_one_trade_per_night_after_stop(self):
        give_vols(self.fx, 20, 20, 25)
        self.at(2026, 10, 21, 22, 31)
        self.b.step()
        pos = self.b.state["open"]
        for n in ("put_short", "call_short"):
            bump(self.fx, pos["legs"][n]["symbol"], 8)
        self.at(2026, 10, 22, 1, 0)
        self.b.step()
        self.clock[0] += 10
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        n = len(self.rows())
        self.at(2026, 10, 22, 2, 1)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(len(self.rows()), n)

    def test_morning_entry_does_not_block_next_night(self):
        self.fx = self.b.c = FakeDelta(sfx="241026")
        give_vols(self.fx, 45, 45, 38)
        self.at(2026, 10, 23, 22, 31)                       # Friday night, not calm
        self.b.step()
        give_vols(self.fx, 20, 20, 30)
        self.at(2026, 10, 24, 4, 1)                         # Saturday morning: calm -> enter
        self.b.step()
        self.assertEqual(self.b.state["last_entry_date"], "2026-10-23")
        self.at(2026, 10, 24, 17, 41)                       # settles
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.fx = FakeDelta(sfx="251026")
        give_vols(self.fx, 20, 20, 30)
        self.b.c = self.fx
        self.at(2026, 10, 24, 22, 31)                       # Saturday night trades again
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])
        self.assertEqual(self.b.state["last_entry_date"], "2026-10-24")

    def test_fed_night_skipped_whole_night(self):
        self.fx = self.b.c = FakeDelta(sfx="101226")
        self.cfg.FED_SKIP = True
        self.b.http_get = lambda url: FED_HTML
        give_vols(self.fx, 20, 20, 25)
        self.at(2026, 12, 9, 22, 31)
        self.b.step()
        self.at(2026, 12, 10, 3, 1)                         # after the 00:30 decision, calm: still no trade
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.fx.orders, [])

    def test_fixed_mode_unchanged(self):
        self.cfg.ENTRY_MODE = "fixed"
        give_vols(self.fx, 45, 45, 38)
        self.at(2026, 10, 21, 22, 35)
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])


class TestDriftExit(Base):
    style = "strangle"

    def setUp(self):
        super().setUp()
        self.cfg.FIXED_LOTS, self.cfg.MAX_LOTS, self.cfg.MAX_LOSS_USD, self.cfg.VOL_SWITCH = 200, 200, 180.0, True
        self.cfg.DRIFT_EXIT_PCT = 2.0
        give_candles(self.fx, 30.0)
        self.b.step()
        self.pos = self.b.state["open"]

    def test_exit_when_btc_up_2pct(self):
        self.fx.spot = 82000 * 1.021
        self.clock[0] += 120
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertIn("drift exit", self.rows()[-1]["note"])

    def test_exit_when_btc_down_2pct(self):
        self.fx.spot = 82000 * 0.979
        self.clock[0] += 120
        self.b.step()
        self.assertIsNone(self.b.state["open"])

    def test_holds_below_limit(self):
        self.fx.spot = 82000 * 1.019
        self.clock[0] += 120
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_off_when_zero(self):
        self.cfg.DRIFT_EXIT_PCT = 0.0
        self.fx.spot = 82000 * 1.05
        self.clock[0] += 120
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_one_trade_per_night_after_drift_exit(self):
        self.fx.spot = 82000 * 1.021
        self.clock[0] += 120
        self.b.step()
        self.clock[0] += 300
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(len(self.rows()), 1)


if __name__ == "__main__":
    unittest.main()
