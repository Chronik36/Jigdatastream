"""Shared training packs: corrections from every BotTrack user, scrubbed of anything personal, teaching every copy.

How it works:
1. Share: on the Training page a user sends their corrected examples (opt in, previewed first). Each example is
   scrubbed here: names, email addresses, phone numbers, street addresses, card numbers, links and order/tracking
   numbers are replaced with made-up ones. An example that still holds one of the user's own names, addresses or
   inboxes after that is left out. The pack goes by email from the user's own Gmail to the developer address.
2. The developer merges packs into jigstream/shared/examples.json, which ships with each update.
3. Every copy puts the shared examples beside its own, and before each email the model is shown the few closest
   ones (same sender's domain, then similar subject). Its own corrections come first when they fit as well.
Examples are plain email text and answers, so they work with any model size (4B, 8B, 14B); trained adapters
are tied to one model and are never shared.
"""
import base64
import gzip
import json
import random
import re
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for

from . import logs

FORMAT = "bottrack-examples"
SHARED_FILE = Path(__file__).with_name("shared") / "examples.json"
MAX_SHARE = 300  # examples per pack
EXAMPLE_CHARS = 2000
PER_EMAIL = 2  # examples shown to the model before each email
MAIL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "icloud.com", "me.com", "mac.com", "outlook.com",
                "hotmail.com", "live.com", "aol.com", "proton.me", "protonmail.com", "msn.com", "ymail.com"}

EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
STREET_RE = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Za-z0-9.'-]+\s+){0,4}(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|"
    r"lane|ct|court|way|pl|place|pkwy|parkway|cir|circle|hwy|highway|ter|terrace|trl|trail|loop|sq|square|route|rte)\b\.?"
    r"(?:[ ,]+(?:apt|suite|ste|unit|#)\s*[\w-]+)?", re.I)
CITY_RE = re.compile(r"\b[A-Z][A-Za-z .'-]{1,30},\s*[A-Z]{2},?\s+\d{5}(?:-\d{4})?\b")
ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})\b")
URL_RE = re.compile(r"(https?://[^/\s\"'<>]+)[^\s\"'<>]*")
GREETING_RE = re.compile(r"\b(Hi|Hello|Hey|Dear|Thanks|Thank you),?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b")
FAKE_NAME = "Alex Customer"
FAKE_STREET = "123 Example St"
FAKE_CITY = "Springfield, ST 00000"


def _lookalike(value, rng):
    """A made-up number shaped like a real one: same letters/digits pattern, same length."""
    return "".join(rng.choice("0123456789") if ch.isdigit() else rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") if ch.isupper()
                   else rng.choice("abcdefghjkmnpqrstuvwxyz") if ch.islower() else ch for ch in value)


def private_words(con):
    """The user's own names, streets and inbox addresses: none of these may leave the Mac."""
    words = set()
    for r in con.execute("SELECT first_name, last_name, ship_address1, bill_address1, email FROM profiles"):
        words.update(w for w in r if w and len(w) >= 3)
    for r in con.execute("SELECT address FROM mail_accounts") if _has(con, "mail_accounts") else []:
        words.add(r[0])
    import os
    from .config import parse_accounts
    words.update(a for a, _ in parse_accounts(os.environ.get("GMAIL_ACCOUNTS", "")))
    for r in con.execute("SELECT shipping_name, recipient_email FROM tracked_orders") if _has_cols(con) else []:
        words.update(w for w in r if w and len(w) >= 3)
    return {w.strip() for w in words if w and w.strip() and not w.strip().isdigit()}


