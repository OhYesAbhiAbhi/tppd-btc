"""BTC P&L calendar page (read-only), same look as the Nifty P&L calendar.

Reads the bot's own trade logs and serves a month calendar of net P&L in USD (with INR):
  live   ~/btc_bot/data_condor/trades.csv          -> "BTC strangle" (counted)
  paper  ~/btc_bot/data_reentry/trades.csv         -> "Re-entry (paper)"   (greyed, not counted)
  paper  ~/btc_bot/data_thursday/thursday.csv      -> "Thu A/B/C (paper)"  (greyed, not counted)
  manual ~/btc_bot/data_manual/trades.csv          -> "Manual trades"      (counted; added with --add-manual)
Profit is counted on the IST day a trade closed (settlement = expiry day, 17:30).

    python btc_pnl.py                 # serve on 127.0.0.1:$BTC_PNL_PORT (default 8095); Caddy maps /btc/ to it
    python btc_pnl.py --html 2026-11  # print one month's page (no server)
    python btc_pnl.py --add-manual "2026-11-05 09:15" "2026-11-05 17:30" 200 23.40 "0.20 strangle 78000P/80000C"
                      [--closed "2026-11-05 14:10"]   # a manual trade (net USD after fees); counted on the page
    python btc_pnl.py --list-manual   /   --undo-manual   (remove the last manual row)
Open: https://<domain>/btc/calendar?key=<BTC_PNL_KEY>&m=YYYY-MM
"""
import argparse
import calendar
import csv
import html
import hmac
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

try:
    import config  # noqa: F401  (loads ~/btc_bot/.env)
except Exception:
    pass

IST = ZoneInfo("Asia/Kolkata")
HERE = Path(__file__).resolve().parent
KEY = os.environ.get("BTC_PNL_KEY", "")
PORT = int(os.environ.get("BTC_PNL_PORT", "8095"))
INR = float(os.environ.get("BTC_PNL_INR", "85"))
SOURCES = [  # (path, default label, short label, live?)
    (os.environ.get("BTC_PNL_LIVE", str(HERE / "data_condor" / "trades.csv")), "BTC strangle", "strgl", True),
    (os.environ.get("BTC_PNL_REENTRY", str(HERE / "data_reentry" / "trades.csv")), "Re-entry (paper)", "re-ent", False),
    (os.environ.get("BTC_PNL_THU", str(HERE / "data_thursday" / "thursday.csv")), "Thu paper", "Thu", False),
    (os.environ.get("BTC_PNL_MANUAL", str(HERE / "data_manual" / "trades.csv")), "Manual trades", "manual", True),
]
THU_NAMES = {"straddle_hold": ("Thu A straddle hold (paper)", "Thu A"),
             "strangle30_hold": ("Thu B 0.30 strangle (paper)", "Thu B"),
             "straddle_2x": ("Thu C straddle 2x (paper)", "Thu C")}
STATE = os.environ.get("BTC_PNL_STATE", str(HERE / "data_condor" / "state.json"))


