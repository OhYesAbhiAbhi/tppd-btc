"""Offline tests for the partner controls in condor.py (no network, no keys).

Control file, per-trade stop, paper nights, auto-paper, Close my trade now (+ shadow result),
alerts off, staggered entry, systemd credentials. The owner's bot (no control file) must be unchanged."""
import json
import os
import tempfile
import unittest
from unittest import mock

import condor as cd
from test_condor import EXPIRY, Base, FakeDelta, bump, give_candles


class PartnerBase(Base):
    style = "strangle"
    mode = "live"

    def setUp(self):
        super().setUp()
        self.cfg.FIXED_LOTS, self.cfg.MAX_LOTS, self.cfg.MAX_LOSS_USD, self.cfg.WARN_USD = 200, 5000, 180.0, 100.0
        self.ctl_path = os.path.join(self.tmp, "control.json")
        self.cfg.CONTROL_FILE = self.ctl_path
        self.cfg.ALERTS = "off"

    def ctl(self, **c):
        with open(self.ctl_path, "w") as f:
            json.dump(c, f)

    def enter(self):
        self.b.step()
        return self.b.state["open"]


class TestOwnerUnchanged(Base):
    style = "strangle"
    mode = "live"

    def setUp(self):
        super().setUp()
        self.cfg.FIXED_LOTS, self.cfg.MAX_LOTS, self.cfg.MAX_LOSS_USD, self.cfg.WARN_USD = 200, 200, 180.0, 100.0

    def test_no_control_file_trades_live_with_own_stop(self):
        self.b.step()
        pos = self.b.state["open"]
        self.assertEqual((pos["trade_mode"], pos["lots"], pos["stop_usd"], pos["warn_usd"]), ("live", 200, 180.0, 100.0))
        self.assertTrue(self.fx.orders)

    def test_live_entry_alert_keeps_the_dampened_reason(self):
        self.cfg.VOL_SWITCH, self.cfg.ENTRY_MODE = True, "dampened"
        give_candles(self.fx, 20.0)
        said = []
        self.b.notify = said.append
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])
        entry = [m for m in said if "STRANGLE x200" in m][0]
        self.assertTrue(entry.startswith("DAMPENED ENTRY"), entry[:80])
        self.assertNotIn("margin in use", entry)

    def test_defaults_are_off(self):
        C = cd.CondorConfig
        self.assertEqual((C.CONTROL_FILE, C.STOP_PER_LOT, C.AUTO_PAPER, C.ALERTS, C.ENTRY_DELAY_SEC),
                         ("", 0.0, False, "full", 0))

    def test_old_open_position_without_new_fields_still_stops(self):
        self.b.step()
        pos = self.b.state["open"]
        for k in ("stop_usd", "warn_usd", "trade_mode", "paper_reason"):
            pos.pop(k)
        bump(self.fx, pos["legs"]["call_short"]["symbol"], 8)
        self.b.step()
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.rows()[-1]["mode"], "live")


class TestLots(PartnerBase):
    def test_control_lots_size_the_trade_and_scale_the_stop(self):
        self.ctl(lots=50, live=True)
        pos = self.enter()
        self.assertEqual((pos["lots"], pos["stop_usd"], pos["warn_usd"]), (50, 45.0, 25.0))   # $0.90 / $0.50 a lot

    def test_stop_per_lot_setting(self):
        self.cfg.STOP_PER_LOT = 1.0
        self.ctl(lots=40)
        self.assertEqual(self.enter()["stop_usd"], 40.0)

    def test_lot_change_during_open_trade_keeps_its_stop(self):
        self.ctl(lots=100)
        pos = self.enter()
        self.assertEqual(pos["stop_usd"], 90.0)
        self.ctl(lots=300)                                  # would be a $270 stop for a new trade
        units = pos["lots"] * pos["cv"]
        put_ask = float(self.fx._c(pos["legs"]["put_short"]["symbol"])["quotes"]["best_ask"])
        need = (100 / units + pos["credit"] + pos["fees_in"]) - put_ask      # an open loss of about $100
        self.fx._c(pos["legs"]["call_short"]["symbol"])["quotes"]["best_ask"] = str(round(need, 1))
        self.b.step()
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        r = self.rows()[-1]
        self.assertIn("stopped", r["note"])
        self.assertEqual((r["lots"], r["max_loss_usd"]), ("100", "90.0"))

    def test_lots_capped_by_max_lots(self):
        self.cfg.MAX_LOTS = 80
        self.ctl(lots=500)
        self.assertEqual(self.enter()["lots"], 80)

    def test_bad_or_missing_lots_never_use_the_base_size(self):
        for c in ({"lots": "lots"}, {"lots": 0}, {"live": True}):
            self.ctl(**c)
            self.assertIsNone(self.enter())
            self.assertEqual(self.fx.orders, [])

    def test_startup_line_shows_partner_lots_and_stop(self):
        self.ctl(lots=40)
        said = []
        self.b.notify = said.append
        self.b.sleep = lambda s: (_ for _ in ()).throw(SystemExit)
        with self.assertRaises(SystemExit):
            self.b.run()
        self.assertIn("40 lots from the control file", said[0])
        self.assertIn("stop at $36", said[0])
        self.assertNotIn("200 lots", said[0])


