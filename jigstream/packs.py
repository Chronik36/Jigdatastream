"""Shared learning: how the AI learned from every BotTrack user's corrections, with none of their emails in it.

How it works:
1. Share: on the Training page a user sends what their corrections taught (opt in, every lesson shown first, any
   one can be left out). A lesson holds only:
   - the store's email domain (never a personal mail domain like gmail.com or one of the user's own inboxes),
   - the right answer from BotTrack's fixed lists (order / expense / sale / not an order, a shipping status,
     an expense category),
   - cue words from the subject, only the ones on a fixed list of shopping words (order, shipped, refund...),
   - for order number, tracking number and total: the words in front of it ("Order #", "Tracking number"),
     only when every one of them is on a fixed list. Never the number or amount itself.
   No email text, names, addresses, numbers, amounts, products or dates. Anything not on a fixed list is dropped,
   so nothing personal can slip through the way a missed pattern could.
   The pack goes by email from the user's own Gmail to the developer address, so the developer sees who sent it.
2. The developer imports packs (each lesson is checked again against the same lists), reads them, removes any
   that teach the wrong thing, and merges the rest into jigstream/shared/lessons.json, which ships with each
   update. That file is in BotTrack's public code, which is fine because lessons hold nothing personal.
3. Before each email, the model is shown the lessons for that email's store as hints, next to the user's own
   corrected examples (which never leave the Mac). Trained adapters are tied to one model and are never shared.
"""
import base64
import gzip
import json
import re
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for

from . import logs

FORMAT = "bottrack-lessons"
OLD_FORMAT = "bottrack-examples"  # packs from older builds held scrubbed email text; importing keeps only the lesson
SHARED_FILE = Path(__file__).with_name("shared") / "lessons.json"
MAX_SHARE = 300  # lessons per pack
EXAMPLE_CHARS = 2000
PER_EMAIL = 2  # own examples shown to the model before each email
HINTS_PER_EMAIL = 2  # shared lessons shown as hints
MAIL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "icloud.com", "me.com", "mac.com", "outlook.com",
                "hotmail.com", "live.com", "aol.com", "proton.me", "protonmail.com", "msn.com", "ymail.com"}

# The only words a lesson may carry. Anything else in a subject or in front of a number is dropped.
CUE_WORDS = set("""
order orders ordered placed confirmed confirmation receipt receipts invoice invoices purchase purchased payment
payments paid charged charge successful success failed declined decline thanks thank your you has have been was is
are will be on its it's the way of for a an to from with we got received processing preparing prepared ready pickup
pick up picked shipped shipping ship ships shipment shipments package packages parcel delivered delivery deliveries
arriving arrives arrive arrived today tomorrow soon out in transit tracking track update updated updates status
delayed delay exception attempted label created cancelled canceled cancellation cancel refund refunded refunds return
returned returns credit partial partially item items backorder backordered preorder pre-order estimated subscription
subscriptions renewal renewed renew renews trial plan membership billing bill expires expiring expired account key
license proxy proxies bandwidth data gb residential isp datacenter bot bots checkout checkouts success group server
captcha sold sale sales payout payouts paid-out offer accepted trade-in tradein earnings funds deposit transferred
new verify verification code security sign-in login password reset welcome newsletter deal deals sale save off
""".split())
FIELD_WORDS = set("""
order orders number no no. # id confirmation reference ref invoice receipt purchase tracking track shipment package
carrier via total grand order-total amount charged paid payment subtotal your web online store
""".split())
FIELD_KEYS = ("order_number", "tracking_number", "total")


def _allowed_labels():
    from .inbox import LABELS
    return set(LABELS) | {"shipped", "in_transit", "out_for_delivery", "not_an_order", "expense", "sale"}


def _known_whats():
    from .inbox import LABELS
    return {t["what"] for _, t in LABELS.values() if t.get("what")}


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


def _own_domains(private):
    return {w.split("@", 1)[1].lower() for w in private if "@" in w}


def store_domain(sender, own_domains=()):
    """The store's email domain, or "" when it's a personal mail service or one of the user's own domains."""
    m = re.search(r"@([\w-]+(?:\.[\w-]+)+)", sender or "")
    domain = m.group(1).lower() if m else ""
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain) or domain in MAIL_DOMAINS:
        return ""
    if any(domain == d or domain.endswith("." + d) for d in own_domains):
        return ""
    return domain


