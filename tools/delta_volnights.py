"""Read-only check on the server: does Delta price options higher on volatile nights, and how would a
0.10-delta strangle have done at Delta's REAL quotes on the nights the strangle bot skipped?
Uses ~/btc_data (collector). Changes nothing. Unpacks ONE day-file at a time to a temp file, keeps only
the 15-minute snapshots of the near expiries in memory, and deletes the temp file straight away."""
import gzip, math, os, shutil, sqlite3, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata"); DIR = Path.home() / "btc_data"; LOTS = 200; CV = 0.001
core = sqlite3.connect(f"file:{DIR / 'core.db'}?mode=ro", uri=True)
C = dict(core.execute("select time, close from candles_1m"))
inst = {r[0]: r[1:] for r in core.execute("select id, symbol, kind, strike, expiry_utc from instruments")}
settle = dict(core.execute("select expiry_utc, settlement_price from settlements"))
fee = lambda p, S: min(0.0001 * S, 0.035 * p) * 1.18 if p > 0 else 0.0
free_gb = lambda: shutil.disk_usage(Path.home()).free / 1e9


def rv(ts, minutes):
    xs = [C.get(ts - 60 * i) for i in range(minutes, -1, -1)]
    xs = [x for x in xs if x]
    if len(xs) < 0.8 * minutes:
        return None
    r = [math.log(b / a) for a, b in zip(xs[:-1], xs[1:])]; m = sum(r) / len(r)
    return math.sqrt(sum((y - m) ** 2 for y in r) / (len(r) - 1)) * math.sqrt(525600) * 100


SNAP = {}          # 15-min slot ts -> {iid: (bid, ask, mark_iv, bid_iv, delta, spot)}


