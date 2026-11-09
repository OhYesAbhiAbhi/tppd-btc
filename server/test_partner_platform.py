"""Tests for the partner vault and results (offline)."""
import csv
import os
import tempfile
import unittest
from datetime import date

import partner_results as pr
import partner_vault as pv

COLS = ["mode", "style", "entry_ist", "expiry", "lots", "put_short", "put_wing", "call_short", "call_wing",
        "spot_entry", "credit", "max_loss_usd", "settle_price", "payoff", "fees_usd", "net_usd",
        "net_pct_credit", "note"]


class TestVault(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.priv, cls.pub = pv.make_keys()

    def test_round_trip(self):
        env = pv.seal(self.pub, {"key": "K123", "secret": "S456"})
        self.assertNotIn("K123", str(env))
        self.assertEqual(pv.open_envelope(self.priv, env), {"key": "K123", "secret": "S456"})

    def test_wrong_vault_key_fails(self):
        other, _ = pv.make_keys()
        with self.assertRaises(Exception):
            pv.open_envelope(other, pv.seal(self.pub, {"key": "K", "secret": "S"}))

    def test_tampered_ciphertext_fails(self):
        env = pv.seal(self.pub, {"key": "K", "secret": "S"})
        env["ct"] = env["ct"][:-4] + ("AAAA" if not env["ct"].endswith("AAAA") else "BBBB")
        with self.assertRaises(Exception):
            pv.open_envelope(self.priv, env)

    def test_init_makes_private_file_600_and_keeps_existing(self):
        d = tempfile.mkdtemp()
        k, p = os.path.join(d, "v.key"), os.path.join(d, "v.pub")
        pv.init(k, p)
        self.assertEqual(oct(os.stat(k).st_mode & 0o777), "0o600")
        first = open(k, "rb").read()
        pv.init(k, p)
        self.assertEqual(open(k, "rb").read(), first)


class TestResults(unittest.TestCase):
    def write(self, rows):
        p = os.path.join(tempfile.mkdtemp(), "trades.csv")
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, COLS)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in COLS})
        return p

    def test_days_and_privacy(self):
        p = self.write([
            {"mode": "live", "entry_ist": "2026-11-06 22:40", "expiry": "2026-11-07 17:30", "lots": 50,
             "net_usd": "18.40", "note": "exchange settlement price", "put_short": "80800", "call_short": "83200"},
            {"mode": "paper", "entry_ist": "2026-11-07 23:10", "expiry": "2026-11-08 17:30", "lots": 50,
             "net_usd": "9.2", "note": "vol exit (60-min vol 66%) at 02:11"},
            {"mode": "live", "entry_ist": "2026-11-08 22:31", "expiry": "2026-11-09 17:30", "lots": 50,
             "net_usd": "6.1", "note": "closed by you at 09:12"},
            {"mode": "shadow", "entry_ist": "2026-11-08 22:31", "expiry": "2026-11-09 17:30", "lots": 50,
             "net_usd": "21.3", "note": "exchange settlement price"},
        ])
        ds = pr.days(p)
        self.assertEqual(list(ds), [date(2026, 11, 7), date(2026, 11, 8), date(2026, 11, 9)])
        m7 = pr.day_message(date(2026, 11, 7), ds[date(2026, 11, 7)], ds)
        self.assertIn("Live · 50 lots · +$18.40 (₹1,564)", m7)
        m8 = pr.day_message(date(2026, 11, 8), ds[date(2026, 11, 8)], ds)
        self.assertIn("Paper", m8)
        m9 = pr.day_message(date(2026, 11, 9), ds[date(2026, 11, 9)], ds)
        self.assertIn("You closed early: +$6.10", m9)
        self.assertIn("Bot's result if held: +$21.30", m9)
        self.assertIn("November so far: +$24.50 live (2 days)", m9)
        for m in (m7, m8, m9):
            for secret in ("80800", "83200", "vol", "settlement", "22:", "02:11", "stop", "drift"):
                self.assertNotIn(secret, m)

    def test_no_trade_and_month_totals(self):
        p = self.write([{"mode": "live", "entry_ist": "2026-11-10 22:40", "expiry": "2026-11-11 17:30",
                         "lots": 20, "net_usd": "-45", "note": "stopped at 03:10"}])
        ds = pr.days(p)
        self.assertEqual(list(ds), [date(2026, 11, 11)])
        self.assertEqual(pr.day_message(date(2026, 11, 12), None, ds), "Thu 12 Nov · No trade today")
        mt = pr.month_totals(ds, 2026, 11)
        self.assertEqual((mt["live_net"], mt["live_days"], mt["win_days"]), (-45.0, 1, 0))
        self.assertIn("−$45.00 (₹−3,825)", pr.day_message(date(2026, 11, 11), ds[date(2026, 11, 11)], ds))


if __name__ == "__main__":
    unittest.main()