class TestPaperNights(PartnerBase):
    def test_switched_off_trades_paper_without_orders(self):
        self.ctl(lots=50, live=False)
        pos = self.enter()
        self.assertEqual((pos["trade_mode"], pos["paper_reason"]), ("paper", "trading switched off"))
        self.assertEqual(self.fx.orders, [])
        self.clock[0] = EXPIRY + 11 * 60
        self.b.step()
        self.assertEqual(self.rows()[-1]["mode"], "paper")

    def test_planned_paper_day(self):
        self.ctl(lots=50, live=True, paper_dates=[self.b.night_day().isoformat()])
        pos = self.enter()
        self.assertEqual((pos["trade_mode"], pos["paper_reason"]), ("paper", "planned paper day"))
        self.assertEqual(self.fx.orders, [])

    def test_other_days_stay_live(self):
        self.ctl(lots=50, live=True, paper_dates=["2020-01-01"])
        self.assertEqual(self.enter()["trade_mode"], "live")

    def test_unreadable_or_missing_control_file_means_no_trade(self):
        with open(self.ctl_path, "w") as f:
            f.write("{not json")
        self.assertIsNone(self.enter())
        os.remove(self.ctl_path)
        self.assertIsNone(self.enter())
        self.assertEqual(self.fx.orders, [])

    def test_off_switch_after_entry_does_not_touch_open_trade(self):
        self.ctl(lots=50, live=True)
        pos = self.enter()
        self.ctl(lots=50, live=False)
        self.b.step()
        self.assertEqual(self.b.state["open"]["trade_mode"], "live")
        self.assertIs(self.b.state["open"]["legs"], self.b.state["open"]["legs"])
        self.assertEqual(pos["entry_ts"], self.b.state["open"]["entry_ts"])


class TestAutoPaper(PartnerBase):
    def setUp(self):
        super().setUp()
        self.fx.used = 0.9                                  # margin in use 90% > 50%: live entry not possible

    def test_auto_paper_with_reason(self):
        self.cfg.AUTO_PAPER = True
        self.ctl(lots=50, live=True)
        pos = self.enter()
        self.assertEqual(pos["trade_mode"], "paper")
        self.assertIn("live entry not possible: margin in use 90%", pos["paper_reason"])
        self.assertEqual(self.fx.orders, [])

    def test_without_auto_paper_no_entry(self):
        self.ctl(lots=50, live=True)
        self.assertIsNone(self.enter())


