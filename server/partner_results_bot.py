"""Shared Telegram results bot for partners (one bot, private one-to-one chats).

- A partner opens https://t.me/<bot>?start=<their code> (shown on their page): their chat is linked.
- After PARTNER_SEND_AFTER (17:45 IST) each day: one message per linked active partner with that day's
  result (partner_results.day_message) - results only, never trade details or other partners.
- If a partner's bot service is not running while their trade is open: one safety alert.
- Commands: /today, /month. Anything from an unlinked chat gets a neutral reply.

    PARTNER_TG_TOKEN=... python partner_results_bot.py
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime

import requests

import partner_portal as pp
import partner_results as pr

SEND_AFTER = os.environ.get("PARTNER_SEND_AFTER", "17:45")
PUBLIC_URL = os.environ.get("PARTNER_PUBLIC_URL", "https://3-106-111-8.sslip.io").rstrip("/")


def dashboard(p):
    return f"{PUBLIC_URL}/p/{p['token']}"


def welcome_text(p):
    return (f"Welcome, {p['name']}! You're approved.\n\n"
            f"Your private dashboard (bookmark it, and don't share it):\n{dashboard(p)}\n\n"
            f"It shows your daily results, and lets you change lots or switch trading on/off. "
            f"You'll also get one message here each day with your result. Send /link any time to get the link again.")


def tg(token, method, **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=40)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(str(d.get("description"))[:200])
    return d.get("result")


def service_active(slug):
    r = subprocess.run(["systemctl", "is-active", f"tppd-partner@{slug}"], capture_output=True, text=True)
    return r.stdout.strip() == "active"


class ResultsBot:
    def __init__(self, st, token, send=None, active=service_active, now_fn=None):
        self.st, self.token = st, token
        self.send = send or (lambda chat, text: tg(token, "sendMessage", chat_id=chat, text=text))
        self.active = active
        self.now = now_fn or time.time

    def ist(self):
        return datetime.fromtimestamp(self.now(), pp.IST)

    def handle(self, u):
        m = u.get("message") or {}
        chat = (m.get("chat") or {})
        if chat.get("type") != "private":
            return
        cid, text = str(chat.get("id")), (m.get("text") or "").strip()
        p = next(iter(self.st.q("SELECT * FROM partners WHERE tg_chat=? AND status='active'", (cid,))), None)
        mt = re.fullmatch(r"/start\s+([A-Za-z0-9_-]{6,40})", text)
        if mt:
            q = self.st.partner(tg_code=mt.group(1))
            if not q or q["status"] not in ("pending", "active"):
                return self.send(cid, "This link is not valid. Use the Telegram link on your results page.")
            self.st.q("UPDATE partners SET tg_chat=?, updated=? WHERE slug=?", (cid, pp.now(), q["slug"]))
            self.st.log(q["slug"], "partner", "telegram linked")
            if q["status"] == "active":
                self.st.put(f"welcomed:{q['slug']}", "1")
                return self.send(cid, welcome_text(q))
            return self.send(cid, f"Linked, {q['name']}. Your request is waiting for approval; you'll get a message here "
                                  f"with your dashboard link once it's approved.")
        if not p:
            return self.send(cid, "This chat isn't linked. Use the Telegram link on your results page.")
        ds = pp.results(p["slug"])
        t = self.ist().date()
        if text.startswith("/today"):
            return self.send(cid, pr.day_message(t, ds.get(t), ds))
        if text.startswith("/link"):
            return self.send(cid, f"Your private dashboard (bookmark it, and don't share it):\n{dashboard(p)}")
        if text.startswith("/month"):
            mt_ = pr.month_totals(ds, t.year, t.month)
            return self.send(cid, f"{t:%B}: {pr.money(mt_['live_net'])} live over {mt_['live_days']} days "
                                  f"({mt_['win_days']} winning). Paper, not counted: {pr.money(mt_['paper_net'])}.")
        return self.send(cid, "Commands: /today, /month, /link")

    def welcome(self):
        """Partners approved after linking Telegram: send the dashboard link once."""
        n = 0
        for p in self.st.q("SELECT * FROM partners WHERE status='active' AND tg_chat IS NOT NULL"):
            key = f"welcomed:{p['slug']}"
            if self.st.get(key):
                continue
            try:
                self.send(p["tg_chat"], welcome_text(p))
            except Exception:
                continue                                     # retried on the next loop
            self.st.put(key, "1")
            n += 1
        return n

    def daily(self):
        t = self.ist()
        hh, mm = map(int, SEND_AFTER.split(":"))
        if (t.hour, t.minute) < (hh, mm):
            return 0
        day = t.date().isoformat()
        n = 0
        for p in self.st.q("SELECT * FROM partners WHERE status='active' AND tg_chat IS NOT NULL"):
            key = f"sent:{p['slug']}"
            if self.st.get(key) == day:
                continue
            ds = pp.results(p["slug"])
            try:
                self.send(p["tg_chat"], pr.day_message(t.date(), ds.get(t.date()), ds))
            except Exception:
                continue                                     # retried on the next loop
            self.st.put(key, day)
            n += 1
        return n

    def safety(self):
        """Trade open but the partner's bot is not running: tell them once per position."""
        for p in self.st.q("SELECT * FROM partners WHERE status='active' AND tg_chat IS NOT NULL"):
            try:
                pos = (json.loads((pp.pdir(p["slug"]) / "data" / "state.json").read_text()) or {}).get("open")
            except (OSError, ValueError):
                pos = None
            key = f"down:{p['slug']}"
            if pos and pos.get("trade_mode") == "live" and not self.active(p["slug"]):
                mark = str(pos.get("entry_ts"))
                if self.st.get(key) != mark:
                    self.send(p["tg_chat"], "Your bot stopped with a trade open: please close it in the Delta app now.")
                    self.st.put(key, mark)

    def run(self):
        off = int(self.st.get("tg_offset", "0"))
        while True:
            try:
                for u in tg(self.token, "getUpdates", offset=off, timeout=25) or []:
                    off = u["update_id"] + 1
                    self.st.put("tg_offset", str(off))
                    try:
                        self.handle(u)
                    except Exception:
                        pass
                self.welcome()
                self.daily()
                self.safety()
            except Exception:
                time.sleep(10)


def main():
    token = os.environ.get("PARTNER_TG_TOKEN", "")
    if not token:
        raise SystemExit("set PARTNER_TG_TOKEN (the results bot's token from @BotFather)")
    st = pp.open_store()
    me = tg(token, "getMe")
    st.put("tg_bot", me.get("username", ""))
    ResultsBot(st, token).run()


if __name__ == "__main__":
    main()
