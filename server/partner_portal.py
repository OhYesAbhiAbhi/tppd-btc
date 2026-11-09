"""Partner portal: sign-up, each partner's private page, and the owner's master page.

Served on 127.0.0.1:8096 behind Caddy at https://3-106-111-8.sslip.io/p/... and /master (phase 2b wiring).
The portal stores Delta keys ONLY as browser-made ciphertext (see partner_vault.py); it cannot decrypt them.
Partner controls are written to ~/btc_partners/<slug>/control.json, which that partner's condor instance
reads before every entry (CONDOR_CONTROL_FILE). Results are read from ~/btc_partners/<slug>/data/trades.csv.

    python partner_portal.py serve
    python partner_portal.py set-master-password [PW]
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from calendar import monthrange
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import partner_results as pr

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(os.environ.get("PARTNER_ROOT", str(Path.home() / "btc_partners")))
PUBKEY = Path(os.environ.get("PARTNER_VAULT_PUB", "/etc/tppd/vault.pub"))
PORT = int(os.environ.get("PARTNER_PORT", "8096"))
SERVER_IP = "3.106.111.8"
INR = float(os.environ.get("PARTNER_INR", "85"))
LOCK = threading.RLock()


def now():
    return time.time()


def today():
    return datetime.fromtimestamp(now(), IST).date()


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


# ------------------------------------------------------------------ store
SCHEMA = """CREATE TABLE IF NOT EXISTS partners (
 id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE, name TEXT, contact TEXT, token TEXT UNIQUE,
 pw_salt TEXT, pw_hash TEXT, envelope TEXT, status TEXT, lots INTEGER, tg_code TEXT, tg_chat TEXT,
 created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS log (ts REAL, slug TEXT, who TEXT, what TEXT);"""


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        os.chmod(path, 0o600)

    def q(self, sql, args=()):
        with LOCK:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return [dict(r) for r in cur.fetchall()]

    def get(self, k, default=None):
        r = self.q("SELECT v FROM settings WHERE k=?", (k,))
        return r[0]["v"] if r else default

    def put(self, k, v):
        self.q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def partner(self, **kw):
        (k, v), = kw.items()
        r = self.q(f"SELECT * FROM partners WHERE {k}=?", (v,))
        return r[0] if r else None

    def log(self, slug, who, what):
        self.q("INSERT INTO log VALUES(?,?,?,?)", (now(), slug, who, what))


def pw_hash(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt, hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def pw_ok(pw, salt, h):
    return bool(salt and h) and hmac.compare_digest(pw_hash(pw, salt)[1], h)


# ------------------------------------------------------------------ partner files
def pdir(slug):
    return ROOT / slug


def control(slug):
    try:
        return json.loads((pdir(slug) / "control.json").read_text())
    except (OSError, ValueError):
        return {"lots": 0, "live": False, "paper_dates": []}


def write_control(slug, c):
    """Atomic write; the condor instance reads this before each entry."""
    d = pdir(slug)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "control.json.tmp"
    tmp.write_text(json.dumps(c, indent=1))
    os.chmod(tmp, 0o644)
    tmp.replace(d / "control.json")


def position_open(slug):
    try:
        return bool((json.loads((pdir(slug) / "data" / "state.json").read_text()) or {}).get("open"))
    except (OSError, ValueError):
        return False


def results(slug):
    return pr.days(pdir(slug) / "data" / "trades.csv")


# ------------------------------------------------------------------ actions
NAME_RE = re.compile(r"[\w .'-]{2,40}")


def signup(st, form):
    g = lambda k: (form.get(k) or [""])[0].strip()
    name, contact, pw, env = re.sub(r"\s+", " ", g("name")), g("contact"), g("pw"), g("envelope")
    if not hmac.compare_digest(g("c"), st.get("invite", "-")):
        return False, "This sign-up link is not valid any more. Ask Abhineet for a new one."
    if not NAME_RE.fullmatch(name):
        return False, "Please enter your name."
    if not 3 <= len(contact) <= 80:
        return False, "Please enter a phone number or email."
    if len(pw) < 6:
        return False, "Choose a password or PIN of at least 6 characters."
    try:
        lots = int(g("lots"))
        e = json.loads(env)
        assert e.get("v") == 1 and all(isinstance(e.get(k), str) and e[k] for k in ("wk", "iv", "ct"))
        assert len(env) < 4000
    except (ValueError, AssertionError, AttributeError):
        return False, "The key could not be encrypted in your browser. Please use a recent Chrome or Safari."
    if not 1 <= lots <= 100000:
        return False, "Lots must be a number from 1 up."
    if g("ack") != "yes":
        return False, "Please tick the box to confirm you understand the risk."
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "partner"
    slug, i = base, 2
    while st.partner(slug=slug):
        slug, i = f"{base}-{i}", i + 1
    salt, h = pw_hash(pw)
    token, code = secrets.token_urlsafe(18), secrets.token_urlsafe(9)
    st.q("INSERT INTO partners(slug,name,contact,token,pw_salt,pw_hash,envelope,status,lots,tg_code,created,updated)"
         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (slug, name, contact, token, salt, h, env, "pending", lots, code, now(), now()))
    st.log(slug, "partner", f"signed up, {lots} lots")
    return True, token


def partner_action(st, p, form):
    """Lots / Live-Off / paper day / close now, from the partner's own page (password checked by caller)."""
    g = lambda k: (form.get(k) or [""])[0].strip()
    a, slug = g("a"), p["slug"]
    if p["status"] != "active":
        return "Your account is not active yet."
    c = control(slug)
    if a == "lots":
        try:
            n = int(g("lots"))
        except ValueError:
            return "Lots must be a whole number."
        if n < 1:
            return "Lots must be at least 1."
        c["lots"] = n
        st.q("UPDATE partners SET lots=?, updated=? WHERE slug=?", (n, now(), slug))
        msg = f"Lots set to {n} from the next entry." + (" Your open trade keeps its size." if position_open(slug) else "")
    elif a in ("on", "off"):
        c["live"] = a == "on"
        msg = ("Trading is ON (live) from the next entry." if a == "on" else
               "Trading is OFF from the next entry: the bot keeps running on paper. An open trade runs to its normal exit.")
    elif a == "paperday":
        d = g("day")
        try:
            dd = date.fromisoformat(d)
        except ValueError:
            return "Pick a date."
        if dd < today():
            return "That date has passed."
        days_ = set(c.get("paper_dates") or [])
        days_.symmetric_difference_update({d})
        c["paper_dates"] = sorted(days_)
        msg = f"{dd:%d %b}: " + ("paper day." if d in days_ else "back to normal.")
    elif a == "close":
        if not position_open(slug):
            return "You have no open trade."
        c["close_now"] = secrets.token_hex(6)
        msg = "Closing your trade now. The day will show your result and the bot's result if held."
    else:
        return "Unknown action."
    write_control(slug, c)
    st.log(slug, "partner", a + (" " + g("lots") if a == "lots" else "") + (" " + g("day") if a == "paperday" else ""))
    return msg


def approve(st, slug, activate=None):
    p = st.partner(slug=slug)
    if not p or p["status"] != "pending":
        return "Not waiting for approval."
    write_control(slug, {"lots": p["lots"], "live": True, "paper_dates": []})
    ok, why = (activate or activate_instance)(slug)
    if not ok:
        return f"Could not start {p['name']}'s bot: {why}"
    st.q("UPDATE partners SET status='active', updated=? WHERE slug=?", (now(), slug))
    st.log(slug, "owner", "approved")
    return f"{p['name']} approved and started ({p['lots']} lots, live)."


def revoke(st, slug, deactivate=None):
    p = st.partner(slug=slug)
    if not p or p["status"] not in ("pending", "active", "suspended"):
        return "Nothing to revoke."
    (deactivate or deactivate_instance)(slug)
    st.q("UPDATE partners SET status='revoked', envelope=NULL, updated=? WHERE slug=?", (now(), slug))
    st.log(slug, "owner", "revoked")
    return f"{p['name']} revoked: bot stopped, encrypted key deleted." + (
        " Their trade was open: tell them to close it in the Delta app." if position_open(slug) else "")


def activate_instance(slug):
    """Phase 2b: hands the envelope to the root-only vault helper, which seals the key as a systemd
    credential and starts tppd-partner@<slug>. Until that is installed, approval is refused."""
    helper = "/usr/local/sbin/tppd-partner-activate"
    if not os.path.exists(helper):
        return False, "the vault helper is not installed yet (phase 2b)"
    import subprocess
    r = subprocess.run(["sudo", "-n", helper, slug], capture_output=True, text=True, timeout=120)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-200:]