def cues(subject):
    """The runs of shopping words in a subject ("your order", "has shipped"); every other word is dropped."""
    runs, run = [], []
    for token in (subject or "").split():  # any other word or number ends a run
        w = token.strip(".,!?:;\"'()[]").lower()
        if w in CUE_WORDS and w == token.strip(".,!?:;\"()[]").lower():
            run.append(w)
        elif run:
            runs.append(" ".join(run))
            run = []
    if run:
        runs.append(" ".join(run))
    return [r for r in dict.fromkeys(runs) if r not in ("the", "a", "an", "to", "of", "for", "on", "is", "it")][:6]


def field_hint(text, value):
    """The words just before a value on its line ("Order #"), only when every one is a FIELD_WORDS word."""
    if value in (None, ""):
        return ""
    shapes = [f"{value:.2f}", f"{value:,.2f}"] if isinstance(value, (int, float)) else [str(value)]
    for shape in shapes:
        if len(shape) < 3:
            continue
        for m in re.finditer(re.escape(shape), text):  # the first place it sits behind a plain label
            before = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
            before = re.sub(r"[$€£:\s]+$", "", before)
            tokens = re.findall(r"\S+", before)[-3:]
            if tokens and all(t.lower() in FIELD_WORDS for t in tokens):
                return " ".join(tokens)
    return ""


def make_lesson(sender, subject, label, answer, text="", own_domains=()):
    """One lesson from one correction: only fixed-list values survive. None when nothing useful is left."""
    answer = answer if isinstance(answer, dict) else {}
    from .extract import EXPENSE_CATEGORIES, STATUSES
    ans = {}
    if answer.get("kind") in ("order", "botting_expense", "sale", "other"):
        ans["kind"] = answer["kind"]
    if answer.get("shipping_status") in STATUSES:
        ans["shipping_status"] = answer["shipping_status"]
    category = answer.get("category") or answer.get("expense_category")
    if category in EXPENSE_CATEGORIES:
        ans["category"] = category
    if answer.get("what") in _known_whats():
        ans["what"] = answer["what"]
    fields = {k: h for k in FIELD_KEYS if (h := field_hint(text or "", answer.get(k)))}
    lesson = {"sender_domain": store_domain(sender, own_domains),
              "label": label if label in _allowed_labels() else "",
              "answer": ans, "cues": cues(subject), "fields": fields}
    if not lesson["sender_domain"] or not (ans or lesson["label"]):
        return None  # a lesson with no store can't be matched to anything, so it teaches nothing
    return lesson


def check(lesson, own_domains=()):
    """A lesson from someone else, put through the same lists again. None when it doesn't pass."""
    if not isinstance(lesson, dict):
        return None
    subject = " ".join(c for c in lesson.get("cues") or [] if isinstance(c, str))
    fields = lesson.get("fields") if isinstance(lesson.get("fields"), dict) else {}
    clean = make_lesson("x@" + str(lesson.get("sender_domain") or ""), subject, lesson.get("label"),
                        lesson.get("answer"), own_domains=own_domains)
    if clean is None:
        return None
    clean["cues"] = [c for c in clean["cues"] if c in (lesson.get("cues") or [])]
    clean["fields"] = {k: v for k, v in fields.items() if k in FIELD_KEYS and isinstance(v, str) and len(v) <= 40
                       and v.split() and all(t.lower() in FIELD_WORDS for t in v.split())}
    return clean


def build(con, store, limit=MAX_SHARE, only=None):
    """What this Mac's corrections taught, as a pack dict. Each lesson carries its local example id as "ref"
    (for leaving it out on the page); public() takes that off before it's sent. only: the ids to keep."""
    own = _own_domains(private_words(con))
    out, seen, dropped = [], set(), 0
    for ex in store.examples()[:limit]:
        if only is not None and ex["id"] not in only:
            continue
        try:
            answer = json.loads(ex["corrected"])
        except (TypeError, json.JSONDecodeError):
            continue
        lesson = make_lesson(ex["sender"], ex["subject"] or _subject(ex["prompt"] or ""), ex["status"], answer,
                             ex["prompt"] or "", own)
        if lesson is None:
            dropped += 1
            continue
        key = _key(lesson)
        if key in seen:
            continue
        seen.add(key)
        out.append({"ref": ex["id"], **lesson})
    return {"format": FORMAT, "version": 2, "created": date.today().isoformat(), "lessons": out, "left_out": dropped}


