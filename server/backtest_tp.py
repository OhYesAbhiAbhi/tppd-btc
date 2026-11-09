"""Strangle + $40 stop, with and without a per-leg take-profit (buy a leg back once X% of its premium
has decayed; the other leg stays open to expiry, still under the stop on the whole position).

python3 backtest_tp.py <btc_options.db> [entry=21:30] [stop=40]
Same entries, strikes and lots as compare3.py / condor.py.
"""
import sqlite3, sys
import numpy as np, pandas as pd

db = sys.argv[1]
ENTRY = sys.argv[2] if len(sys.argv) > 2 else "21:30"
STOP = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0
TPS = [None, 0.95, 0.90, 0.80, 0.70]
CV, MAXLOSS, MAXLOTS, MAXW = 0.001, 40.0, 100, 1500
fee = lambda p, S: min(0.0001 * S, 0.035 * p) * 1.18 if p > 0 else 0.0
settle_fee_max = lambda S: 2 * 0.0001 * S * 1.18

c = sqlite3.connect(db)
m = pd.read_sql("select timestamp, coalesce(spot_price,btc_spot_price) spot from market_snapshots "
                "where coalesce(spot_price,btc_spot_price)>0", c)
m["t"] = pd.to_datetime(m.timestamp, utc=True, format="mixed")
s = m.sort_values("t").set_index("t").spot
o = pd.read_sql("select timestamp, expiry, symbol, option_type, strike, bid, ask, delta from option_snapshots "
                "where expiry like '%T12:00:00%'", c)
o["t"] = pd.to_datetime(o.timestamp, utc=True, format="mixed")
o["exp"] = pd.to_datetime(o.expiry, utc=True)
o["kind"] = np.where(o.option_type == "put_options", "put", "call")

hh, mm = (int(x) for x in ENTRY.split(":"))
rows = []
for exp, g in o.groupby("exp"):
    exp_ist = exp.tz_convert("Asia/Kolkata")
    tgt = (exp_ist - pd.Timedelta(days=1)).replace(hour=hh, minute=mm).tz_convert("UTC")
    times = pd.DatetimeIndex(sorted(g.t.unique()))
    k = times.searchsorted(tgt)
    if k >= len(times) or times[k] - tgt > pd.Timedelta(minutes=150):
        continue
    t0 = times[k]; ch = g[g.t == t0]; S0 = s.asof(t0)

    def short(kind):
        x = ch[(ch.kind == kind) & ((ch.strike < S0) if kind == "put" else (ch.strike > S0)) & (ch.bid > 0) & (ch.ask > 0)]
        x = x[((x.ask - x.bid) / ((x.ask + x.bid) / 2) <= 0.15) & ((x.delta.abs() - 0.20).abs() <= 0.07)]
        return None if x.empty else x.iloc[(x.delta.abs() - 0.20).abs().argmin()]

    def wing(sh):
        x = ch[(ch.kind == sh.kind) & (ch.ask > 0) & ((ch.strike < sh.strike) if sh.kind == "put" else (ch.strike > sh.strike))
               & ((ch.strike - sh.strike).abs() <= MAXW)]
        return None if x.empty else x.iloc[(x.delta.abs() - 0.07).abs().argmin()]

    ps, cs = short("put"), short("call")
    if ps is None or cs is None:
        continue
    pw, cw = wing(ps), wing(cs)
    if pw is None or cw is None:
        continue
    credit_c = ps.bid + cs.bid - pw.ask - cw.ask
    fees_c = sum(fee(p, S0) for p in (ps.bid, cs.bid, pw.ask, cw.ask))
    width = max(ps.strike - pw.strike, cw.strike - cs.strike)
    lots = min(MAXLOTS, int(MAXLOSS // ((width - credit_c + fees_c + settle_fee_max(S0)) * CV)))
    units = lots * CV
    near = s[(s.index >= exp - pd.Timedelta(minutes=10)) & (s.index <= exp + pd.Timedelta(minutes=10))]
    if near.empty:
        continue
    ST = near.iloc[np.abs((near.index - exp).total_seconds()).argmin()]
    intr = {ps.symbol: max(ps.strike - ST, 0), cs.symbol: max(ST - cs.strike, 0)}
    entry = {ps.symbol: ps.bid, cs.symbol: cs.bid}
    fin = fee(ps.bid, S0) + fee(cs.bid, S0)
    path = g[(g.t > t0) & (g.t < exp) & (g.symbol.isin([ps.symbol, cs.symbol]))]
    piv = path.pivot_table(index="t", columns="symbol", values="ask", aggfunc="last").dropna()
    gap = piv.index.to_series().diff().max() if len(piv) > 1 else pd.Timedelta(days=1)
    row = dict(night=str(exp_ist.date()), btc_entry=round(S0), btc_1730=round(ST), put_K=int(ps.strike),
               call_K=int(cs.strike), lots=lots, credit=round((ps.bid + cs.bid) * units, 2),
               cont=gap <= pd.Timedelta(minutes=30))
    for tp in TPS:
        exit_, how = {}, ""
        res = None
        for t, r in piv.iterrows():
            if tp is not None:
                for sym in (ps.symbol, cs.symbol):
                    if sym not in exit_ and r[sym] <= (1 - tp) * entry[sym]:
                        exit_[sym] = r[sym]
                        how += f"{'P' if sym == ps.symbol else 'C'}@{t.tz_convert('Asia/Kolkata'):%H:%M} "
                if len(exit_) == 2:
                    break
            cost = {sym: exit_.get(sym, r[sym]) for sym in (ps.symbol, cs.symbol)}
            pnl = (sum(entry.values()) - sum(cost.values()) - fin - sum(fee(v, S0) for v in cost.values())) * units
            if pnl <= -STOP:
                res, how = pnl, how + f"STOP@{t.tz_convert('Asia/Kolkata'):%H:%M}"
                break
        if res is None:
            val = {sym: exit_.get(sym, intr[sym]) for sym in (ps.symbol, cs.symbol)}
            res = (sum(entry.values()) - sum(val.values()) - fin - sum(fee(v, S0) for v in val.values())) * units
        lab = "no_tp" if tp is None else f"tp{int(tp * 100)}"
        row[lab], row[lab + "_how"] = round(res, 2), how.strip()
    rows.append(row)

d = pd.DataFrame(rows)
pd.set_option("display.width", 250)
print(d.to_string(index=False))
labs = ["no_tp"] + [f"tp{int(t * 100)}" for t in TPS[1:]]
for name, x in (("ALL nights with a known 17:30 price", d), ("nights with continuous quotes", d[d.cont])):
    print(f"\n== {name}: {len(x)} nights")
    for lab in labs:
        print(f"  {lab:6} total {x[lab].sum():+8.2f} USD | wins {(x[lab] > 0).sum():2}/{len(x)} | "
              f"avg {x[lab].mean():+6.2f} | worst {x[lab].min():+7.2f} | "
              f"legs taken early {x[lab + '_how'].str.count('@').sum() - x[lab + '_how'].str.count('STOP').sum() if lab != 'no_tp' else 0}")