def deactivate_instance(slug):
    helper = "/usr/local/sbin/tppd-partner-deactivate"
    if os.path.exists(helper):
        import subprocess
        subprocess.run(["sudo", "-n", helper, slug], capture_output=True, timeout=120)


# ------------------------------------------------------------------ pages
CSS = """:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--mute:#5d6672;--line:#e3e6ea;--acc:#1f6feb;--good:#1a7f37;--bad:#cf222e;--soft:#eef4ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e6e9ed;--mute:#9aa4ae;--line:#2a3038;--acc:#4c8dff;--good:#3fb950;--bad:#f85149;--soft:#16233a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 48px}h1{font-size:1.4rem;margin:.2em 0}h2{font-size:1.05rem;margin:0 0 .4em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 14px}.mute{color:var(--mute)}
label{display:block;font-weight:600;font-size:.9rem;margin:12px 0 4px}input,select{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
button{border:0;border-radius:8px;padding:9px 14px;font:600 .9rem system-ui,sans-serif;background:var(--acc);color:#fff;cursor:pointer}button.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}button.bad{background:var(--bad)}
.msg{padding:10px 12px;border-radius:8px;margin:0 0 14px;background:var(--soft)}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:end}.row>*{flex:1;min-width:120px}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}.kpi div{background:var(--soft);border-radius:10px;padding:10px}.kpi b{display:block;font-size:1.2rem}
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;font-size:.78rem}.cal div{min-height:58px;border:1px solid var(--line);border-radius:8px;padding:4px}
.pos{color:var(--good)}.neg{color:var(--bad)}.paper{opacity:.55}table{width:100%;border-collapse:collapse;font-size:.88rem}td,th{padding:6px;border-bottom:1px solid var(--line);text-align:left}"""


