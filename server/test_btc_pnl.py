"""Offline tests for the BTC P&L page."""
import csv
import importlib
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from datetime import date, datetime
from zoneinfo import ZoneInfo

COLS = ["mode", "style", "entry_ist", "expiry", "lots", "put_short", "put_wing", "call_short", "call_wing",
        "spot_entry", "credit", "max_loss_usd", "settle_price", "payoff", "fees_usd", "net_usd", "net_pct_credit", "note"]


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        live = os.path.join(cls.d, "live.csv")
        with open(live, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            rows = [("live", "2026-10-25 21:31", "2026-10-26 17:30", "1.5", "vol exit (60-min vol 60%) at 05:52"),
                    ("live", "2026-10-26 21:31", "2026-10-27 17:30", "20.5", "exchange settlement price"),
                    ("live", "2026-10-29 21:31", "2026-10-30 17:30", "-195.0", "stopped at 02:11"),
                    ("live", "2026-10-30 21:31", "2026-10-31 17:30", "-3.0", "vol exit (61%) at 22:52"),
                    ("paper", "2026-10-20 21:31", "2026-10-21 17:30", "9.9", "exchange settlement price")]
            for mode, ent, exp, net, note in rows:
                w.writerow([mode, "strangle", ent, exp, 200] + [""] * 10 + [net, "", note])
        thu = os.path.join(cls.d, "thu.csv")
        with open(thu, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "entry_ist", "expiry", "lots", "put", "call", "spot_entry", "cost", "fees_in",
                        "exit_ist", "exit_value", "settle_price", "fees_out", "net_usd", "net_pct_cost", "note"])
            w.writerow(["straddle_hold", "2026-11-05 22:31", "2026-11-06 17:30", 100, 76500, 76500, 76600, 1700, 1,
                        "2026-11-06 17:40", 0, 76500, 0, "-170.5", "-1", "settled"])
        os.environ.update(BTC_PNL_LIVE=live, BTC_PNL_THU=thu, BTC_PNL_REENTRY=os.path.join(cls.d, "none.csv"), BTC_PNL_MANUAL=os.path.join(cls.d, "manual", "trades.csv"),
                          BTC_PNL_STATE=os.path.join(cls.d, "none.json"), BTC_PNL_KEY="secretkey123", BTC_PNL_PORT="18095")
        import btc_pnl
        cls.m = importlib.reload(btc_pnl)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def test_close_day(self):
        cd = self.m.close_day
        self.assertEqual(cd({"entry_ist": "2026-10-29 21:31", "expiry": "2026-10-30 17:30", "note": "stopped at 02:11"}),
                         date(2026, 10, 30))
        self.assertEqual(cd({"entry_ist": "2026-10-30 21:31", "expiry": "2026-10-31 17:30", "note": "vol exit at 22:52"}),
                         date(2026, 10, 30))
        self.assertEqual(cd({"entry_ist": "2026-10-26 21:31", "expiry": "2026-10-27 17:30", "note": "settled"}),
                         date(2026, 10, 27))
        self.assertEqual(cd({"entry_ist": "2026-11-05 22:31", "expiry": "2026-11-06 17:30", "exit_ist": "2026-11-06 17:40"}),
                         date(2026, 11, 6))

    def test_month_totals_live_only(self):
        now = datetime(2026, 11, 3, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        h = self.m.page((2026, 10), "secretkey123", now)
        self.assertIn("BTC P&amp;L · October 2026", h)
        self.assertIn("−$176.00", h)                          # 1.5 + 20.5 - 195 - 3 (paper 9.9 not counted)
        self.assertIn('<div class="v">3</div>', h)            # 3 live trading days (two trades closed 30 Oct)
        self.assertIn('<div class="v">2/3</div>', h)
        self.assertIn('<td class="r">2/4</td>', h)
        self.assertIn("BTC strangle (paper)", h)
        self.assertIn("paper", h)

    def test_manual_trade_counted(self):
        m = self.m
        m.add_manual("2026-12-03 09:15", "2026-12-03 17:30", 200, 23.4, "0.20 strangle; 78000P/80000C")
        m.add_manual("2026-12-04 09:20", "2026-12-04 17:30", 100, -12.5, "test", closed="2026-12-04 13:05")
        with self.assertRaises(ValueError):
            m.add_manual("bad", "2026-12-04 17:30", 100, 1, "x")
        now = datetime(2026, 12, 5, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        h = m.page((2026, 12), "secretkey123", now)
        self.assertIn("Manual trades", h)
        self.assertIn("+$10.90", h)                           # 23.4 - 12.5, counted in the month total
        self.assertIn('x200 · in 09:15 · <i class="tp">Full</i> 17:30', h)
        self.assertIn("Closed</i> 13:05", h)
        self.assertEqual(len(m.manual_rows()), 3)             # header + 2
        self.assertIn("held to expiry", m.manual_rows()[1][-1])

    def test_trade_details(self):
        now = datetime(2026, 11, 3, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        h = self.m.page((2026, 10), "secretkey123", now)
        self.assertIn('x200 · in 29 Oct 21:31 · <i class="st">Stop</i> 02:11', h)
        self.assertIn('x200 · in 21:31 · <i class="">Vol exit</i> 22:52', h)          # exit on the entry day
        self.assertIn('x200 · in 26 Oct 21:31 · <i class="tp">Full</i> 17:30', h)
        self.assertIn("<td>Full 1 · Vol exit 2 · Stop 1</td>", h)
        tag = self.m.exit_tag
        self.assertEqual(tag({"note": "take profit at 05:12"}), ("25% TP", "05:12"))
        self.assertEqual(tag({"note": "sold at 2.05x at 03:40"}), ("Sold 2.05x", "03:40"))
        self.assertEqual(tag({"note": "settled (exchange settlement)", "expiry": "2026-11-06 17:30"}), ("Full", "17:30"))

    def test_inr_toggle(self):
        now = datetime(2026, 11, 3, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        h = self.m.page((2026, 10), "secretkey123", now, cur="inr")
        self.assertIn("−₹14,960", h)                          # -176 x 85 as the main figure
        self.assertIn('<div class="x">−$176.00</div>', h)     # USD underneath
        self.assertIn("−₹16,575", h)                          # 30 Oct stop -195 x 85
        self.assertIn("m=2026-09&amp;cur=inr", h.replace("key=secretkey123&amp;cur=inr&amp;m=2026-09", "m=2026-09&amp;cur=inr"))
        self.assertIn('class="on" href="?key=secretkey123&amp;m=2026-10&amp;cur=inr">₹', h)
        u = self.m.page((2026, 10), "secretkey123", now)
        self.assertIn('class="on" href="?key=secretkey123&amp;m=2026-10">$', u)
        self.assertIn('<div class="x">−₹14,960</div>', u)

    def test_thursday_paper_greyed(self):
        now = datetime(2026, 11, 7, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        h = self.m.page((2026, 11), "secretkey123", now)
        self.assertIn("Thu A straddle hold (paper)", h)
        self.assertIn('class="s paper"', h)
        self.assertIn('<div class="v">0</div>', h)            # no live days in Nov in the sample

    def test_server_key(self):
        srv = self.m.ThreadingHTTPServer(("127.0.0.1", 18095), self.m.H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen("http://127.0.0.1:18095/calendar?key=wrong")
            self.assertEqual(e.exception.code, 403)
            r = urllib.request.urlopen("http://127.0.0.1:18095/calendar?key=secretkey123&m=2026-10")
            self.assertEqual(r.status, 200)
            self.assertIn("October 2026", r.read().decode())
            r = urllib.request.urlopen("http://127.0.0.1:18095/calendar?key=secretkey123&m=bad")
            self.assertEqual(r.status, 200)
            r = urllib.request.urlopen("http://127.0.0.1:18095/calendar?key=secretkey123&m=2026-10&cur=inr")
            self.assertIn("−₹14,960", r.read().decode())
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=1)