def _key(lesson):
    return json.dumps([lesson.get("sender_domain"), lesson.get("label"), lesson.get("answer"), lesson.get("cues"),
                       lesson.get("fields")], sort_keys=True)


def public(pack):
    """The pack as it leaves this computer: no local ids, no counts."""
    return {"format": FORMAT, "version": 2, "created": pack["created"],
            "lessons": [{k: v for k, v in ls.items() if k != "ref"} for ls in pack["lessons"]]}


def _subject(prompt):
    m = re.search(r"^Subject:\s*(.*)$", prompt, re.M)
    return m.group(1).strip()[:200] if m else ""


def encode(pack):
    """The pack as text for an email body: gzip + base64 between marker lines."""
    blob = base64.b64encode(gzip.compress(json.dumps(pack).encode())).decode()
    lines = [blob[i:i + 76] for i in range(0, len(blob), 76)]
    return "-----BEGIN BOTTRACK PACK-----\n" + "\n".join(lines) + "\n-----END BOTTRACK PACK-----"


def decode(text):
    """A pack from a file or an email body: plain JSON, or the encoded block. An older pack of scrubbed email text
    comes back as lessons only; its text is dropped here and never stored."""
    text = text.strip()
    m = re.search(r"-----BEGIN BOTTRACK PACK-----(.*?)-----END BOTTRACK PACK-----", text, re.S)
    data = json.loads(gzip.decompress(base64.b64decode(re.sub(r"\s", "", m.group(1)))) if m else text)
    if isinstance(data, dict) and data.get("format") == OLD_FORMAT:
        lessons = []
        for ex in data.get("examples") or []:
            if isinstance(ex, dict):
                lesson = make_lesson("x@" + str(ex.get("sender_domain") or ""), ex.get("subject") or "",
                                     ex.get("label"), ex.get("answer"), ex.get("prompt") or "")
                if lesson:
                    lessons.append(lesson)
        return {"format": FORMAT, "version": 2, "created": data.get("created") or "", "lessons": lessons}
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("That isn't a BotTrack shared learning pack.")
    return data


def developer():
    """Where packs go: the address the release was built with (or BOTTRACK_LOGS_TO). Unlike logs, never the user's
    own inbox, since a pack mailed to yourself teaches nobody."""
    import os
    return (os.environ.get("BOTTRACK_LOGS_TO") or logs.DEVELOPER_EMAIL or logs._built_in_address()).strip()


def send(pack, sender, password, version="", to=None, smtp=None):
    """Email a pack from the user's own Gmail to the developer address."""
    pack = public(pack)
    to = to or developer()
    if not to:
        raise RuntimeError("This copy of BotTrack has no developer address to share with.")
    msg = EmailMessage()
    msg["Subject"] = f"BotTrack shared learning ({len(pack['lessons'])} lessons, {version or 'unknown build'})"
    msg["From"], msg["To"] = sender, to
    msg.set_content("What this BotTrack's corrections taught: store domains, answers, cue words and field labels. "
                    "No email text or values.\nImport on the Training page (Shared learning), read it, then merge "
                    "into jigstream/shared/lessons.json.\n\n" + encode(pack))
    msg.add_attachment(json.dumps(pack, indent=1).encode(), maintype="application", subtype="json",
                       filename="bottrack-shared-learning.json")
    if smtp is None:
        smtp = smtplib.SMTP_SSL(logs.SMTP_HOST, 465, context=ssl.create_default_context(), timeout=30)
    with smtp as s:
        s.login(sender, password)
        s.send_message(msg)
    return to


def merge(packs):
    """Several packs as one, without repeats."""
    seen, out = set(), []
    for pack in packs:
        for ls in pack.get("lessons", []):
            ls = check(ls)
            if ls and _key(ls) not in seen:
                seen.add(_key(ls))
                out.append(ls)
    return {"format": FORMAT, "version": 2, "created": date.today().isoformat(), "lessons": out}