def _dt(s):
    try:
        return datetime.strptime(s.strip()[:16], "%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return None


def close_day(row):
    """IST date the trade closed: an early exit's 'at HH:MM' time after entry, else the expiry day."""
    ent, exp = _dt(row.get("entry_ist", "")), _dt(row.get("expiry", ""))
    ex = _dt(row.get("exit_ist", "") or "")
    if ex:
        return ex.date()
    if exp is None:
        return ent.date() if ent else None
    m = re.search(r"\bat (\d{2}):(\d{2})\b", row.get("note", "") or "")
    if m and ent:
        t = (int(m.group(1)), int(m.group(2)))
        same_day = ent.date() == exp.date()
        if not same_day and t >= (ent.hour, ent.minute):
            return ent.date()
        return exp.date()
    return exp.date()


def exit_tag(row):
    """(short exit label, IST time) from the trade's note: 25% TP / Full / Vol exit / Stop, or the paper equivalents."""
    note = (row.get("note") or "").lower()
    m = re.search(r"\bat (\d{2}:\d{2})\b", note)
    t = m.group(1) if m else ""
    if note.startswith("take profit"):
        return "25% TP", t
    if note.startswith("vol exit"):
        return "Vol exit", t
    if note.startswith("stop"):
        return "Stop", t
    if note.startswith("closed"):
        return "Closed", t
    if note.startswith("sold at"):
        mult = re.match(r"sold at ([\d.]+)x", note)
        return (f"Sold {mult.group(1)}x" if mult else "Sold"), t
    exp = _dt(row.get("expiry", ""))
    return "Full", (exp.strftime("%H:%M") if exp else "17:30")


def load():
    out = []
    for path, label, short, live in SOURCES:
        p = Path(path)
        if not p.exists():
            continue
        with p.open() as f:
            for r in csv.DictReader(f):
                try:
                    net = float(r.get("net_usd") or 0)
                except ValueError:
                    continue
                d = close_day(r)
                if d is None:
                    continue
                lab, sh = label, short
                if "strategy" in r:
                    lab, sh = THU_NAMES.get(r["strategy"], (f"Thu {r['strategy']} (paper)", "Thu"))
                is_live = live and (r.get("mode", "live") == "live")
                if live and not is_live:
                    lab, sh = "BTC strangle (paper)", "strgl p"
                note = (r.get("note") or "").split(";")[0]
                ent = _dt(r.get("entry_ist", ""))
                how, when = exit_tag(r)
                out.append({"day": d, "net": net, "label": lab, "short": sh, "live": is_live,
                            "lots": r.get("lots", ""), "note": note, "entry": ent.strftime("%H:%M") if ent else "",
                            "entry_day": ent.date() if ent else None, "how": how, "when": when})
    return out


def usd(v, sign=True):
    s = f"{abs(v):,.2f}"
    return (("+" if v > 0 else "−" if v < 0 else "") if sign else ("−" if v < 0 else "")) + "$" + s


def inr(v):
    return ("+" if v > 0 else "−" if v < 0 else "") + "₹" + f"{abs(v) * INR:,.0f}"


def money(v, cur):
    return inr(v) if cur == "inr" else usd(v)


def other(v, cur):
    return usd(v) if cur == "inr" else inr(v)


def col(v):
    return "var(--pos)" if v > 0 else "var(--neg)" if v < 0 else "var(--ink)"


CSS = """
:root{--bg:#f4f5f7;--card:#fff;--ink:#1d2330;--mut:#6b7385;--line:#e3e6ec;--pos:#0b7a43;--neg:#c0392b;
--posbg:#e7f6ee;--negbg:#fdecea;--wk:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#121418;--card:#1c1f25;--ink:#e8eaee;--mut:#9aa1ad;--line:#2c3038;
--pos:#4cc38a;--neg:#ff7b6b;--posbg:#16301f;--negbg:#3a1d1a;--wk:#181b20}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.35 -apple-system,Segoe UI,Roboto,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.top{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:12px}
h1{font-size:20px;margin:0}.nav a{display:inline-block;padding:6px 12px;border:1px solid var(--line);border-radius:8px;
color:var(--ink);text-decoration:none;background:var(--card);margin-left:4px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.k{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.k .l{font-size:12px;color:var(--mut)}
.k .v{font-size:18px;font-weight:700;margin-top:2px}.k .x{font-size:12px;color:var(--mut);margin-top:1px}
.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}
.dow{font-size:12px;color:var(--mut);text-align:center;padding:2px 0}
.c{background:var(--card);border:1px solid var(--line);border-radius:10px;min-height:92px;padding:6px 7px;overflow:hidden}
.c.wk{background:var(--wk)}.c.out{visibility:hidden}.c.pos{background:var(--posbg)}.c.neg{background:var(--negbg)}
.c.today{outline:2px solid #3b6fd8}
.d{font-size:12px;color:var(--mut);display:flex;justify-content:space-between}
.t{font-weight:700;font-size:15px;margin:2px 0 3px}
.s{font-size:11.5px;display:flex;justify-content:space-between;gap:4px;border-top:1px dashed var(--line);padding-top:2px;margin-top:2px}
.s span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--mut);min-width:0}
.s b{font-weight:400}.sn{display:none}.v{white-space:nowrap}.t{white-space:nowrap}
.pos .t,.s .p{color:var(--pos)}.neg .t,.s .n{color:var(--neg)}
.paper{opacity:.55;font-style:italic}
.m{font-size:10.5px;color:var(--mut);line-height:1.25;margin-top:1px}.m i{font-style:normal;font-weight:600}
.m .tp{color:var(--pos)}.m .st{color:var(--neg)}
.tbl{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;margin-top:14px;font-size:13px}
.tbl th,.tbl td{padding:7px 10px;border-top:1px solid var(--line);text-align:left}.tbl td.r,.tbl th.r{text-align:right}
.note{color:var(--mut);font-size:12px;margin-top:10px}
.cur{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-left:4px;vertical-align:middle}
.cur a{margin:0;border:0;border-radius:0;padding:6px 10px}.cur a.on{background:var(--ink);color:var(--bg);font-weight:700}
@media (max-width:640px){.wrap{padding:10px}.kpis{grid-template-columns:repeat(2,1fr)}
.grid{gap:4px}.c{min-height:74px;padding:5px;border-radius:7px}.t{font-size:12px}.d{font-size:11px}.dow{font-size:11px}
.s{display:block;font-size:10.5px}.s span{display:block}.fn{display:none}.sn{display:inline}}
"""


def open_line():
    try:
        st = json.loads(Path(STATE).read_text())
        pos = st.get("open")
    except (OSError, ValueError):
        return ""
    if not pos:
        return ""
    return (f"Open now: strangle x{pos.get('lots')} for the {pos.get('expiry')} expiry "
            f"(sold {pos['legs']['put_short']['strike']:.0f}P / {pos['legs']['call_short']['strike']:.0f}C). ")


def page(month, key, now=None, cur="usd"):
    cur = "inr" if cur == "inr" else "usd"
    now = now or datetime.now(IST)
    y, m = month
    rows = load()
    first = date(y, m, 1)
    last = date(y, m, calendar.monthrange(y, m)[1])
    inm = [r for r in rows if first <= r["day"] <= last]
    live_m = [r for r in inm if r["live"]]
    by_day = {}
    for r in inm:
        by_day.setdefault(r["day"], []).append(r)
    live_days = {}
    for r in live_m:
        live_days[r["day"]] = live_days.get(r["day"], 0) + r["net"]
    month_tot = sum(live_days.values())
    all_live = sum(r["net"] for r in rows if r["live"])
    wins = sum(v > 0 for v in live_days.values())
    prev = (first - timedelta(days=1)).strftime("%Y-%m")
    nxt = (last + timedelta(days=1)).strftime("%Y-%m")
    k = html.escape(key) + ("&amp;cur=inr" if cur == "inr" else "")
    kk = html.escape(key)
    mm = f"{y:04d}-{m:02d}"
    h = [f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
         f'<title>BTC P&amp;L calendar</title><style>{CSS}</style></head><body><div class="wrap">',
         f'<div class="top"><h1>BTC P&amp;L · {first:%B %Y}</h1><div class="nav">'
         f'<a href="?key={k}&amp;m={prev}">‹ {datetime.strptime(prev, "%Y-%m"):%b}</a>'
         f'<a href="?key={k}&amp;m={now:%Y-%m}">Today</a>'
         f'<a href="?key={k}&amp;m={nxt}">{datetime.strptime(nxt, "%Y-%m"):%b} ›</a>'
         f'<span class="cur"><a class="{"on" if cur == "usd" else ""}" href="?key={kk}&amp;m={mm}">$</a>'
         f'<a class="{"on" if cur == "inr" else ""}" href="?key={kk}&amp;m={mm}&amp;cur=inr">₹</a></span></div></div>',
         '<div class="kpis">',
         f'<div class="k"><div class="l">This month</div><div class="v" style="color:{col(month_tot)}">{money(month_tot, cur)}</div>'
         f'<div class="x">{other(month_tot, cur)}</div></div>',
         f'<div class="k"><div class="l">Trading days</div><div class="v">{len(live_days)}</div></div>',
         f'<div class="k"><div class="l">Winning days</div><div class="v">{wins}/{len(live_days)}</div></div>',
         f'<div class="k"><div class="l">All time (live)</div><div class="v" style="color:{col(all_live)}">{money(all_live, cur)}</div>'
         f'<div class="x">{other(all_live, cur)}</div></div></div>',
         '<div class="grid">'] + [f'<div class="dow">{d}</div>' for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    for _ in range(first.weekday()):
        h.append('<div class="c out"></div>')
    for dnum in range(1, last.day + 1):
        d = date(y, m, dnum)
        items = by_day.get(d, [])
        tot = live_days.get(d)
        cls = ["c"]
        if tot is not None:
            cls.append("pos" if tot > 0 else "neg" if tot < 0 else "")
        if d == now.date():
            cls.append("today")
        cell = [f'<div class="{" ".join(c for c in cls if c)}"><div class="d"><span>{dnum}</span></div>']
        if tot is not None:
            cell.append(f'<div class="t">{money(tot, cur)}</div>')
        for r in sorted(items, key=lambda r: not r["live"]):
            pc = "p" if r["net"] > 0 else "n" if r["net"] < 0 else ""
            tip = html.escape(f'{r["label"]} x{r["lots"]}: {r["note"]}')
            cell.append(f'<div class="s{"" if r["live"] else " paper"}"><span title="{tip}"><b class="fn">'
                        f'{html.escape(r["label"])}</b><b class="sn">{html.escape(r["short"])}</b></span>'
                        f'<span class="v {pc}">{money(r["net"], cur)}</span></div>')
            hc = "tp" if r["how"] in ("25% TP", "Full") or r["how"].startswith("Sold") else (
                "st" if r["how"] == "Stop" else "")
            prev = "" if r["entry_day"] is None or r["entry_day"] == d else f'{r["entry_day"]:%d %b} '
            cell.append(f'<div class="m{"" if r["live"] else " paper"}">x{html.escape(str(r["lots"]))} · in '
                        f'{prev}{r["entry"]} · <i class="{hc}">{html.escape(r["how"])}</i>'
                        f'{" " + r["when"] if r["when"] else ""}</div>')
        cell.append("</div>")
        h.append("".join(cell))
    for _ in range((7 - (first.weekday() + last.day) % 7) % 7):
        h.append('<div class="c out"></div>')
    h.append("</div>")
    strat, exits = {}, {}
    for r in inm:
        s = strat.setdefault(r["label"], [0, 0, 0.0, r["live"]])
        s[0] += 1
        s[1] += r["net"] > 0
        s[2] += r["net"]
        e = exits.setdefault(r["label"], {})
        e[r["how"]] = e.get(r["how"], 0) + 1
    h.append(f'<table class="tbl"><tbody><tr><th>Strategy ({first:%b})</th><th class="r">Trades</th><th class="r">Wins</th>'
             f'<th>Exits</th><th class="r">P&amp;L</th></tr>')
    for lab, (n, w, tot, lv) in sorted(strat.items(), key=lambda x: (not x[1][3], x[0])):
        order = ["Full", "25% TP", "Vol exit", "Stop"]
        ex = sorted(exits[lab].items(), key=lambda x: (order.index(x[0]) if x[0] in order else 9, x[0]))
        ext = " · ".join(f"{html.escape(a)} {b}" for a, b in ex)
        h.append(f'<tr{"" if lv else " class=paper"}><td>{html.escape(lab)}</td><td class="r">{n}</td>'
                 f'<td class="r">{w}/{n}</td><td>{ext}</td>'
                 f'<td class="r" style="color:{col(tot)}">{money(tot, cur)}</td></tr>')
    if not strat:
        h.append('<tr><td colspan="5">No closed trades this month.</td></tr>')
    h.append("</tbody></table>")
    h.append(f'<div class="note">{html.escape(open_line())}Net P&amp;L (traded in USD) from the bot\'s own trade log: fill prices '
             f'minus Delta fees as the bot calculates them (0.01% of notional capped at 3.5% of premium, +18% GST), '
             f'not Delta\'s account statement. ₹ shown at {INR:g} per $ (BTC_PNL_INR); switch with $ / ₹ at the top. Under each trade: lots · entry time (IST; date shown if the trade was entered the day before) · how it closed: Full = held to the 17:30 settlement, 25% TP = take profit on a volatile night, Vol exit = 60-min volatility exit on a calm night, Stop = stop loss. Profit is counted on the IST day a trade closed '
             f'(settlement = expiry day 17:30). Thursdays are skipped by the live bot. Paper trades are greyed and not '
             f'counted. Updated {now:%d %b %H:%M}.</div></div></body></html>')
    return "\n".join(h)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/plain; charset=utf-8"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.rstrip("/") not in ("/calendar", "/btc/calendar"):
            return self.send(404, "not found")
        k = (q.get("key") or [""])[0]
        if not KEY or not hmac.compare_digest(k, KEY):
            return self.send(403, "forbidden")
        now = datetime.now(IST)
        try:
            y, m = (int(x) for x in (q.get("m") or [now.strftime("%Y-%m")])[0].split("-"))
            date(y, m, 1)
        except (ValueError, TypeError):
            y, m = now.year, now.month
        cur = (q.get("cur") or ["usd"])[0].lower()
        self.send(200, page((y, m), k, now, cur), "text/html; charset=utf-8")


MANUAL_COLS = ["mode", "style", "entry_ist", "expiry", "exit_ist", "lots", "net_usd", "note"]


def manual_path():
    return Path(SOURCES[3][0])


def add_manual(entry, expiry, lots, net, desc, closed=None):
    for v in (entry, expiry) + ((closed,) if closed else ()):
        if _dt(v) is None:
            raise ValueError(f"bad date/time {v!r}: use YYYY-MM-DD HH:MM (IST)")
    lots, net = int(lots), float(net)
    desc = (desc or "").replace(";", ",").replace("\n", " ").strip()
    note = (f"closed at {_dt(closed):%H:%M}; " if closed else "held to expiry; ") + desc
    p = manual_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists() or p.stat().st_size == 0
    with p.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(MANUAL_COLS)
        w.writerow(["live", "manual", _dt(entry).strftime("%Y-%m-%d %H:%M"), _dt(expiry).strftime("%Y-%m-%d %H:%M"),
                    _dt(closed).strftime("%Y-%m-%d %H:%M") if closed else "", lots, f"{net:.2f}", note])
    return note


def manual_rows():
    p = manual_path()
    if not p.exists():
        return []
    with p.open() as f:
        return list(csv.reader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", metavar="YYYY-MM")
    ap.add_argument("--add-manual", nargs=5, metavar=("ENTRY", "EXPIRY", "LOTS", "NET_USD", "DESC"))
    ap.add_argument("--closed", metavar="'YYYY-MM-DD HH:MM'", help="with --add-manual: when you closed it early (IST)")
    ap.add_argument("--list-manual", action="store_true")
    ap.add_argument("--undo-manual", action="store_true")
    a = ap.parse_args()
    if a.add_manual:
        note = add_manual(*a.add_manual, closed=a.closed)
        print(f"Added manual trade: x{a.add_manual[2]}, net {float(a.add_manual[3]):+.2f} USD, {note}")
        return
    if a.list_manual or a.undo_manual:
        rows = manual_rows()
        if a.undo_manual and len(rows) > 1:
            gone = rows.pop()
            with manual_path().open("w", newline="") as f:
                csv.writer(f).writerows(rows)
            print("Removed:", ", ".join(gone))
        for r in rows[1:]:
            print(", ".join(r))
        if len(rows) <= 1:
            print("No manual trades.")
        return
    if a.html:
        y, m = (int(x) for x in a.html.split("-"))
        print(page((y, m), KEY or "KEY"))
        return
    if not KEY:
        sys.exit("BTC_PNL_KEY not set in ~/btc_bot/.env")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
