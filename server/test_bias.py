"""Offline tests for the option-flow bias trader (no network, no keys)."""
import csv
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import bias_trader as bt
from condor import IST

WED = datetime(2026, 11, 11, 22, 31, tzinfo=IST).timestamp()
THU = datetime(2026, 11, 12, 22, 31, tzinfo=IST).timestamp()


class FakeFut:
    def __init__(self, px=72000.0):
        self.px, self.orders, self.cancels, self.held = px, [], [], 0

    def ticker(self, symbol):
        assert symbol == "BTCUSD"
        return {"mark_price": str(self.px)}

    def product(self, symbol):
        return {"id": 27}

    def candles(self, symbol, res, start, end):
        step = {"1m": 60, "15m": 900, "1h": 3600}[res]
        n = int((end - start) // step)
        return [{"time": start + i * step, "open": self.px, "high": self.px, "low": self.px, "close": self.px}
                for i in range(n)]

    def place_order(self, **o):
        self.orders.append(o)
        if o.get("stop_order_type"):
            return {"id": 900 + len(self.orders), "size": o["size"], "unfilled_size": o["size"]}
        self.held += (o["size"] if o["side"] == "buy" else -o["size"])
        return {"id": len(self.orders), "size": o["size"], "unfilled_size": 0, "average_fill_price": str(self.px)}

    def cancel_order(self, oid, pid):
        self.cancels.append(oid)

    def positions(self):
        return [{"product_id": 27, "size": self.held}] if self.held else []


class Base(unittest.TestCase):
    mode = "live"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = type("C", (bt.BiasConfig,), {"DATA_DIR": self.tmp, "MODE": self.mode, "TELEGRAM_BOT_TOKEN": "",
                                                "LOTS": 50, "CHECK_SECONDS": 0})
        self.clock = [THU]
        self.fx = FakeFut()
        self.msgs = []
        self.b = self.make()

    def make(self):
        b = bt.BiasTrader(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)
        b.notify = lambda m: self.msgs.append(m)
        self.set(b)
        return b

    def set(self, b, ps=0.81, st=-1, crowd=3.1, taker=0.512, vols=(58, 65, 45), oi=0.0, rsi=40.0):
        self.r = dict(ps=ps, st=st, crowd=crowd, taker=taker, vols=vols, oi=oi, rsi=rsi)
        b.deribit_put_share = lambda: (self.r["ps"], 1800.0) if self.r["ps"] is not None else (_ for _ in ()).throw(OSError("down"))
        b.st_dir = lambda: self.r["st"]
        b.crowd = lambda: self.r["crowd"]
        b.taker_share = lambda: self.r["taker"]
        b.vols = lambda: self.r["vols"]
        b.oi_change_1h = lambda: self.r["oi"]
        b.rsi15 = lambda: self.r["rsi"]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def rows(self):
        with open(f"{self.tmp}/trades.csv") as f:
            return list(csv.DictReader(f))

    def entry_orders(self):
        return [o for o in self.fx.orders if not o.get("stop_order_type") and not o.get("reduce_only")]


class TestEntry(Base):
    def test_thursday_short_goes_live_with_exchange_stop(self):
        self.b.step()
        pos = self.b.state["open"][0]
        self.assertEqual((pos["mode"], pos["side"], pos["lots"]), ("live", -1, 50))
        self.assertEqual(self.entry_orders()[0]["side"], "sell")
        stop = [o for o in self.fx.orders if o.get("stop_order_type")][0]
        self.assertEqual(stop["side"], "buy")
        self.assertTrue(stop["reduce_only"])
        self.assertGreater(float(stop["stop_price"]), 72000)
        self.assertIn("LIVE SHORT", self.msgs[-1])

    def test_thursday_crowd_fail_is_paper_only(self):
        self.r["crowd"] = 1.1
        self.b.step()
        self.assertEqual(self.b.state["open"][0]["mode"], "paper")
        self.assertEqual(self.fx.orders, [])

    def test_crowd_unreadable_is_paper_only(self):
        self.r["crowd"] = None
        self.b.step()
        self.assertEqual(self.b.state["open"][0]["mode"], "paper")
        self.assertEqual(self.fx.orders, [])

    def test_other_night_signal_is_paper_even_in_live_mode(self):
        self.clock[0] = WED
        self.b.step()
        self.assertEqual(self.b.state["open"][0]["mode"], "paper")
        self.assertEqual(self.fx.orders, [])

    def test_calm_other_night_left_to_strangle(self):
        self.clock[0] = WED
        self.r["vols"] = (20, 25, 35)
        self.b.step()
        self.assertEqual(self.b.state["open"], [])

    def test_calm_thursday_still_trades(self):
        self.r["vols"] = (20, 25, 35)
        self.b.step()
        self.assertEqual(self.b.state["open"][0]["mode"], "live")

    def test_no_signal_thursday_notice(self):
        self.r["ps"] = 0.60
        self.b.step()
        self.assertEqual(self.b.state["open"], [])
        self.assertIn("no bias trade", self.msgs[-1])

    def test_supertrend_must_agree(self):
        self.r["st"] = 1
        self.b.step()
        self.assertEqual(self.b.state["open"], [])

    def test_long_signal(self):
        self.r.update(ps=0.2, st=1, taker=0.49)
        self.b.step()
        pos = self.b.state["open"][0]
        self.assertEqual(pos["side"], 1)
        self.assertEqual(self.entry_orders()[0]["side"], "buy")

    def test_filters_recorded(self):
        self.r["taker"] = 0.53                                   # buyers 53%: against a short, so the move is not spent
        self.b.step()
        pos = self.b.state["open"][0]
        self.assertTrue(pos["not_spent"])
        self.assertAlmostEqual(pos["spot_flow"], -3.0, places=3)

    def test_once_per_night(self):
        self.b.step()
        self.clock[0] += 120
        self.b.step()
        self.assertEqual(len(self.b.state["open"]), 1)
        self.assertEqual(len(self.entry_orders()), 1)

    def test_outside_window_nothing(self):
        self.clock[0] = datetime(2026, 11, 12, 21, 0, tzinfo=IST).timestamp()
        self.b.step()
        self.assertEqual(self.b.state["open"], [])

    def test_option_flow_down_no_trade(self):
        self.r["ps"] = None
        self.b.step()
        self.assertEqual(self.b.state["open"], [])

    def test_vol_scaled_levels(self):
        self.b.step()
        pos = self.b.state["open"][0]
        self.assertAlmostEqual(pos["tp_pct"] / pos["sl_pct"], 2.0, places=2)
        self.assertGreater(pos["sl_pct"], 0.3)


class TestExit(Base):
    def open_short(self):
        self.b.step()
        return self.b.state["open"][0]

    def test_target(self):
        pos = self.open_short()
        self.fx.px = 72000 * (1 - pos["tp_pct"] / 100 - 0.001)
        self.clock[0] += 600
        self.b.step()
        self.assertEqual(self.b.state["open"], [])
        row = self.rows()[0]
        self.assertEqual(row["reason"], "target")
        self.assertGreater(float(row["net_usd"]), 0)
        close = [o for o in self.fx.orders if o.get("reduce_only") and not o.get("stop_order_type")][0]
        self.assertEqual(close["side"], "buy")
        self.assertEqual(len(self.fx.cancels), 1)

    def test_stop(self):
        pos = self.open_short()
        self.fx.px = 72000 * (1 + pos["sl_pct"] / 100 + 0.001)
        self.clock[0] += 600
        self.b.step()
        self.assertEqual(self.rows()[0]["reason"], "stop")
        self.assertLess(float(self.rows()[0]["net_usd"]), 0)

    def test_time_exit(self):
        self.open_short()
        self.clock[0] = datetime(2026, 11, 13, 17, 30, 5, tzinfo=IST).timestamp()
        self.b.step()
        self.assertEqual(self.rows()[0]["reason"], "time (17:30)")

    def test_rsi_return_exit(self):
        self.open_short()
        self.r["rsi"] = 22
        self.clock[0] += 900
        self.b.step()
        self.assertEqual(self.b.state["open"][0]["rsi_ext"], True)
        self.r["rsi"] = 33
        self.clock[0] += 900
        self.b.step()
        self.assertIn("RSI", self.rows()[0]["reason"])

    def test_oi_flush_exit(self):
        pos = self.open_short()
        self.r["oi"] = -2.0
        self.fx.px = 72000 * 0.991                               # 0.9% our way, below the target
        self.assertLess(0.9, pos["tp_pct"])
        orig = self.fx.candles
        self.fx.candles = lambda s, res, a, b: [{"close": 72000.0}] + orig(s, res, a, b)[1:]
        self.clock[0] += 900
        self.b.step()
        self.assertIn("OI flush", self.rows()[0]["reason"])

    def test_exchange_stop_already_filled(self):
        pos = self.open_short()
        self.fx.held = 0                                         # Delta's stop order closed it
        self.fx.px = 72000 * (1 + pos["sl_pct"] / 100 + 0.002)
        self.clock[0] += 600
        n = len(self.fx.orders)
        self.b.step()
        self.assertEqual(len(self.fx.orders), n)
        self.assertIn("already closed", self.rows()[0]["reason"])

    def test_restart_keeps_position(self):
        pos = self.open_short()
        b2 = self.make()
        self.fx.px = 72000 * (1 - pos["tp_pct"] / 100 - 0.001)
        self.clock[0] += 600
        b2.step()
        self.assertEqual(b2.state["open"], [])
        self.assertEqual(len(self.rows()), 1)


class TestPaperMode(Base):
    mode = "paper"

    def test_paper_mode_never_orders(self):
        self.b.step()
        self.assertEqual(self.b.state["open"][0]["mode"], "paper")
        pos = self.b.state["open"][0]
        self.fx.px = 72000 * (1 - pos["tp_pct"] / 100 - 0.001)
        self.clock[0] += 600
        self.b.step()
        self.assertEqual(self.fx.orders, [])
        self.assertEqual(self.rows()[0]["mode"], "paper")


class TestHelpers(unittest.TestCase):
    def test_put_share_filters_near_dated_near_money(self):
        now = datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc).timestamp()
        tr = [{"instrument_name": "BTC-13NOV26-71000-P", "index_price": 71700, "amount": 3},
              {"instrument_name": "BTC-13NOV26-72000-C", "index_price": 71700, "amount": 1},
              {"instrument_name": "BTC-27NOV26-70000-P", "index_price": 71700, "amount": 50},   # too far out
              {"instrument_name": "BTC-13NOV26-60000-P", "index_price": 71700, "amount": 50}]   # too far OTM
        ps, vol = bt.put_share(tr, now)
        self.assertAlmostEqual(ps, 0.75)
        self.assertEqual(vol, 4)

    def test_supertrend_direction(self):
        up = [100 + i for i in range(60)]
        self.assertEqual(bt.supertrend([x + 1 for x in up], [x - 1 for x in up], up), 1)
        dn = [200 - i for i in range(60)]
        self.assertEqual(bt.supertrend([x + 1 for x in dn], [x - 1 for x in dn], dn), -1)

    def test_rsi_extremes(self):
        self.assertGreater(bt.rsi([100 + i for i in range(40)]), 90)
        self.assertLess(bt.rsi([200 - i for i in range(40)]), 10)


if __name__ == "__main__":
    unittest.main()
