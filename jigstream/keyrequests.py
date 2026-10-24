"""Asking for a key from inside BotTrack, and the key arriving by email by itself.

- On the key screen, Request access emails the developer address (the one the release was built with) from the
  person's own Gmail, the same way Send logs does. Copies already using a Gmail send it in one click.
- On jacob's computer (the one with the signing key) the Account keys page lists the requests it finds in his
  connected inboxes. Allow makes the key and emails it to that Gmail from his; Ignore hides the request.
- The waiting copy looks in its Gmail (inbox and spam) for the key email every minute, checks it like a typed key,
  and unlocks. A key email can't be faked: the key is signed and has to name that Gmail.

A request names the Gmail it was sent from (Gmail sets the From), so nobody can ask on someone else's behalf.
"""
import email
import imaplib
import json
import logging
import platform
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr

from flask import Blueprint, current_app, flash, jsonify, redirect, request, url_for

from . import accountkeys

logger = logging.getLogger("jigstream.keyrequests")
bp = Blueprint("keyrequests", __name__)

ASK_SUBJECT = "BotTrack access request"
KEY_SUBJECT = "Your BotTrack key"
MARK = "BOTTRACK-REQUEST "
FOLDERS = ("INBOX", '"[Gmail]/Spam"')
LOOK_EVERY = 60  # seconds between looks for a key email while waiting
REQUESTS_EVERY = 600  # seconds between looks for new requests on jacob's computer
SCHEMA = """
CREATE TABLE IF NOT EXISTS key_requests (
    email TEXT PRIMARY KEY, name TEXT, note TEXT, computer TEXT, version TEXT, asked_at TEXT, inbox TEXT,
    status TEXT DEFAULT 'waiting', kid TEXT, done_at TEXT
);
"""
_looked = {"key": 0.0, "requests": 0.0}
_lock = threading.Lock()


def developer():
    from . import logs
    import os
    return (os.environ.get("BOTTRACK_LOGS_TO") or logs.DEVELOPER_EMAIL or logs._built_in_address()).strip()


def _smtp():
    from .logs import SMTP_HOST
    return smtplib.SMTP_SSL(SMTP_HOST, 465, context=ssl.create_default_context(), timeout=30)


def _text(msg):
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/plain":
            return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def search(config, address, password, subject, since_days=30):
    """[(From address, Subject, plain text)] of mail with this subject in the inbox and spam."""
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(config.imap_host, timeout=30)
    out = []
    try:
        imap.login(address, password)
        for folder in FOLDERS:
            try:
                if imap.select(folder, readonly=True)[0] != "OK":
                    continue
                _, data = imap.uid("SEARCH", None, f'SUBJECT "{subject}" SINCE {since}')
                for uid in (data[0] or b"").split()[-100:]:
                    _, got = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                    raw = next((m[1] for m in got if isinstance(m, tuple)), b"")
                    msg = email.message_from_bytes(raw)
                    out.append((parseaddr(msg.get("From", ""))[1].lower(), str(msg.get("Subject", "")), _text(msg)))
            except imaplib.IMAP4.error as e:
                logger.info("couldn't look in %s of %s: %s", folder, address, e)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return out


# ---- the person asking ----