# ---- shared lessons on this Mac: the file that ships with the app plus packs imported here ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_lessons (
    id INTEGER PRIMARY KEY, source TEXT, sender_domain TEXT, lesson TEXT, lesson_key TEXT UNIQUE
);
"""


def ensure(con):
    from . import db
    db.setup_once(con, "packs2", lambda c: c.executescript(SCHEMA))
    if _has(con, "shared_examples"):  # older builds kept other people's scrubbed email text: keep the lessons only
        rows = [dict(r) for r in con.execute("SELECT source, sender_domain, subject, label, prompt, answer"
                                              " FROM shared_examples")]
        for r in rows:
            try:
                answer = json.loads(r["answer"] or "{}")
            except json.JSONDecodeError:
                continue
            lesson = make_lesson("x@" + (r["sender_domain"] or ""), r["subject"], r["label"], answer, r["prompt"] or "")
            if lesson:
                _insert(con, lesson, r["source"])
        con.execute("DROP TABLE shared_examples")
        con.commit()


def _insert(con, lesson, source):
    return con.execute("INSERT OR IGNORE INTO shared_lessons (source, sender_domain, lesson, lesson_key)"
                       " VALUES (?,?,?,?)", (source, lesson["sender_domain"], json.dumps(lesson, sort_keys=True),
                                             _key(lesson))).rowcount


def import_pack(con, pack, source):
    """Add a pack's lessons to this Mac, each checked again against the fixed lists. Returns how many were new."""
    ensure(con)
    own = _own_domains(private_words(con))
    n = 0
    for ls in pack.get("lessons") or []:
        clean = check(ls, own)
        if clean:
            n += _insert(con, clean, source)
    con.commit()
    return n


def built_in():
    try:
        return [ls for ls in (check(x) for x in decode(SHARED_FILE.read_text())["lessons"]) if ls]
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def shared(con):
    ensure(con)
    return built_in() + [json.loads(r[0]) for r in con.execute("SELECT lesson FROM shared_lessons")]


# ---- picking what's shown to the model for one email ----

TEXT_WORD_RE = re.compile(r"[a-z]{3,}")


def _words(text):
    return set(TEXT_WORD_RE.findall((text or "").lower())) - {"your", "the", "and", "for", "you", "has", "have", "with",
                                                              "from"}


def _same_store(a, b):
    return bool(a and b and (a == b or a.endswith("." + b) or b.endswith("." + a)))


def describe(lesson):
    """A lesson as one plain sentence for the model (and the page)."""
    ans = lesson.get("answer") or {}
    what = ans.get("what") or {"order": "an order", "botting_expense": "a botting expense", "sale": "a sale or payout",
                               "other": "not an order"}.get(ans.get("kind"), lesson.get("label") or "")
    if ans.get("shipping_status"):
        what += f" with shipping status {ans['shipping_status']}"
    if ans.get("category"):
        what += f" in category {ans['category']}"
    joined = '" / "'.join(lesson.get("cues") or [])
    cue = f' with "{joined}" in the subject' if joined else ""
    out = f"Emails from {lesson['sender_domain']}{cue} were {what}."
    names = {"order_number": "The order number", "tracking_number": "The tracking number", "total": "The total"}
    for k, label in (lesson.get("fields") or {}).items():
        out += f" {names.get(k, k)} comes after \"{label}\"."
    return out


class Picker:
    """Chooses what's shown before each email: the user's own closest corrected examples (by sender domain and
    subject), and as hints the shared lessons for the same store. With no close examples, the same default pair
    every time, which keeps the engine's prompt cache warm."""

    def __init__(self, own, lessons=(), default=(), per_email=PER_EMAIL):
        self.pool = [(o["sender_domain"], _words(o["subject"]), o["prompt"], o["answer"]) for o in own]
        self.lessons = list(lessons)
        self.default = list(default)
        self.per_email = per_email

    def __len__(self):
        return len(self.pool) + len(self.lessons) or len(self.default)

    def __call__(self, email):
        domain, words = _domain(email.sender), _words(email.subject)
        scored = []
        for i, (d, w, prompt, answer) in enumerate(self.pool):
            score = (3 if _same_store(d, domain) else 0) + 2 * len(words & w) / (len(words | w) or 1)
            if score >= 1:
                scored.append((score, -i, prompt, answer))
        scored.sort(reverse=True)
        picked = [(p[:EXAMPLE_CHARS], a) for _, _, p, a in scored[:self.per_email]]
        return picked or self.default

    def hint(self, email):
        """Shared lessons for this email's store, as a note added under the email. "" when there are none."""
        domain, subject = _domain(email.sender), (email.subject or "").lower()
        mine = [ls for ls in self.lessons if _same_store(ls.get("sender_domain"), domain)]
        if not mine:
            return ""
        mine.sort(key=lambda ls: -sum(c in subject for c in ls.get("cues") or []))
        lines = [describe(ls) for ls in mine[:HINTS_PER_EMAIL]]
        return ("\n\n(What other BotTrack users' corrections taught about this store. Use it only where this email "
                "agrees: " + " ".join(lines) + ")")


