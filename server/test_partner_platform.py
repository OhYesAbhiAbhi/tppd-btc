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


class TestPortal(unittest.TestCase):
    def setUp(self):
        import partner_portal as pp
        self.pp = pp
        self.tmp = tempfile.mkdtemp()
        pp.ROOT = __import__("pathlib").Path(self.tmp)
        self.priv, self.pub = pv.make_keys()
        pubf = os.path.join(self.tmp, "vault.pub")
        open(pubf, "w").write(self.pub)
        pp.PUBKEY = __import__("pathlib").Path(pubf)
        self.st = pp.open_store()
        self.env = __import__("json").dumps(pv.seal(self.pub, {"key": "REALKEY99", "secret": "REALSECRET99"}))

    def form(self, **kw):
        f = {"c": self.st.get("invite"), "name": "Ravi Kumar", "contact": "98765", "pw": "pin123", "lots": "50",
             "envelope": self.env, "ack": "yes"}
        f.update(kw)
        return {k: [v] for k, v in f.items()}

    def test_signup_stores_ciphertext_only(self):
        ok, token = self.pp.signup(self.st, self.form())
        self.assertTrue(ok, token)
        p = self.st.partner(token=token)
        self.assertEqual((p["status"], p["lots"], p["slug"]), ("pending", 50, "ravi-kumar"))
        dump = str(self.st.q("SELECT * FROM partners"))
        self.assertNotIn("REALKEY99", dump)
        self.assertNotIn("pin123", dump)
        self.assertEqual(pv.open_envelope(self.priv, __import__("json").loads(p["envelope"]))["key"], "REALKEY99")

    def test_signup_checks(self):
        self.assertFalse(self.pp.signup(self.st, self.form(c="wrong"))[0])
        self.assertFalse(self.pp.signup(self.st, self.form(envelope="plain key"))[0])
        self.assertFalse(self.pp.signup(self.st, self.form(ack=""))[0])
        self.assertFalse(self.pp.signup(self.st, self.form(pw="12"))[0])

    def active(self):
        ok, token = self.pp.signup(self.st, self.form())
        msg = self.pp.approve(self.st, "ravi-kumar", activate=lambda s: (True, ""))
        self.assertIn("approved", msg)
        return self.st.partner(token=token)

    def test_approve_writes_control_and_actions(self):
        p = self.active()
        c = self.pp.control(p["slug"])
        self.assertEqual((c["lots"], c["live"]), (50, True))
        f = lambda **kw: {k: [v] for k, v in kw.items()}
        self.pp.partner_action(self.st, p, f(a="lots", lots="80"))
        self.pp.partner_action(self.st, p, f(a="off"))
        d = (self.pp.today() + __import__("datetime").timedelta(days=3)).isoformat()
        self.pp.partner_action(self.st, p, f(a="paperday", day=d))
        c = self.pp.control(p["slug"])
        self.assertEqual((c["lots"], c["live"], c["paper_dates"]), (80, False, [d]))
        self.assertIn("no open trade", self.pp.partner_action(self.st, p, f(a="close")))
        os.makedirs(os.path.join(self.tmp, p["slug"], "data"), exist_ok=True)
        open(os.path.join(self.tmp, p["slug"], "data", "state.json"), "w").write('{"open": {"lots": 80}}')
        self.pp.partner_action(self.st, p, f(a="close"))
        self.assertTrue(self.pp.control(p["slug"])["close_now"])

    def test_approval_refused_without_vault_helper(self):
        self.pp.signup(self.st, self.form())
        self.assertIn("Could not start", self.pp.approve(self.st, "ravi-kumar", activate=lambda s: (False, "no helper")))
        self.assertEqual(self.st.partner(slug="ravi-kumar")["status"], "pending")

    def test_revoke_deletes_ciphertext(self):
        p = self.active()
        self.pp.revoke(self.st, p["slug"], deactivate=lambda s: None)
        q = self.st.partner(slug=p["slug"])
        self.assertEqual((q["status"], q["envelope"]), ("revoked", None))

    def test_partner_page_shows_results_not_details(self):
        p = self.active()
        d = os.path.join(self.tmp, p["slug"], "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "trades.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, COLS)
            w.writeheader()
            w.writerow({c: "" for c in COLS} | {"mode": "live", "entry_ist": "2026-11-06 22:40", "expiry": "2026-11-07 17:30",
                        "lots": 50, "net_usd": "18.4", "put_short": "80800", "call_short": "83200", "note": "exchange settlement price"})
        html_ = self.pp.partner_page(self.st, p, 2026, 11)
        self.assertIn("+$18.40", html_)
        for s in ("80800", "83200", "settlement", "22:40", "REALKEY"):
            self.assertNotIn(s, html_)
        self.assertIn("Ravi Kumar", self.pp.master_page(self.st))

    def test_join_page_has_public_key_only(self):
        h = self.pp.join_page(self.st, self.st.get("invite"))
        self.assertIn(self.pub[:40], h)
        self.assertIn("RSA-OAEP", h)
        self.assertIn("3.106.111.8", h)