def ask(sender, password, name="", note="", version="", to=None, smtp=None):
    """Email a request for a key from the person's Gmail to the developer."""
    to = to or developer()
    if not to:
        raise RuntimeError("This copy of BotTrack wasn't built with an address to ask. Ask the person who sent it to you.")
    info = {"email": sender.lower(), "name": name.strip()[:60], "note": note.strip()[:300],
            "computer": platform.node()[:60], "version": version, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    msg = EmailMessage()
    msg["Subject"] = f"{ASK_SUBJECT}: {sender}"
    msg["From"], msg["To"], msg["Reply-To"] = sender, to, sender
    msg.set_content(f"{name or sender} asked for a BotTrack key.\n" + (f"\n{note.strip()}\n" if note.strip() else "")
                    + "\nOpen BotTrack, Setup, Account keys to allow it.\n\n" + MARK + json.dumps(info))
    with (smtp or _smtp()) as s:
        s.login(sender, password)
        s.send_message(msg)
    return to


def key_in(text, accounts):
    """The first key in this text that opens BotTrack here, or None."""
    for key in re.findall(re.escape(accountkeys.PREFIX) + r"[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", text or ""):
        if accountkeys.check(key, accounts) is None:
            return key
    return None


def look_for_key(config, search_fn=search):
    """A key emailed to one of this computer's Gmail inboxes, saved and returned; None when none has come."""
    from .config import save_env
    accounts = [a for a, _ in config.accounts]
    for address, password in config.accounts:
        try:
            found = search_fn(config, address, password, KEY_SUBJECT)
        except Exception as e:
            logger.info("couldn't look for a key in %s: %s", address, e)
            continue
        for _, _, text in reversed(found):
            key = key_in(text, accounts)
            if key:
                save_env({"BOTTRACK_KEY": key})
                logger.info("BotTrack key arrived by email for %s", address)
                return key
    return None


# ---- jacob's computer ----

def _con():
    from . import db
    con = db.connect(current_app.config.get("DB_PATH"))
    return db.setup_once(con, "keyrequests", lambda c: c.executescript(SCHEMA))


def note_requests(con, config, search_fn=search):
    """Add the requests in this computer's inboxes to key_requests. Returns how many are new."""
    con.executescript(SCHEMA)
    new = 0
    for address, password in config.accounts:
        try:
            found = search_fn(config, address, password, ASK_SUBJECT, since_days=60)
        except Exception as e:
            logger.info("couldn't look for key requests in %s: %s", address, e)
            continue
        for sender, _, text in found:
            line = next((l for l in text.splitlines() if l.startswith(MARK)), "")
            try:
                info = json.loads(line[len(MARK):]) if line else {}
            except ValueError:
                info = {}
            if not sender or "@" not in sender:
                continue
            # Asking again after being allowed or ignored (a lost key, a new computer) puts it back on the list.
            cur = con.execute("INSERT INTO key_requests (email, name, note, computer, version, asked_at, inbox)"
                              " VALUES (?,?,?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET status='waiting',"
                              " asked_at=excluded.asked_at, name=excluded.name, note=excluded.note, computer=excluded.computer,"
                              " version=excluded.version, inbox=excluded.inbox"
                              " WHERE excluded.asked_at > key_requests.asked_at AND key_requests.status != 'waiting'", (sender, str(info.get("name", ""))[:60], str(info.get("note", ""))[:300],
                                                          str(info.get("computer", ""))[:60], str(info.get("version", ""))[:20],
                                                          str(info.get("at", ""))[:25], address))
            new += cur.rowcount
    con.commit()
    return new


def waiting(con):
    con.executescript(SCHEMA)
    return [dict(r) for r in con.execute("SELECT * FROM key_requests WHERE status='waiting' ORDER BY asked_at DESC")]


def send_key(sender, password, to, key, smtp=None):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = KEY_SUBJECT, sender, to
    msg.set_content("You're in. BotTrack picks up this key by itself within a minute when it's open on the key screen.\n"
                    "If it doesn't, copy the whole key below and paste it there.\n\n" + key + "\n")
    with (smtp or _smtp()) as s:
        s.login(sender, password)
        s.send_message(msg)


def allow(con, config, address, days=None, smtp=None):
    """Make the key for a request and email it from the inbox the request came to."""
    row = con.execute("SELECT * FROM key_requests WHERE email=?", (address,)).fetchone()
    if row is None:
        raise ValueError("That request is gone.")
    key, body = accountkeys.issue(accountkeys.signing_key(), address, days, row["name"] or "")
    accountkeys.ensure(con)
    con.execute("INSERT INTO license_keys (kid, email, note, issued_at, expires, key) VALUES (?,?,?,?,?,?)",
                (body["i"], address, body.get("n", ""), body["t"], body.get("x"), key))
    passwords = dict(config.accounts)
    sender = row["inbox"] if row["inbox"] in passwords else (config.accounts[0][0] if config.accounts else "")
    if not sender:
        raise RuntimeError("Connect a Gmail inbox to send keys from.")
    send_key(sender, passwords[sender], address, key, smtp=smtp)
    con.execute("UPDATE key_requests SET status='allowed', kid=?, done_at=? WHERE email=?",
                (body["i"], datetime.now(timezone.utc).strftime("%Y-%m-%d"), address))
    con.commit()
    logger.info("allowed a key request from %s (%s)", address, body["i"])
    return key


def start(app):
    """On the computer with the signing key, look for new requests every few minutes. On a copy that asked for a
    key, look for the key email even when the key screen isn't showing (a copy still in its week)."""
    import os

    def loop():
        time.sleep(90)
        while True:
            try:
                from . import db
                from .config import Config
                config = Config.from_env()
                if accountkeys.signing_key():
                    note_requests(db.connect(app.config.get("DB_PATH")), config)
                    _looked["requests"] = time.time()
                elif os.environ.get("BOTTRACK_KEY_ASKED") and accountkeys.status([a for a, _ in config.accounts])["why"]:
                    look_for_key(config)
            except Exception:
                logger.exception("looking for key requests failed")
            time.sleep(REQUESTS_EVERY)
    threading.Thread(target=loop, daemon=True, name="key-requests").start()


# ---- pages ----

@bp.post("/activate/request")
def request_access():
    import os
    from .config import Config, format_accounts, save_env
    from .mail import check_login
    from .updater import installed_version
    config = Config.from_env()
    passwords = dict(config.accounts)
    address = request.form.get("address", "").strip().lower()
    password = request.form.get("password", "").replace(" ", "").strip()
    if address not in passwords:
        if "@" not in address or not password:
            flash("Enter your Gmail address and its app password.")
            return redirect(url_for("accountkeys.activate"))
        error = check_login(config, address, password)
        if error:
            flash(f"{address}: {error}")
            return redirect(url_for("accountkeys.activate"))
        save_env({"GMAIL_ACCOUNTS": format_accounts(config.accounts + [(address, password)])})
        passwords[address] = password
    try:
        ask(address, passwords[address], request.form.get("name", ""), request.form.get("note", ""), installed_version())
    except Exception as e:
        logger.warning("key request failed: %s", e)
        flash(f"Couldn't send the request: {e}")
        return redirect(url_for("accountkeys.activate"))
    save_env({"BOTTRACK_KEY_ASKED": f"{address}|{datetime.now(timezone.utc):%Y-%m-%d}"})
    os.environ["BOTTRACK_KEY_ASKED"] = f"{address}|{datetime.now(timezone.utc):%Y-%m-%d}"
    flash("Request sent. Keep BotTrack open: it unlocks by itself when your key arrives in your Gmail.")
    return redirect(url_for("accountkeys.activate"))


@bp.get("/activate/check")
def check_now():
    """Has a key arrived? Looks at most once a minute unless asked by hand."""
    from .config import Config
    with _lock:
        if request.args.get("now") or time.time() - _looked["key"] >= LOOK_EVERY:
            _looked["key"] = time.time()
            config = Config.from_env()
            if config.accounts and look_for_key(config):
                return jsonify(open=True)
    s = accountkeys.status([a for a, _ in Config.from_env().accounts])
    return jsonify(open=bool(s["open"] and not s["why"]))


def _developer_here():
    from .mobile import from_this_mac
    return from_this_mac() and accountkeys.signing_key()


@bp.post("/keys/requests/look")
def look_requests():
    from .config import Config
    if not _developer_here():
        return redirect("/")
    n = note_requests(_con(), Config.from_env())
    _looked["requests"] = time.time()
    flash(f"{n} new request{'s' if n != 1 else ''}." if n else "No new requests.")
    return redirect(url_for("accountkeys.keys_page"))


@bp.post("/keys/requests/allow")
def allow_page():
    from .config import Config
    if not _developer_here():
        return redirect("/")
    address = request.form.get("email", "")
    try:
        allow(_con(), Config.from_env(), address, request.form.get("days") or None)
    except Exception as e:
        logger.warning("allowing %s failed: %s", address, e)
        flash(f"Couldn't send the key to {address}: {e}")
        return redirect(url_for("accountkeys.keys_page"))
    flash(f"Key sent to {address}. Their BotTrack unlocks within a minute.")
    return redirect(url_for("accountkeys.keys_page"))


@bp.post("/keys/requests/ignore")
def ignore_page():
    if not _developer_here():
        return redirect("/")
    con = _con()
    con.execute("UPDATE key_requests SET status='ignored', done_at=? WHERE email=?",
                (datetime.now(timezone.utc).strftime("%Y-%m-%d"), request.form.get("email", "")))
    con.commit()
    return redirect(url_for("accountkeys.keys_page"))
