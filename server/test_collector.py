"""Offline tests for the data recorder (no network)."""
import gzip
import math
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

import collector as col
from collector import IST, Collector, CollectorConfig, expiry_of, expiry_type, rsi

T0 = int(datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc).timestamp())   # 15:30 IST


class FakeDelta:
    def __init__(self):
        self.spot = 82000.0
        self.product_calls = []

    def candles(self, symbol, res, start, end):
        out = []
        for t in range(int(start) // 60 * 60, int(end), 60):
            p = 82000 + 300 * math.sin(t / 3600)
            out.append({"time": t, "open": p, "high": p + 5, "low": p - 5, "close": p, "volume": 10})
        return out

    def ticker(self, symbol):
        return {"spot_price": str(self.spot), "mark_price": str(self.spot + 3), "close": str(self.spot + 2),
                "funding_rate": "0.0012", "oi": "870.5", "oi_value_usd": "7.1e7", "volume": "7000",
                "turnover_usd": "5.8e8", "mark_basis": "-0.0004",
                "quotes": {"best_bid": "81999.5", "best_ask": "82000.5", "bid_size": "4000", "ask_size": "2500"}}

    def orderbook(self, symbol):
        return {"buy": [{"price": "81999.5", "size": 100}, {"price": "81950", "size": 200}, {"price": "81000", "size": 999}],
                "sell": [{"price": "82000.5", "size": 50}, {"price": "82300", "size": 70}]}

    def all_options(self, und):
        out, pid = [], 1
        for sfx in ("231026", "241026", "251026", "301026", "271126", "251226"):   # daily x3, weekly, monthly, quarterly
            for K in range(80000, 84001, 1000):
                for ct, pre in (("call_options", "C"), ("put_options", "P")):
                    out.append({"symbol": f"{pre}-BTC-{K}-{sfx}", "product_id": pid, "contract_type": ct,
                                "strike_price": str(K), "mark_price": "100", "oi": "5", "volume": "2",
                                "spot_price": str(self.spot),
                                "quotes": {"best_bid": "95", "best_ask": "105", "bid_size": "10", "ask_size": "12",
                                           "bid_iv": "0.30", "ask_iv": "0.34", "mark_iv": "0.32"},
                                "greeks": {"delta": "0.3", "gamma": "0.001", "theta": "-40", "vega": "5"}})
                    pid += 1
        out.append({"symbol": "C-BTC-90000-231026", "product_id": 999, "contract_type": "call_options",
                    "strike_price": "90000", "oi": "0", "quotes": {"best_bid": "0", "best_ask": "0"}, "greeks": {}})
        return out

    def product(self, symbol):
        self.product_calls.append(symbol)
        return {"settlement_price": "82111.5"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = type("C", (CollectorConfig,), {"DATA_DIR": self.tmp, "TELEGRAM_BOT_TOKEN": "",
                                                  "BACKFILL_DAYS": 3})
        self.clock = [T0]
        self.fx = FakeDelta()
        self.c = Collector(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)

    def tearDown(self):
        shutil.rmtree(self.tmp)



class TestPieces(unittest.TestCase):
    def test_expiry_parse_and_type(self):
        self.assertEqual(expiry_of("C-BTC-82000-231026"), datetime(2026, 10, 23, 12, tzinfo=timezone.utc))
        self.assertEqual(expiry_type(expiry_of("C-BTC-1-221026")), "daily")      # Thursday
        self.assertEqual(expiry_type(expiry_of("C-BTC-1-231026")), "weekly")     # Friday, not last
        self.assertEqual(expiry_type(expiry_of("C-BTC-1-301026")), "monthly")    # last Friday of Oct
        self.assertEqual(expiry_type(expiry_of("C-BTC-1-251226")), "quarterly")  # last Friday of Dec

    def test_rsi_extremes_and_mid(self):
        self.assertEqual(rsi(list(range(30))), 100.0)
        self.assertLess(rsi(list(range(30, 0, -1))), 1)
        self.assertAlmostEqual(rsi([1, 2] * 20), 50, delta=5)
        self.assertIsNone(rsi([1, 2, 3]))


class TestCollector(Base):
    def test_backfill_and_minute_step(self):
        self.c.backfill()
        n = self.c.core.execute("select count(*) from candles_1m").fetchone()[0]
        self.assertGreaterEqual(n, 3 * 1440)
        self.clock[0] = T0 + 60            # minute 1: not a 5-min mark -> near expiries only
        rows = self.c.step()
        self.assertEqual(rows, 3 * 5 * 2)  # 3 daily expiries x 5 strikes x 2 kinds; empty 90k call skipped
        self.clock[0] = T0 + 300           # 5-minute mark -> every expiry
        self.assertEqual(self.c.step(), 6 * 5 * 2)
        m = self.c.core.execute("select funding_rate, oi, depth_bid_01, depth_ask_01, depth_bid_05 from market "
                                "order by ts desc").fetchone()
        self.assertEqual(m[:2], (0.0012, 870.5))
        self.assertEqual(m[2], 100 + 200)  # within 0.1% of mid (81,950 is 0.06% away)
        self.assertEqual(m[3], 50)
        self.assertEqual(m[4], 300)        # 81,000 is >0.5% away
        ind = self.c.core.execute("select rsi_1m, rsi_5m, rsi_15m, rsi_1h, bb_z_5m, ema_240, atr_1h_pct "
                                  "from indicators").fetchone()
        self.assertTrue(all(v is not None for v in ind), ind)
        types = dict(self.c.core.execute("select expiry_type, count(distinct expiry_utc) from instruments group by 1"))
        self.assertEqual(types, {"daily": 2, "weekly": 1, "monthly": 2, "quarterly": 1})

    def test_option_values_stored(self):
        self.c.backfill()
        self.c.step()
        db = sqlite3.connect(f"{self.tmp}/options_2026-10-23.db")
        r = db.execute("select bid, ask, bid_iv, mark_iv, delta, theta, oi, spot from option_snaps limit 1").fetchone()
        self.assertEqual(r, (95.0, 105.0, 0.30, 0.32, 0.3, -40.0, 5.0, 82000.0))

    def test_day_rollover_gzip_and_export(self):
        self.c.backfill()
        self.c.step()
        self.clock[0] = int(datetime(2026, 10, 24, 0, 30, tzinfo=IST).timestamp())   # next IST day
        self.c.step()
        self.c.maintain(self.clock[0])
        names = sorted(p.name for p in __import__("pathlib").Path(self.tmp).glob("options_*"))
        self.assertIn("options_2026-10-23.db.gz", names)
        self.assertIn("options_2026-10-24.db", names)
        gz = self.c.export("2026-10-23", "2026-10-24")
        raw = f"{self.tmp}/check.db"
        with gzip.open(gz) as a, open(raw, "wb") as b:
            b.write(a.read())
        db = sqlite3.connect(raw)
        self.assertGreater(db.execute("select count(distinct ts) from option_snaps").fetchone()[0], 1)
        self.assertGreater(db.execute("select count(*) from candles_1m").fetchone()[0], 1000)

    def test_settlement_recorded_after_expiry(self):
        self.c.backfill()
        self.c.step()
        self.clock[0] = int(datetime(2026, 10, 23, 12, 30, tzinfo=timezone.utc).timestamp())
        self.c.record_settlements(self.clock[0])
        r = self.c.core.execute("select expiry_type, settlement_price, btc_close_at_expiry from settlements").fetchall()
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0][0], "weekly")
        self.assertEqual(r[0][1], 82111.5)
        self.assertIsNotNone(r[0][2])

    def test_low_disk_keeps_near_only_then_stops(self):
        self.c.backfill()
        self.clock[0] = T0 + 300
        self.c.free_gb = lambda: 1.5
        self.assertEqual(self.c.step(), 3 * 5 * 2)
        self.c.free_gb = lambda: 0.5
        self.assertEqual(self.c.step(), 0)

    def test_restart_keeps_instrument_ids(self):
        self.c.backfill()
        self.c.step()
        ids = dict(self.c.iids)
        c2 = Collector(self.fx, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)
        self.assertEqual(c2.iids, ids)
        self.assertTrue(c2.state.get("backfilled"))

    def test_status_text(self):
        self.c.backfill()
        self.c.step()
        s = self.c.status()
        self.assertIn("candles 1m", s)
        self.assertIn("RSI 1m", s)


if __name__ == "__main__":
    unittest.main(verbosity=1)