class TestCloseNow(PartnerBase):
    def setUp(self):
        super().setUp()
        self.ctl(lots=50, live=True)
        self.pos = self.enter()

    def next_evening(self):
        self.clock[0] = EXPIRY + 4 * 3600 + 10 * 60          # 21:40 on expiry day: the next night
        self.fx = FakeDelta(sfx="231026")                    # that night's chain (23 Oct expiry)
        self.b.c = self.fx

    def test_closes_live_and_keeps_a_shadow_to_the_normal_exit(self):
        self.ctl(lots=50, live=True, close_now="r1")
        self.b.step()
        self.assertEqual(self.fx.positions(), [])            # both legs bought back
        r = self.rows()[-1]
        self.assertEqual(r["mode"], "live")
        self.assertIn("closed by you", r["note"])
        sh = self.b.state["open"]
        self.assertEqual((sh["trade_mode"], sh["lots"], sh["entry_ts"]), ("shadow", 50, self.pos["entry_ts"]))
        n_orders = len(self.fx.orders)
        self.clock[0] = EXPIRY + 11 * 60
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.rows()[-1]["mode"], "shadow")  # the bot's result if held
        self.assertEqual(len(self.fx.orders), n_orders)      # the shadow never trades

    def test_same_request_is_not_applied_twice(self):
        self.ctl(lots=50, live=True, close_now="r1")
        self.b.step()
        self.b.step()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.b.state["open"]["trade_mode"], "shadow")

    def test_shadow_follows_the_normal_stop(self):
        self.ctl(lots=50, live=True, close_now="r1")
        self.b.step()
        bump(self.fx, self.pos["legs"]["call_short"]["symbol"], 8)
        self.b.step()
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        r = self.rows()[-1]
        self.assertEqual(r["mode"], "shadow")
        self.assertIn("stopped", r["note"])

    def test_stale_request_never_closes_a_later_trade(self):
        self.clock[0] = EXPIRY + 11 * 60
        self.fx.pos.clear()                                  # Delta has settled the expired options
        self.b.step()                                        # settled; no position
        self.assertIsNone(self.b.state["open"])
        self.ctl(lots=50, live=True, close_now="old")
        self.b.step()
        self.assertEqual(self.b.state["close_done"], "old")
        self.next_evening()
        self.b.step()
        self.assertEqual(self.b.state["open"]["trade_mode"], "live")

    def test_close_after_expiry_waits_for_settlement_instead(self):
        self.clock[0] = EXPIRY + 11 * 60                      # expired; Delta not settled yet
        self.ctl(lots=50, live=True, close_now="late")
        n = len(self.fx.orders)
        self.b.step()
        self.assertEqual(len(self.fx.orders), n)              # no buy-back of expired options
        self.assertEqual(self.b.state["open"]["trade_mode"], "live")
        self.fx.pos.clear()
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.assertEqual(self.rows()[-1]["mode"], "live")

    def test_close_on_a_paper_night_does_nothing(self):
        self.clock[0] = EXPIRY + 11 * 60
        self.fx.pos.clear()
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.ctl(lots=50, live=False)
        self.next_evening()
        self.b.step()
        self.assertEqual(self.b.state["open"]["trade_mode"], "paper")
        self.ctl(lots=50, live=False, close_now="r9")
        self.b.step()
        self.assertEqual(self.b.state["open"]["trade_mode"], "paper")


class TestAlertsAndDelay(PartnerBase):
    def test_alerts_off_sends_nothing(self):
        self.cfg.TELEGRAM_BOT_TOKEN, self.cfg.TELEGRAM_CHAT_ID = "x", "1"
        self.ctl(lots=50)
        with mock.patch.object(cd.requests, "post") as post:
            self.enter()
            self.b.notify("hello")
        post.assert_not_called()

    def test_alerts_full_still_sends(self):
        self.cfg.ALERTS = "full"
        self.cfg.TELEGRAM_BOT_TOKEN, self.cfg.TELEGRAM_CHAT_ID = "x", "1"
        with mock.patch.object(cd.requests, "post") as post:
            self.b.notify("hello")
        post.assert_called_once()

    def test_entry_delay(self):
        self.cfg.ENTRY_DELAY_SEC = 60
        self.ctl(lots=50)
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.clock[0] += 30
        self.b.step()
        self.assertIsNone(self.b.state["open"])
        self.clock[0] += 31
        self.b.step()
        self.assertIsNotNone(self.b.state["open"])

    def test_delay_works_with_dampened_entry(self):
        self.cfg.VOL_SWITCH, self.cfg.ENTRY_MODE, self.cfg.ENTRY_DELAY_SEC = True, "dampened", 120
        give_candles(self.fx, 20.0, 10.0)
        self.ctl(lots=50)
        self.b.step()
        self.assertIsNone(self.b.state["open"])


class TestCredentials(unittest.TestCase):
    def test_reads_systemd_credentials(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "delta_api_key"), "w") as f:
            f.write("KEY123\n")
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": d}):
            self.assertEqual(cd._cred("delta_api_key"), "KEY123")
            self.assertEqual(cd._cred("delta_api_secret"), "")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cd._cred("delta_api_key"), "")


if __name__ == "__main__":
    unittest.main()