def _domain(sender):
    m = re.search(r"@([\w.-]+)", sender or "")
    return m.group(1).lower() if m else ""


def picker(store, default):
    """The Picker for a sync: this Mac's corrections plus the shared lessons."""
    own = []
    for ex in store.examples():
        own.append({"sender_domain": _domain(ex["sender"]), "subject": ex["subject"] or "",
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
    for ls in pack["lessons"]:
        ls["said"] = describe(ls)
    return render_template("training_share.html", pack=pack, own=len(store.examples()),
                           shared_n=len(shared(store.db)), built_in_n=len(built_in()), to=developer(),
                           accounts=[a for a, _ in Config.from_env().accounts],
                           sources=[dict(r) for r in store.db.execute(
                               "SELECT source, COUNT(*) AS n FROM shared_lessons GROUP BY source ORDER BY source")])


@bp.post("/training/share")
def share():
    from .config import Config
    store = _store()
    accounts = dict(Config.from_env().accounts)
    sender = request.form.get("sender") or next(iter(accounts), "")
    if not request.form.get("consent"):
        flash("Tick the box to say it's OK to share these lessons.")
    elif sender not in accounts:
        flash("Connect a Gmail inbox on Setup first; the pack is sent from it.")
    else:
        keep = {int(v) for v in request.form.getlist("keep") if v.isdigit()}
        pack = build(store.db, store, only=keep)
        if not pack["lessons"]:
            flash("Nothing is ticked to share.")
        else:
            try:
                from .updater import installed_version
                send(pack, sender, accounts[sender], installed_version())
                flash(f"Shared {len(pack['lessons'])} lessons. Thank you! They'll reach everyone in a coming update.")
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
        flash(f"Added {n} new lesson{'s' if n != 1 else ''} ({len(pack['lessons']) - n} were already here or "
              "didn't pass the check).")
    except (ValueError, json.JSONDecodeError, OSError) as e:
        flash(f"Couldn't read that pack: {str(e)[:200]}")
    return redirect(url_for("packs.page"))


@bp.get("/training/share/look")
def look():
    """Every lesson from one imported pack, to read before it's merged into the shared file."""
    store = _store()
    source = request.args.get("source", "")
    rows = []
    for r in store.db.execute("SELECT id, lesson FROM shared_lessons WHERE source = ? ORDER BY id", (source,)):
        lesson = json.loads(r["lesson"])
        rows.append({"id": r["id"], "said": describe(lesson), "lesson": lesson})
    return render_template("training_share_look.html", source=source, rows=rows)


@bp.post("/training/share/drop")
def drop():
    """Take one shared lesson out (it teaches the wrong thing)."""
    store = _store()
    lid = request.form.get("id", type=int)
    row = store.db.execute("SELECT source FROM shared_lessons WHERE id = ?", (lid,)).fetchone()
    store.db.execute("DELETE FROM shared_lessons WHERE id = ?", (lid,))
    store.db.commit()
    return redirect(url_for("packs.look", source=row["source"]) if row else url_for("packs.page"))


@bp.post("/training/share/forget")
def forget():
    store = _store()
    store.db.execute("DELETE FROM shared_lessons WHERE source = ?", (request.form.get("source", ""),))
    store.db.commit()
    return redirect(url_for("packs.page"))


@bp.get("/training/share/export.json")
def export_merged():
    """Every shared lesson on this Mac in one pack: for the developer to ship as jigstream/shared/lessons.json."""
    store = _store()
    merged = merge([{"lessons": shared(store.db)}])
    return Response(json.dumps(merged, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=lessons.json"})