def load_day(day):
    """Read one IST day of option snapshots (15-minute slots, expiries within 3 days only)."""
    p, tmp = DIR / f"options_{day}.db", None
    if not p.exists():
        g = DIR / f"options_{day}.db.gz"
        if not g.exists():
            return
        if free_gb() < 6:
            raise SystemExit(f"Stopping: only {free_gb():.1f} GB free - not unpacking more data.")
        fd, tmp = tempfile.mkstemp(suffix=".db", dir=str(DIR)); os.close(fd)
        with gzip.open(g) as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        p = Path(tmp)
    try:
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        lim = (datetime.fromisoformat(day).replace(tzinfo=IST) + timedelta(days=3)).astimezone(timezone.utc).isoformat()
        near = {i for i, v in inst.items() if v[3] <= lim}
        for ts, iid, bid, ask, miv, biv, dl, spot in db.execute(
                "select ts,iid,bid,ask,mark_iv,bid_iv,delta,spot from option_snaps where (ts/60)%15=0"):
            if iid in near:
                SNAP.setdefault(ts // 60 * 60, {})[iid] = (bid, ask, miv, biv, dl, spot)
        db.close()
    finally:
        if tmp:
            os.remove(tmp)


def at(ts, exp_iso):
    for k in (0, -60, 60, -120, 120):
        s = SNAP.get(ts + k)
        if s:
            return {i: r for i, r in s.items() if inst[i][3] == exp_iso}
    return {}


first = datetime.fromtimestamp(min(C), IST).date() + timedelta(days=2)
last_exp_ok = datetime.now(IST) - timedelta(minutes=30)
days, d = [], first
while datetime(d.year, d.month, d.day, 17, 30, tzinfo=IST) + timedelta(days=1) < last_exp_ok:
    days.append(d); d += timedelta(days=1)
loaded = set()
out = []
for d in days:
    for x in (d, d + timedelta(days=1)):
        if x.isoformat() not in loaded:
            load_day(x.isoformat()); loaded.add(x.isoformat())
    for old in [k for k in SNAP if k < int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())]:
        del SNAP[old]                                    # keep memory small
    t0 = datetime(d.year, d.month, d.day, 22, 30, tzinfo=IST); ts = int(t0.timestamp()) // 60 * 60
    exp = (t0 + timedelta(days=1)).replace(hour=17, minute=30).astimezone(timezone.utc); exp_iso = exp.isoformat()
    v = (rv(ts, 60), rv(ts, 360), rv(ts, 1440))
    calm = None not in v and v[0] <= 30 and v[1] <= 30 and v[2] <= 40
    rows = at(ts, exp_iso)
    if not rows:
        continue
    S0 = next(iter(rows.values()))[5] or C.get(ts)
    atm = [r for i, r in rows.items() if abs(inst[i][2] / S0 - 1) < 0.006 and r[2]]
    res = dict(night=d, day=t0.strftime("%a"), calm=calm, v=v, atm_iv=(sum(r[2] for r in atm) / len(atm) if atm else None))
    puts = [(i, r) for i, r in rows.items() if inst[i][1] == "put" and r[4] and r[0] and inst[i][2] < S0]
    calls = [(i, r) for i, r in rows.items() if inst[i][1] == "call" and r[4] and r[0] and inst[i][2] > S0]
    if puts and calls and not calm and t0.strftime("%a") != "Thu":
        pi, p = min(puts, key=lambda x: abs(abs(x[1][4]) - 0.10)); ci, c = min(calls, key=lambda x: abs(abs(x[1][4]) - 0.10))
        credit = p[0] + c[0]; fin = fee(p[0], S0) + fee(c[0], S0); u = LOTS * CV; why, net = "expiry", None
        t = ts + 900
        while t < int(exp.timestamp()) - 300:
            r2 = at(t, exp_iso)
            if pi in r2 and ci in r2 and r2[pi][1] and r2[ci][1]:
                pnl = (credit - r2[pi][1] - r2[ci][1] - fin - fee(r2[pi][1], S0) - fee(r2[ci][1], S0)) * u
                if pnl <= -credit * u:
                    why, net = "stop", pnl; break
            t += 900
        if net is None:
            e0 = int(exp.timestamp()) // 60 * 60
            stl = settle.get(exp_iso) or next((C[e0 + k * 60] for k in (0, -1, 1, -2, 2, -5, 5, -10, 10) if e0 + k * 60 in C), None)
            if stl is None:
                out.append(res); continue
            iv = max(inst[pi][2] - stl, 0) + max(stl - inst[ci][2], 0)
            net = (credit - iv - fin - fee(iv, S0)) * u
        res.update(strikes=f"{inst[pi][2]:.0f}/{inst[ci][2]:.0f}", credit=credit * u, net=net, why=why)
    out.append(res)

f = lambda x: "  n/a" if x is None else f"{x:5.0f}"
print("night          calm  1h   6h  24h   ATM IV | 0.10-delta strangle on skipped non-Thu nights (real Delta quotes)")
for r in out:
    s = (f"{r['night']} {r['day']} {'calm' if r['calm'] else ' -  '}{f(r['v'][0])}{f(r['v'][1])}{f(r['v'][2])}  "
         f"{'n/a' if r['atm_iv'] is None else round(r['atm_iv'], 3)!s:>7}")
    if "net" in r:
        s += f" | {r['strikes']} credit ${r['credit']:.1f} -> {r['net']:+.1f} ({r['why']})"
    print(s)
cal = [r["atm_iv"] for r in out if r["calm"] and r["atm_iv"]]; hot = [r["atm_iv"] for r in out if not r["calm"] and r["atm_iv"]]
if cal and hot:
    print(f"\nAverage ATM IV at 22:30: calm nights {sum(cal)/len(cal):.3f} ({len(cal)}), volatile nights {sum(hot)/len(hot):.3f} ({len(hot)})")
tr = [r["net"] for r in out if "net" in r]
if tr:
    print(f"0.10-delta strangle on skipped nights: {len(tr)} trades, total {sum(tr):+.1f} USD at {LOTS} lots, wins {sum(x > 0 for x in tr)}")
print(f"(disk free now: {free_gb():.1f} GB)")