def _has(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is not None


def _has_cols(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(tracked_orders)")}
    return {"shipping_name", "recipient_email"} <= cols


def scrub_example(prompt, answer, private=(), rng=None):
    """(prompt, answer) with everything personal replaced, or None when something personal is still left."""
    rng = rng or random.Random()
    ans = dict(answer)
    text = prompt
    # Numbers the answer names (order, tracking) get look-alikes, the same in the email and the answer.
    for key in ("order_number", "tracking_number"):
        v = ans.get(key)
        if isinstance(v, str) and len(v) >= 4:
            fake = _lookalike(v, rng)
            text = text.replace(v, fake)
            ans[key] = fake
    sender = re.search(r"^From:.*?([\w.+-]+@[\w.-]+)", text, re.M)
    sender_domain = sender.group(1).split("@")[1].lower() if sender else ""

    def mail(m):  # the store's own addresses stay; anyone's personal address doesn't
        domain = m.group(1).lower()
        return m.group(0) if domain == sender_domain and domain not in MAIL_DOMAINS else "customer@example.com"
    text = EMAIL_RE.sub(mail, text)
    text = logs.scrub(text)
    text = URL_RE.sub(lambda m: m.group(1) + "/…", text)
    text = CITY_RE.sub(FAKE_CITY, text)
    text = STREET_RE.sub(FAKE_STREET, text)
    text = ZIP_RE.sub("00000-0000", text)
    text = PHONE_RE.sub("555-010-0100", text)
    text = GREETING_RE.sub(lambda m: m.group(1) + " Alex", text)
    for word in sorted(private, key=len, reverse=True):
        text = re.sub(r"\b" + re.escape(word) + r"\b", "Alex" if " " not in word and word[:1].isupper() else "[private]", text,
                      flags=re.I)
    for key, fake in (("shipping_name", FAKE_NAME), ("shipping_address", f"{FAKE_STREET}, {FAKE_CITY}"),
                      ("recipient_email", "customer@example.com"), ("tracking_url", None), ("account", None)):
        if ans.get(key):
            ans[key] = fake
    blob = text + json.dumps(ans)
    if any(re.search(r"\b" + re.escape(w) + r"\b", blob, re.I) for w in private if len(w) >= 4):
        return None  # something of theirs got through; leave this one out
    return text[:EXAMPLE_CHARS], ans


def build(con, store, limit=MAX_SHARE, rng=None):
    """The user's corrected examples, scrubbed, as a pack dict."""
    rng = rng or random.Random()
    private = private_words(con)
    out, dropped = [], 0
    for ex in store.examples()[:limit]:
        try:
            answer = json.loads(ex["corrected"])
        except (TypeError, json.JSONDecodeError):
            continue
        clean = scrub_example(ex["prompt"] or "", answer, private, rng)
        if clean is None:
            dropped += 1
            continue
        prompt, ans = clean
        sender = re.search(r"@([\w.-]+)", ex["sender"] or "")
        out.append({"sender_domain": sender.group(1).lower() if sender else "", "subject": _subject(prompt),
                    "label": ex["status"] or ans.get("shipping_status") or ans.get("kind"), "prompt": prompt,
                    "answer": ans})
    return {"format": FORMAT, "version": 1, "created": date.today().isoformat(), "examples": out, "left_out": dropped}


def _subject(prompt):
    m = re.search(r"^Subject:\s*(.*)$", prompt, re.M)
    return m.group(1).strip()[:200] if m else ""


def encode(pack):
    """The pack as text for an email body: gzip + base64 between marker lines."""
    blob = base64.b64encode(gzip.compress(json.dumps(pack).encode())).decode()
    lines = [blob[i:i + 76] for i in range(0, len(blob), 76)]
    return "-----BEGIN BOTTRACK PACK-----\n" + "\n".join(lines) + "\n-----END BOTTRACK PACK-----"


def decode(text):
    """A pack from a file or an email body: plain JSON, or the encoded block."""
    text = text.strip()
    m = re.search(r"-----BEGIN BOTTRACK PACK-----(.*?)-----END BOTTRACK PACK-----", text, re.S)
    data = json.loads(gzip.decompress(base64.b64decode(re.sub(r"\s", "", m.group(1)))) if m else text)
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("That isn't a BotTrack training pack.")
    return data


def developer():
    """Where packs go: the address the release was built with (or BOTTRACK_LOGS_TO). Unlike logs, never the user's
    own inbox, since a pack mailed to yourself teaches nobody."""
    import os
    return (os.environ.get("BOTTRACK_LOGS_TO") or logs.DEVELOPER_EMAIL or logs._built_in_address()).strip()


def send(pack, sender, password, version="", to=None, smtp=None):
    """Email a pack from the user's own Gmail to the developer address."""
    to = to or developer()
    if not to:
        raise RuntimeError("This copy of BotTrack has no developer address to share with.")
    msg = EmailMessage()
    msg["Subject"] = f"BotTrack training pack ({len(pack['examples'])} examples, {version or 'unknown build'})"
    msg["From"], msg["To"] = sender, to
    msg.set_content("Corrections shared from BotTrack, scrubbed of names, addresses, emails, phones and card numbers.\n"
                    "Import on the Training page (Shared examples), or merge into jigstream/shared/examples.json.\n\n"
                    + encode(pack))
    msg.add_attachment(json.dumps(pack, indent=1).encode(), maintype="application", subtype="json",
                       filename="bottrack-training-pack.json")
    if smtp is None:
        smtp = smtplib.SMTP_SSL(logs.SMTP_HOST, 465, context=ssl.create_default_context(), timeout=30)
    with smtp as s:
        s.login(sender, password)
        s.send_message(msg)
    return to


def merge(packs):
    """Several packs as one, without repeats (same sender domain, subject and answer)."""
    seen, out = set(), []
    for pack in packs:
        for ex in pack.get("examples", []):
            key = (ex.get("sender_domain"), ex.get("subject"), json.dumps(ex.get("answer"), sort_keys=True))
            if key not in seen and ex.get("prompt") and isinstance(ex.get("answer"), dict):
                seen.add(key)
                out.append(ex)
    return {"format": FORMAT, "version": 1, "created": date.today().isoformat(), "examples": out}


# ---- shared examples on this Mac: the pack that ships with the app plus packs imported here ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_examples (
    id INTEGER PRIMARY KEY, source TEXT, sender_domain TEXT, subject TEXT, label TEXT, prompt TEXT, answer TEXT,
    UNIQUE (sender_domain, subject, answer)
);
"""


def ensure(con):
    from . import db
    return db.setup_once(con, "packs", lambda c: c.executescript(SCHEMA))


def import_pack(con, pack, source):
    """Add a pack's examples to this Mac's shared examples. Returns how many were new."""
    ensure(con)
    n = 0
    for ex in merge([pack])["examples"]:
        cur = con.execute("INSERT OR IGNORE INTO shared_examples (source, sender_domain, subject, label, prompt, answer)"
                          " VALUES (?,?,?,?,?,?)", (source, ex.get("sender_domain") or "", ex.get("subject") or "",
                                                    ex.get("label") or "", ex["prompt"][:EXAMPLE_CHARS],
                                                    json.dumps(ex["answer"], sort_keys=True)))
        n += cur.rowcount
    con.commit()
    return n


def built_in():
    try:
        return decode(SHARED_FILE.read_text())["examples"]
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def shared(con):
    ensure(con)
    rows = [dict(r) for r in con.execute("SELECT sender_domain, subject, label, prompt, answer FROM shared_examples")]
    for r in rows:
        r["answer"] = json.loads(r["answer"])
    return built_in() + rows


# ---- picking the examples shown to the model for one email ----

WORD_RE = re.compile(r"[a-z]{3,}")


def _words(text):
    return set(WORD_RE.findall((text or "").lower())) - {"your", "the", "and", "for", "you", "has", "have", "with", "from"}


class Picker:
    """Chooses the examples shown before each email: the closest ones by sender domain and subject, the user's
    own corrections before shared ones. With nothing close, the same default pair every time, which keeps the
    engine's prompt cache warm."""

    def __init__(self, own, shared_examples=(), default=(), per_email=PER_EMAIL):
        self.pool = [(o["sender_domain"], _words(o["subject"]), 1, o["prompt"], o["answer"]) for o in own] + \
                    [(s.get("sender_domain") or "", _words(s.get("subject")), 0, s["prompt"],
                      s["answer"] if isinstance(s["answer"], str) else json.dumps(s["answer"])) for s in shared_examples]
        self.default = list(default)
        self.per_email = per_email

    def __len__(self):
        return len(self.pool) or len(self.default)

    def __call__(self, email):
        m = re.search(r"@([\w.-]+)", email.sender or "")
        domain, words = (m.group(1).lower() if m else ""), _words(email.subject)
        scored = []
        for i, (d, w, own, prompt, answer) in enumerate(self.pool):
            score = (3 if d and domain and (d == domain or d.endswith("." + domain) or domain.endswith("." + d)) else 0)
            score += 2 * len(words & w) / (len(words | w) or 1)
            if score >= 1:
                scored.append((score + own * .5, -i, prompt, answer))
        scored.sort(reverse=True)
        picked = [(p[:EXAMPLE_CHARS], a) for _, _, p, a in scored[:self.per_email]]
        return picked or self.default


def picker(store, default):
    """The Picker for a sync: this Mac's corrections plus the shared ones."""
    own = []
    for ex in store.examples():
        m = re.search(r"@([\w.-]+)", ex["sender"] or "")
        own.append({"sender_domain": m.group(1).lower() if m else "", "subject": ex["subject"] or "",
                    "prompt": (ex["prompt"] or "")[:EXAMPLE_CHARS], "answer": ex["corrected"]})
    return Picker(own, shared(store.db), default)


# ---- pages ----

bp = Blueprint("packs", __name__)


def _store():
    from . import db
    from .orderstore import Store
    store = Store(db.connect(current_app.config.get("DB_PATH")))
    ensure(store.db)
    return store


@bp.get("/training/share")
def page():
    from .config import Config
    store = _store()
    pack = build(store.db, store)
    return render_template("training_share.html", pack=pack, preview=pack["examples"][:3], own=len(store.examples()),
                           shared_n=len(shared(store.db)), built_in_n=len(built_in()), to=developer(),
                           accounts=[a for a, _ in Config.from_env().accounts],
                           sources=[dict(r) for r in store.db.execute(
                               "SELECT source, COUNT(*) AS n FROM shared_examples GROUP BY source ORDER BY source")])


@bp.post("/training/share")
def share():
    from .config import Config
    store = _store()
    accounts = dict(Config.from_env().accounts)
    sender = request.form.get("sender") or next(iter(accounts), "")
    if not request.form.get("consent"):
        flash("Tick the box to say it's OK to share these examples.")
    elif sender not in accounts:
        flash("Connect a Gmail inbox on Setup first; the pack is sent from it.")
    else:
        pack = build(store.db, store)
        if not pack["examples"]:
            flash("There are no corrections to share yet.")
        else:
            try:
                from .updater import installed_version
                send(pack, sender, accounts[sender], installed_version())
                flash(f"Shared {len(pack['examples'])} examples. Thank you! They'll reach everyone in a coming update.")
            except Exception as e:
                flash(f"Couldn't send the pack: {logs.redact(str(e))}")
    return redirect(url_for("packs.page"))


@bp.post("/training/share/import")
def import_():
    store = _store()
    f = request.files.get("pack")
    text = f.read().decode("utf-8", "replace") if f and f.filename else request.form.get("text", "")
    try:
        pack = decode(text)
        n = import_pack(store.db, pack, (f.filename if f and f.filename else "pasted") + " " + date.today().isoformat())
        flash(f"Added {n} new shared example{'s' if n != 1 else ''} ({len(pack['examples']) - n} were already here).")
    except (ValueError, json.JSONDecodeError, OSError) as e:
        flash(f"Couldn't read that pack: {str(e)[:200]}")
    return redirect(url_for("packs.page"))


@bp.post("/training/share/forget")
def forget():
    store = _store()
    store.db.execute("DELETE FROM shared_examples WHERE source = ?", (request.form.get("source", ""),))
    store.db.commit()
    return redirect(url_for("packs.page"))


@bp.get("/training/share/export.json")
def export_merged():
    """Every shared example on this Mac in one pack: for the developer to ship as jigstream/shared/examples.json."""
    store = _store()
    merged = merge([{"examples": shared(store.db)}])
    return Response(json.dumps(merged, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=examples.json"})
