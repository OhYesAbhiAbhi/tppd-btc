"""Read-only check on the server: does Delta price options higher on volatile nights, and how would a
0.10-delta strangle have done at Delta's REAL quotes on the nights the strangle bot skipped?
Uses ~/btc_data (collector). Changes nothing."""
import gzip, math, shutil, sqlite3, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
IST = ZoneInfo("Asia/Kolkata"); DIR = Path.home() / "btc_data"; LOTS = 200; CV = 0.001
core = sqlite3.connect(DIR / "core.db")
C = dict(core.execute("select time, close from candles_1m"))
inst = {r[0]: r[1:] for r in core.execute("select id, symbol, kind, strike, expiry_utc from instruments")}
settle = dict(core.execute("select expiry_utc, settlement_price from settlements"))
fee = lambda p, S: min(0.0001 * S, 0.035 * p) * 1.18 if p > 0 else 0.0

def rv(ts, minutes):
    xs = [C.get(ts - 60 * i) for i in range(minutes, -1, -1)]
    xs = [x for x in xs if x]
    if len(xs) < 0.8 * minutes: return None
    r = [math.log(b / a) for a, b in zip(xs[:-1], xs[1:])]; m = sum(r) / len(r)
    return math.sqrt(sum((y - m) ** 2 for y in r) / (len(r) - 1)) * math.sqrt(525600) * 100

def opt_db(day):
    p = DIR / f"options_{day}.db"
    if p.exists(): return sqlite3.connect(p)
    g = DIR / f"options_{day}.db.gz"
    if not g.exists(): return None
    t = Path(tempfile.mkdtemp()) / "o.db"
    with gzip.open(g) as fi, open(t, "wb") as fo: shutil.copyfileobj(fi, fo)
    return sqlite3.connect(t)

def snaps(dbs, ts, exp_iso):
    """option rows (iid -> row) at the snapshot nearest ts, for one expiry"""
    for db in dbs:
        r = db.execute("select ts from option_snaps where ts between ? and ? order by abs(ts-?) limit 1", (ts - 120, ts + 120, ts)).fetchone()
        if r:
            rows = db.execute("select iid,bid,ask,mark_iv,bid_iv,delta,spot from option_snaps where ts=?", (r[0],)).fetchall()
            return {x[0]: x for x in rows if x[0] in inst and inst[x[0]][3] == exp_iso}
    return {}

out = []
start = datetime.fromtimestamp(min(C), IST).date() + timedelta(days=2)
d = start
while (datetime(d.year, d.month, d.day, 17, 30, tzinfo=IST) + timedelta(days=1)) < datetime.now(IST) - timedelta(minutes=30):
    t0 = datetime(d.year, d.month, d.day, 22, 30, tzinfo=IST); ts = int(t0.timestamp()) // 60 * 60
    exp = (t0 + timedelta(days=1)).replace(hour=17, minute=30).astimezone(timezone.utc)
    exp_iso = exp.isoformat()
    v = (rv(ts, 60), rv(ts, 360), rv(ts, 1440))
    calm = None not in v and v[0] <= 30 and v[1] <= 30 and v[2] <= 40
    dbs = [x for x in (opt_db(d.isoformat()), opt_db((d + timedelta(days=1)).isoformat())) if x]
    rows = snaps(dbs, ts, exp_iso) if dbs else {}
    if not rows: d += timedelta(days=1); continue
    S0 = next(iter(rows.values()))[6] or C.get(ts)
    atm = [r for r in rows.values() if abs(inst[r[0]][2] / S0 - 1) < 0.006 and r[3]]
    atm_iv = sum(r[3] for r in atm) / len(atm) if atm else None
    res = dict(night=d, day=t0.strftime("%a"), calm=calm, v=v, atm_iv=atm_iv)
    puts = [r for r in rows.values() if inst[r[0]][1] == "put" and r[5] and r[1] and inst[r[0]][2] < S0]
    calls = [r for r in rows.values() if inst[r[0]][1] == "call" and r[5] and r[1] and inst[r[0]][2] > S0]
    if puts and calls and not calm and t0.strftime("%a") != "Thu":
        p = min(puts, key=lambda r: abs(abs(r[5]) - 0.10)); c = min(calls, key=lambda r: abs(abs(r[5]) - 0.10))
        credit = p[1] + c[1]; fin = fee(p[1], S0) + fee(c[1], S0); u = LOTS * CV
        why, net = "expiry", None
        t = ts + 300
        while t < int(exp.timestamp()) - 300:
            dd = datetime.fromtimestamp(t, IST).date().isoformat()
            r2 = snaps([x for x in (opt_db(dd),) if x], t, exp_iso) if dd else {}
            if p[0] in r2 and c[0] in r2 and r2[p[0]][2] and r2[c[0]][2]:
                cost = r2[p[0]][2] + r2[c[0]][2]
                pnl = (credit - cost - fin - fee(r2[p[0]][2], S0) - fee(r2[c[0]][2], S0)) * u
                if pnl <= -credit * u: why, net = "stop", pnl; break
            t += 900
        if net is None:
            e0 = int(exp.timestamp()) // 60 * 60
            stl = settle.get(exp_iso) or next((C[e0 + k * 60] for k in (0, -1, 1, -2, 2, -5, 5, -10, 10) if (e0 + k * 60) in C), None)
            if stl is None: d += timedelta(days=1); continue
            iv = max(inst[p[0]][2] - stl, 0) + max(stl - inst[c[0]][2], 0)
            net = (credit - iv - fin - fee(iv, S0)) * u
        res.update(strikes=f"{inst[p[0]][2]:.0f}/{inst[c[0]][2]:.0f}", credit=credit * u, bid_iv=(p[4], c[4]), net=net, why=why)
    out.append(res); d += timedelta(days=1)

f = lambda x: "  n/a" if x is None else f"{x:5.0f}"
print("night        calm  1h/6h/24h vol     ATM mark IV | 0.10-delta strangle on skipped non-Thu nights (real Delta bids/asks)")
for r in out:
    s = f"{r['night']} {r['day']} {'calm' if r['calm'] else ' -  '} {f(r['v'][0])}{f(r['v'][1])}{f(r['v'][2])}   {r['atm_iv'] if r['atm_iv'] is None else round(r['atm_iv'], 3)!s:>7}"
    if "net" in r: s += f" | {r['strikes']} credit ${r['credit']:.1f} -> {r['net']:+.1f} ({r['why']}) bid IVs {r['bid_iv']}"
    print(s)
cal = [r["atm_iv"] for r in out if r["calm"] and r["atm_iv"]]; hot = [r["atm_iv"] for r in out if not r["calm"] and r["atm_iv"]]
if cal and hot: print(f"\nAverage ATM IV at 22:30: calm nights {sum(cal)/len(cal):.3f} ({len(cal)}), volatile nights {sum(hot)/len(hot):.3f} ({len(hot)})")
tr = [r["net"] for r in out if "net" in r]
if tr: print(f"0.10-delta strangle on skipped nights: {len(tr)} trades, total {sum(tr):+.1f} USD at {LOTS} lots, wins {sum(x > 0 for x in tr)}")
