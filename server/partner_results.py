"""Partner results: day-wise totals from a partner instance's trades.csv, and the privacy-safe texts.

Only the day, lots, live/paper/shadow and net USD leave this module. Never strikes, prices, times,
exit reasons or rule names. Profit counts on the IST day the trade closed (same rule as the P&L page).
"""
import csv
import re
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path

INR_DEFAULT = 85.0
WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def close_day(row):
    """IST date the trade closed: an early exit's 'at HH:MM' in the note, else the expiry day."""
    try:
        entry = datetime.strptime(row["entry_ist"], "%Y-%m-%d %H:%M")
        expiry = datetime.strptime(row["expiry"], "%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        return None
    m = re.search(r"\bat (\d{1,2}):(\d{2})\b", row.get("note") or "")
    if not m:
        return expiry.date()
    hm = int(m.group(1)) * 60 + int(m.group(2))
    if entry.date() == expiry.date():
        return entry.date()
    return entry.date() if hm >= entry.hour * 60 + entry.minute else expiry.date()


def days(trades_csv):
    """{date: {"live": net, "paper": net, "shadow": net, "lots": n, "closed_early": bool}} oldest first."""
    out = {}
    p = Path(trades_csv)
    if not p.exists():
        return OrderedDict()
    with p.open() as f:
        for r in csv.DictReader(f):
            d = close_day(r)
            try:
                net = float(r.get("net_usd") or 0)
                lots = int(float(r.get("lots") or 0))
            except ValueError:
                continue
            if d is None:
                continue
            mode = (r.get("mode") or "live").strip()
            mode = mode if mode in ("live", "paper", "shadow") else "paper"
            x = out.setdefault(d, {"live": None, "paper": None, "shadow": None, "lots": 0, "closed_early": False})
            x[mode] = round((x[mode] or 0) + net, 2)
            x["lots"] = max(x["lots"], lots)
            if mode == "live" and "closed by you" in (r.get("note") or ""):
                x["closed_early"] = True
    return OrderedDict(sorted(out.items()))


def month_totals(ds, year, month):
    live = [v["live"] for d, v in ds.items() if (d.year, d.month) == (year, month) and v["live"] is not None]
    paper = [v["paper"] for d, v in ds.items() if (d.year, d.month) == (year, month) and v["paper"] is not None]
    return {"live_net": round(sum(live), 2), "live_days": len(live), "win_days": sum(1 for x in live if x > 0),
            "paper_net": round(sum(paper), 2), "paper_days": len(paper)}


def money(v, inr=None):
    s = f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"
    return s + (f" (₹{'−' if v < 0 else ''}{abs(v) * inr:,.0f})" if inr else "")


def day_message(d: date, v, ds, inr=INR_DEFAULT):
    """The one daily Telegram message. Results only."""
    head = f"{WEEKDAY[d.weekday()]} {d.day} {d:%b}"
    mt = month_totals(ds, d.year, d.month)
    tail = f"{d:%B} so far: {money(mt['live_net'])} live ({mt['live_days']} days)"
    if v is None:
        return f"{head} · No trade today"
    if v["live"] is not None and v["closed_early"]:
        sh = f"\nBot's result if held: {money(v['shadow'])}" if v["shadow"] is not None else ""
        return f"{head} · Live · {v['lots']} lots · You closed early: {money(v['live'], inr)}{sh}\n{tail}"
    if v["live"] is not None:
        return f"{head} · Live · {v['lots']} lots · {money(v['live'], inr)}\n{tail}"
    if v["paper"] is not None:
        return (f"{head} · Paper (not in your account) · {v['lots']} lots · {money(v['paper'])}\n"
                f"{d:%B} live so far: {money(mt['live_net'])}")
    return f"{head} · No trade today"