def page(title, body):
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=robots content=noindex><title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


JOIN_JS = """
async function seal(e){e.preventDefault();const f=e.target,m=document.getElementById('m');
try{const pub=await crypto.subtle.importKey('spki',Uint8Array.from(atob(f.dataset.pub),c=>c.charCodeAt(0)),{name:'RSA-OAEP',hash:'SHA-256'},false,['encrypt']);
const aes=await crypto.subtle.generateKey({name:'AES-GCM',length:256},true,['encrypt']);const iv=crypto.getRandomValues(new Uint8Array(12));
const data=new TextEncoder().encode(JSON.stringify({key:f.dkey.value.trim(),secret:f.dsecret.value.trim()}));
const ct=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv},aes,data));const raw=new Uint8Array(await crypto.subtle.exportKey('raw',aes));
const wk=new Uint8Array(await crypto.subtle.encrypt({name:'RSA-OAEP'},pub,raw));const b=a=>btoa(String.fromCharCode(...a));
f.envelope.value=JSON.stringify({v:1,wk:b(wk),iv:b(iv),ct:b(ct)});f.dkey.value='';f.dsecret.value='';f.dkey.disabled=true;f.dsecret.disabled=true;f.submit();}
catch(err){m.textContent='Your browser could not encrypt the key. Please use a recent Chrome or Safari.';}}"""


def join_page(st, code, msg=""):
    pub = PUBKEY.read_text().strip() if PUBKEY.exists() else ""
    if not pub:
        return page("Not ready", "<h1>Sign-up is not open yet</h1>")
    return page("Join", f"""<h1>Join the BTC night bot</h1><p class=mute>Your own Delta account and money. You see only your daily results.</p>
<div class=msg id=m>{esc(msg)}</div>
<div class=card><h2>Before you start</h2><ol><li>In Delta Exchange India: Settings &rarr; API keys &rarr; create a key with <b>Read</b> and <b>Trading</b>, and whitelist IP <b>{SERVER_IP}</b>.</li>
<li>Keep enough margin in Delta for the lots you pick; your page suggests how much.</li></ol></div>
<form class=card method=post action="/p/join" onsubmit="seal(event)" data-pub="{esc(pub)}" autocomplete=off>
<input type=hidden name=c value="{esc(code)}"><input type=hidden name=envelope>
<label>Your name</label><input name=name required maxlength=40><label>Phone or email</label><input name=contact required maxlength=80>
<label>Lots to trade</label><input name=lots inputmode=numeric required>
<label>Delta API key</label><input name=dkey required spellcheck=false autocapitalize=off>
<label>Delta API secret</label><input name=dsecret type=password required spellcheck=false autocapitalize=off>
<label>Choose a password or PIN for your page (6+ characters)</label><input name=pw type=password required minlength=6>
<label style="font-weight:400"><input type=checkbox name=ack value=yes required style="width:auto"> I understand this sells options with my own money, a night can lose more than the stop, and I can stop it any time.</label>
<p><button>Send request</button></p><p class=mute>Your key and secret are encrypted on this device before they are sent.</p></form>
<script>{JOIN_JS}</script>""")


