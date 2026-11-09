"""Offline tests for the Thursday paper tracker (no network, no keys, no orders)."""
import csv
import shutil
import tempfile
import unittest
from datetime import datetime

from condor import IST
from test_condor import FakeDelta
from thursday_paper import ThursdayConfig, ThursdayPaper

THU = datetime(2026, 10, 22, 21, 40, tzinfo=IST).timestamp()      # Thursday, inside 21:30 + 60 min
EXPIRY = datetime(2026, 10, 23, 17, 30, tzinfo=IST).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = type("T", (ThursdayConfig,), {"DATA_DIR": self.tmp, "TELEGRAM_BOT_TOKEN": "", "LOTS": 100,
                                                 "CHECK_SECONDS": 0})
        self.clock = [THU]
        self.fx = FakeDelta(sfx="231026")
        self.b = ThursdayPaper(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def rows(self):
        with open(f"{self.tmp}/thursday.csv") as f:
            return list(csv.DictReader(f))

    def set_bid(self, sym, bid):
        c = next(c for c in self.fx.chain if c["symbol"] == sym)
        c["quotes"]["best_bid"] = str(bid)


class TestThursday(Base):
    def test_buys_three_on_thursday_no_orders(self):
        self.b.step()
        P = self.b.state["paper"]
        self.assertEqual(set(P), {"straddle_hold", "strangle30_hold", "straddle_2x"})
        self.assertEqual(self.fx.orders, [])
        a = P["straddle_hold"]["legs"]
        self.assertEqual(a["put"]["strike"], a["call"]["strike"])
        self.assertEqual(a["put"]["strike"], 82000)
        b = P["strangle30_hold"]["legs"]
        self.assertLess(b["put"]["strike"], 82000)
        self.assertGreater(b["call"]["strike"], 82000)
        self.assertEqual(P["straddle_hold"]["expiry"], "2026-10-23 17:30")

    def test_not_on_wednesday(self):
        self.clock[0] = datetime(2026, 10, 21, 21, 40, tzinfo=IST).timestamp()
        self.b.step()
        self.assertEqual(self.b.state["paper"], {})

    def test_once_per_thursday(self):
        self.b.step()
        self.clock[0] = EXPIRY + 11 * 60
        self.fx.settlement = 82000.0
        self.b.step()
        self.assertEqual(self.b.state["paper"], {})
        self.clock[0] = THU + 600                            # same Thursday again (e.g. restart)
        self.b.step()
        self.assertEqual(self.b.state["paper"], {})

    def test_2x_sells_only_strategy_c(self):
        self.b.step()
        sym = self.b.state["paper"]["straddle_2x"]["legs"]["call"]["symbol"]
        cost = self.b.state["paper"]["straddle_2x"]["cost"]
        self.clock[0] += 3600
        self.set_bid(sym, round(cost * 2.3, 1))               # call alone now worth >2x the straddle
        self.b.step()
        P = self.b.state["paper"]
        self.assertNotIn("straddle_2x", P)
        self.assertIn("straddle_hold", P)
        r = self.rows()
        self.assertEqual(r[0]["strategy"], "straddle_2x")
        self.assertGreater(float(r[0]["net_usd"]), 0)
        self.assertTrue(r[0]["note"].startswith("sold at"))

    def test_settles_hold_positions_at_exchange_price(self):
        self.b.step()
        self.clock[0] = EXPIRY + 11 * 60
        self.fx.settlement = 84000.0                          # up 2,000
        self.b.step()
        r = {x["strategy"]: x for x in self.rows()}
        self.assertEqual(len(r), 3)
        self.assertEqual(float(r["straddle_hold"]["settle_price"]), 84000.0)
        self.assertGreater(float(r["straddle_hold"]["net_usd"]), 0)         # 2,000 intrinsic beats cost
        self.assertEqual(float(r["straddle_hold"]["exit_value"]), 2000.0)

    def test_quiet_night_loses_premium(self):
        self.b.step()
        cost = {n: p["cost"] + p["fees_in"] for n, p in self.b.state["paper"].items()}
        self.clock[0] = EXPIRY + 11 * 60
        self.fx.settlement = 82000.0                          # pinned at the ATM strike
        self.b.step()
        r = {x["strategy"]: x for x in self.rows()}
        self.assertAlmostEqual(float(r["straddle_hold"]["net_usd"]), -cost["straddle_hold"] * 0.1, places=3)
        self.assertLess(float(r["strangle30_hold"]["net_usd"]), 0)

    def test_restart_keeps_positions(self):
        self.b.step()
        b2 = ThursdayPaper(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)
        self.assertEqual(set(b2.state["paper"]), {"straddle_hold", "strangle30_hold", "straddle_2x"})

    def test_restart_after_results_keeps_file_intact(self):
        self.b.step()
        self.clock[0] = EXPIRY + 11 * 60
        self.fx.settlement = 82000.0
        self.b.step()
        before = open(f"{self.tmp}/thursday.csv").read()
        ThursdayPaper(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)
        self.assertEqual(open(f"{self.tmp}/thursday.csv").read(), before)
        self.assertIn("Paper totals", self.b.summary_text())

    def test_best_multiple_tracked(self):
        self.b.step()
        sym = self.b.state["paper"]["straddle_hold"]["legs"]["put"]["symbol"]
        self.clock[0] += 600
        self.set_bid(sym, 1200.0)
        self.b.step()
        self.assertGreater(self.b.state["paper"]["straddle_hold"]["best"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