def money_cls(v):
    return "pos" if v >= 0 else "neg"


def partner_page(st, p, y, m, msg=""):
    ds = results(p["slug"])
    mt = pr.month_totals(ds, y, m)
    alltime = round(sum(v["live"] for v in ds.values() if v["live"] is not None), 2)
    c = control(p["slug"])
    first = date(y, m, 1).weekday()
    cells = ["<div class=mute></div>"] * first
    for dnum in range(1, monthrange(y, m)[1] + 1):
        d = date(y, m, dnum)
        v = ds.get(d)
        body = ""
        if v:
            if v["live"] is not None:
                body = f"<span class={money_cls(v['live'])}>{pr.money(v['live'])}</span><br>{v['lots']} lots"
                if v["closed_early"] and v["shadow"] is not None:
                    body += f"<br><span class=mute>held: {pr.money(v['shadow'])}</span>"
            elif v["paper"] is not None:
                body = f"<span class=paper>{pr.money(v['paper'])}<br>paper</span>"
        elif d.isoformat() in (c.get("paper_dates") or []):
            body = "<span class=mute>paper day</span>"
        cells.append(f"<div><b>{dnum}</b><br>{body}</div>")
    prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
    next_m = (y, m + 1) if m < 12 else (y + 1, 1)
    base = f"/p/{p['token']}"
    state = "ON (live)" if c.get("live", True) else "OFF (paper)"
    form = lambda a, inner, cls="": (f"<form method=post action='{base}/do' class=row><input type=hidden name=a value={a}>{inner}"
                                    f"<input name=pw type=password placeholder='password / PIN' required><button class='{cls}'>Save</button></form>")
    ctrl = "" if p["status"] != "active" else f"""<div class=card><h2>Your controls</h2>
<p>Trading: <b>{state}</b> &middot; Lots: <b>{c.get('lots')}</b></p>
{form('lots', f"<input name=lots inputmode=numeric value='{esc(c.get('lots'))}'>")}
{form('off' if c.get('live', True) else 'on', '<span>' + ('Switch trading OFF (paper)' if c.get('live', True) else 'Switch trading ON (live)') + '</span>')}
{form('paperday', f"<input type=date name=day min='{today().isoformat()}'><span class=mute>tap again to undo</span>")}
{form('close', "<span>Close my open trade now</span>", 'bad') if position_open(p['slug']) else ''}
<p class=mute>Changes apply at the next entry. Each lot needs margin in your Delta account; keep a buffer for fast moves.</p></div>"""
    bot = st.get("tg_bot")
    tgcard = "" if not bot else (
        f"<div class=card><h2>Daily result on Telegram</h2>" + (
            "<p>Connected. You get one message a day.</p>" if p["tg_chat"] else
            f"<p><a href='https://t.me/{esc(bot)}?start={esc(p['tg_code'])}'>Open Telegram and press Start</a> "
            f"to get one message a day with your result.</p>") + "</div>")
    status = {"pending": "Waiting for approval.", "active": "", "suspended": "Paused by Abhineet.",
              "revoked": "Access ended."}.get(p["status"], "")
    return page("My results", f"""<h1>{esc(p['name'])}</h1>{f'<div class=msg>{esc(msg)}</div>' if msg else ''}{f'<div class=msg>{status}</div>' if status else ''}
<div class=card><div class=row><a href="{base}?m={prev_m[0]}-{prev_m[1]:02d}">&larr;</a><h2 style="text-align:center">{date(y, m, 1):%B %Y}</h2>
<a style="text-align:right" href="{base}?m={next_m[0]}-{next_m[1]:02d}">&rarr;</a></div>
<div class=kpi><div>Month (live)<b class={money_cls(mt['live_net'])}>{pr.money(mt['live_net'])}</b></div><div>Live days<b>{mt['live_days']}</b></div>
<div>Winning days<b>{mt['win_days']}</b></div><div>All-time live<b class={money_cls(alltime)}>{pr.money(alltime)}</b></div>
<div>Paper (not counted)<b class=paper>{pr.money(mt['paper_net'])}</b></div></div><br>
<div class=cal>{''.join(f'<div class=mute>{w}</div>' for w in pr.WEEKDAY)}{''.join(cells)}</div></div>{ctrl}{tgcard}""")


def master_page(st, msg=""):
    rows = []
    for p in st.q("SELECT * FROM partners ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, id"):
        ds = results(p["slug"])
        t = today()
        mt = pr.month_totals(ds, t.year, t.month)
        allt = round(sum(v["live"] for v in ds.values() if v["live"] is not None), 2)
        c = control(p["slug"])
        acts = ""
        if p["status"] == "pending":
            acts = f"<form method=post action='/master/do'><input type=hidden name=slug value='{esc(p['slug'])}'><button name=a value=approve>Approve</button></form>"
        if p["status"] in ("pending", "active", "suspended"):
            acts += (f"<form method=post action='/master/do' onsubmit=\"return confirm('Revoke {esc(p['name'])}?')\">"
                     f"<input type=hidden name=slug value='{esc(p['slug'])}'><button class=bad name=a value=revoke>Revoke</button></form>")
        rows.append(f"<tr><td><b>{esc(p['name'])}</b><br><span class=mute>{esc(p['contact'])}</span></td><td>{esc(p['status'])}"
                    f"{' &middot; ' + ('live' if c.get('live', True) else 'paper') if p['status'] == 'active' else ''}</td>"
                    f"<td>{esc(c.get('lots') or p['lots'])}</td><td class={money_cls(mt['live_net'])}>{pr.money(mt['live_net'])}</td>"
                    f"<td class={money_cls(allt)}>{pr.money(allt)}</td><td>{'open' if position_open(p['slug']) else ''}</td><td>{acts}</td></tr>")
    link = f"https://3-106-111-8.sslip.io/p/join?c={st.get('invite', '')}"
    return page("Master", f"""<h1>Partners</h1>{f'<div class=msg>{esc(msg)}</div>' if msg else ''}
<div class=card><table><tr><th>Partner</th><th>Status</th><th>Lots</th><th>Month live</th><th>All-time</th><th>Trade</th><th></th></tr>{''.join(rows) or '<tr><td colspan=7 class=mute>No partners yet.</td></tr>'}</table></div>
<div class=card><h2>Sign-up link</h2><input readonly value="{esc(link)}" onclick="this.select()">
<form method=post action='/master/do'><p><button class=ghost name=a value=newinvite>Make a new link (old one stops working)</button></p></form></div>""")


# ------------------------------------------------------------------ HTTP
HITS = {}


def allow(key, n, window):
    t = now()
    HITS[key] = [x for x in HITS.get(key, []) if t - x < window]
    if len(HITS[key]) >= n:
        return False
    HITS[key].append(t)
    return True


class Handler(BaseHTTPRequestHandler):
    st = None
    server_version = "tppd"
    sys_version = ""

    def log_message(self, *a):
        pass

    def ip(self):
        xf = self.headers.get("X-Forwarded-For", "")
        return xf.split(",")[-1].strip() if xf else self.client_address[0]

    def send(self, code, body, headers=()):
        b = body.encode()
        self.send_response(code)
        for k, v in [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(b))),
                     ("Cache-Control", "no-store"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                     ("X-Content-Type-Options", "nosniff"), *headers]:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        return parse_qs(self.rfile.read(min(n, 20000)).decode("utf-8", "replace"), keep_blank_values=True)

    def cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def master_ok(self):
        s = self.cookie("ms")
        try:
            exp, sig = s.split(".", 1)
            good = hmac.new(self.st.get("secret").encode(), f"m.{exp}".encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, good) and float(exp) > now()
        except (ValueError, AttributeError):
            return False

    def month(self, q):
        try:
            y, m = map(int, (q.get("m") or [""])[0].split("-"))
            date(y, m, 1)
            return y, m
        except ValueError:
            t = today()
            return t.year, t.month

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/p/join":
            code = (q.get("c") or [""])[0]
            if not hmac.compare_digest(code, self.st.get("invite", "-")):
                return self.send(403, page("Invite only", "<h1>Invite only</h1>"))
            return self.send(200, join_page(self.st, code))
        m = re.fullmatch(r"/p/([A-Za-z0-9_-]{20,40})", u.path)
        if m:
            p = self.st.partner(token=m.group(1))
            if not p or p["status"] == "revoked":
                return self.send(404, page("Not found", "<h1>Not found</h1>"))
            return self.send(200, partner_page(self.st, p, *self.month(q)))
        if u.path == "/master":
            if not self.master_ok():
                return self.send(200, page("Master", "<h1>Master</h1><form class=card method=post action='/master/login'>"
                                                      "<label>Password</label><input type=password name=pw autofocus><p><button>Log in</button></p></form>"))
            return self.send(200, master_page(self.st))
        return self.send(404, page("Not found", "<h1>Not found</h1>"))

    def do_POST(self):
        u = urlparse(self.path)
        f = self.form()
        g = lambda k: (f.get(k) or [""])[0]
        if u.path == "/p/join":
            if not allow(("join", self.ip()), 6, 3600):
                return self.send(429, page("Wait", "<h1>Too many tries; try again in an hour.</h1>"))
            ok, info = signup(self.st, f)
            if not ok:
                return self.send(200, join_page(self.st, g("c"), info))
            return self.send(200, page("Sent", f"<h1>Request sent</h1><div class=msg>Abhineet will review it. Your private page "
                                               f"(bookmark it): <a href='/p/{esc(info)}'>/p/{esc(info)}</a></div>"))
        m = re.fullmatch(r"/p/([A-Za-z0-9_-]{20,40})/do", u.path)
        if m:
            p = self.st.partner(token=m.group(1))
            if not p:
                return self.send(404, page("Not found", "<h1>Not found</h1>"))
            if not allow(("pw", p["slug"]), 8, 900):
                return self.send(429, page("Wait", "<h1>Too many tries; wait 15 minutes.</h1>"))
            t = today()
            if not pw_ok(g("pw"), p["pw_salt"], p["pw_hash"]):
                return self.send(200, partner_page(self.st, p, t.year, t.month, "Wrong password / PIN; nothing changed."))
            msg = partner_action(self.st, p, f)
            return self.send(200, partner_page(self.st, self.st.partner(token=m.group(1)), t.year, t.month, msg))
        if u.path == "/master/login":
            if not allow(("mlogin", self.ip()), 5, 900):
                return self.send(429, page("Wait", "<h1>Too many tries; wait 15 minutes.</h1>"))
            if pw_ok(g("pw"), self.st.get("m_salt"), self.st.get("m_hash")):
                exp = str(int(now()) + 12 * 3600)
                sig = hmac.new(self.st.get("secret").encode(), f"m.{exp}".encode(), hashlib.sha256).hexdigest()
                return self.send(200, master_page(self.st), [("Set-Cookie", f"ms={exp}.{sig}; Path=/master; HttpOnly; Secure; SameSite=Strict")])
            return self.send(403, page("Master", "<h1>Wrong password</h1>"))
        if u.path == "/master/do":
            if not self.master_ok():
                return self.send(403, page("Master", "<h1>Log in again</h1>"))
            a, slug = g("a"), g("slug")
            if a == "approve":
                msg = approve(self.st, slug)
            elif a == "revoke":
                msg = revoke(self.st, slug)
            elif a == "newinvite":
                self.st.put("invite", secrets.token_urlsafe(12))
                msg = "New sign-up link made."
            else:
                msg = "Unknown action."
            return self.send(200, master_page(self.st, msg))
        return self.send(404, page("Not found", "<h1>Not found</h1>"))


def open_store():
    st = Store(ROOT / "portal.db")
    if not st.get("secret"):
        st.put("secret", secrets.token_hex(32))
    if not st.get("invite"):
        st.put("invite", secrets.token_urlsafe(12))
    return st


def main(argv):
    st = open_store()
    if argv[:1] == ["set-master-password"]:
        pw = argv[1] if len(argv) > 1 else secrets.token_urlsafe(10)
        salt, h = pw_hash(pw)
        st.put("m_salt", salt)
        st.put("m_hash", h)
        print("Master password:", pw)
    elif argv[:1] == ["serve"]:
        Handler.st = st
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
