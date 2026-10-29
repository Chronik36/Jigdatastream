"""Resale products: the words for what you buy to flip (Pokémon, One Piece, NeeDoh...), from any store.

A purchase that names one of them is an order wherever it was bought: a warehouse or grocery store
(Sam's Club, Aldi), an in-store e-receipt, a pickup. There is no store list; the products decide. On a
mixed receipt (groceries and a few booster packs) only the resale items are kept.

The words are yours to change on the Training page. Pokémon, One Piece and NeeDoh come built in and can be
removed too. Adding a word re-checks a year of mail for receipts that name it.

A product word alone never makes an order: an ad listing Pokémon cards for sale is still an ad. The email
must show a real purchase (an order number, a receipt, a payment, a tracking number); see
extract.purchase_proof.
"""
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone

from . import db

logger = logging.getLogger("jigstream.resale")

DEFAULT_WORDS = ["Pokémon", "One Piece", "NeeDoh"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS resale_words (
    word TEXT PRIMARY KEY COLLATE NOCASE,
    removed INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS resale_state (key TEXT PRIMARY KEY, value TEXT);
-- Orders taken out because their emails were ads (no order number, receipt or payment), kept so Put back works.
CREATE TABLE IF NOT EXISTS ad_orders (order_id INTEGER PRIMARY KEY, row TEXT, subject TEXT, removed_at TEXT);
CREATE TABLE IF NOT EXISTS ad_checked (order_id INTEGER PRIMARY KEY);
"""


def ensure(con):
    return db.setup_once(con, "resale", lambda c: (c.executescript(SCHEMA), c.commit()))


def fold(text):
    """Lower case without accents, so Pokémon, POKEMON and pokemon read the same."""
    return "".join(c for c in unicodedata.normalize("NFKD", (text or "").lower()) if not unicodedata.combining(c))


def words(con):
    """Your resale words: the built-in ones you haven't removed, then the ones you added."""
    ensure(con)
    rows = {fold(r["word"]): dict(r) for r in con.execute("SELECT * FROM resale_words ORDER BY created_at")}
    out = [w for w in DEFAULT_WORDS if not (rows.get(fold(w)) or {}).get("removed")]
    have = {fold(w) for w in out}
    out += [r["word"] for k, r in rows.items() if not r["removed"] and k not in have]
    return out


def add(con, word):
    word = re.sub(r"\s+", " ", (word or "").strip())[:40]
    if len(fold(word).strip()) < 3:
        return None
    ensure(con)
    if fold(word) in {fold(w) for w in words(con)}:
        return word
    builtin = next((w for w in DEFAULT_WORDS if fold(w) == fold(word)), None)
    con.execute("DELETE FROM resale_words WHERE word = ?", (builtin or word,))
    if not builtin:
        con.execute("INSERT INTO resale_words (word, removed, created_at) VALUES (?, 0, ?)",
                    (word, datetime.now(timezone.utc).isoformat()))
    con.commit()
    logger.info("resale word added: %s", word)
    return builtin or word


def remove(con, word):
    ensure(con)
    builtin = next((w for w in DEFAULT_WORDS if fold(w) == fold(word)), None)
    con.execute("DELETE FROM resale_words WHERE word = ?", (word,))
    if builtin:  # remembered, so it doesn't come back
        con.execute("INSERT OR REPLACE INTO resale_words (word, removed, created_at) VALUES (?, 1, ?)",
                    (builtin, datetime.now(timezone.utc).isoformat()))
    con.commit()
    logger.info("resale word removed: %s", word)


def _pattern(word):
    """NeeDoh also matches "Nee Doh" and "nee-doh"; One Piece also "one-piece" and "OnePiece"."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", word)  # NeeDoh -> Nee Doh
    tokens = [t for t in re.split(r"[^0-9a-z]+", fold(spaced)) if t]
    return r"(?<![0-9a-z])" + r"[\s\-_.']*".join(map(re.escape, tokens)) + r"(?![0-9a-z])" if tokens else None


def matcher(word_list):
    """A function text -> the words it names (as you wrote them), or None when there are no words."""
    pats = [(w, re.compile(p)) for w in word_list if (p := _pattern(w))]
    if not pats:
        return None

    def named(text):
        t = fold(text)
        return [w for w, p in pats if p.search(t)]
    return named


def hits(email, word_list):
    named = matcher(word_list)
    return named(f"{email.subject}\n{(email.body or '')[:30000]}") if named else []


def is_resale(name, word_list):
    named = matcher(word_list)
    return bool(named and named(name or ""))


ITEM_LINE = re.compile(r"^\s*(?:(\d{1,3})\s*[x@]?\s+)?(.*?[A-Za-z].*?)\s+\$?\s?(\d{1,5}\.\d{2})\s*[A-Z]{0,2}\s*$")


def items_from_lines(body, word_list):
    """Receipt lines that name a resale word, read straight from the email: "POKEMON TCG BOOSTER   19.98".
    For a receipt the model wouldn't itemize."""
    named = matcher(word_list)
    out = []
    for line in (body or "").splitlines()[:600]:
        if not named or not named(line):
            continue
        m = ITEM_LINE.match(line)
        name = (m.group(2) if m else line).strip(" -:\t*")[:120]
        if not name or len(out) >= 30:
            continue
        out.append({"name": name, "quantity": int(m.group(1)) if m and m.group(1) else 1,
                    "price": float(m.group(3)) if m else None})
    return out


def keep_resale(result, word_list, rescued=False):
    """A receipt from a store visit (no tracking, no ship-to) or one the model didn't call an order keeps only
    its resale items; online orders stay whole. When items were dropped and every kept one has a price, the
    total becomes what the resale items cost, so groceries don't count as spend."""
    items = result.get("items") or []
    if not (rescued or (not result.get("tracking_number") and not result.get("shipping_address"))):
        return result
    kept = [i for i in items if is_resale(i.get("name"), word_list)]
    if not kept or len(kept) == len(items):
        return result
    out = dict(result, items=kept)
    if all(isinstance(i.get("price"), (int, float)) for i in kept):
        out["total"] = round(sum(i["price"] * (i.get("quantity") or 1) for i in kept), 2)
    logger.info("mixed receipt from %s: kept %d resale item(s) of %d (receipt total %s, counted %s)",
                result.get("retailer"), len(kept), len(items), result.get("total"), out.get("total"))
    return out


def signature(word_list):
    return json.dumps(sorted(fold(w) for w in word_list))


def needs_recheck(con):
    """True when your words changed since the last re-check of old mail (and on the first run)."""
    ensure(con)
    row = con.execute("SELECT value FROM resale_state WHERE key = 'checked'").fetchone()
    return (row[0] if row else None) != signature(words(con))


def mark_checked(con, word_list):
    ensure(con)
    con.execute("INSERT OR REPLACE INTO resale_state VALUES ('checked', ?)", (signature(word_list),))
    con.commit()


def state(con, key, value=None):
    ensure(con)
    if value is None:
        row = con.execute("SELECT value FROM resale_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    con.execute("INSERT OR REPLACE INTO resale_state VALUES (?, ?)", (key, value))
    con.commit()
    return value


def gmail_query(word_list):
    """A Gmail search for every email naming a resale word, accented or not."""
    terms = []
    for w in word_list:
        for form in dict.fromkeys([w, fold(w)]):
            terms.append(f'"{form}"' if " " in form else form)
    return f"({' OR '.join(dict.fromkeys(terms))}) -in:trash" if terms else None


# ---------- ads that were saved as orders ----------

def ad_candidates(con, limit=300):
    """Orders an ad could have become: read from email only (never typed, edited or tracked), no tracking number,
    not shipped, and not checked before."""
    ensure(con)
    have = {r[1] for r in con.execute("PRAGMA table_info(tracked_orders)")}
    edited = " AND COALESCE(total_manual, 0) = 0" if "total_manual" in have else ""
    return [dict(r) for r in con.execute(
        "SELECT * FROM tracked_orders WHERE COALESCE(status_manual, 0) = 0" + edited +
        " AND COALESCE(tracking_number, '') = '' AND COALESCE(shipping_status, 'unknown') IN ('ordered', 'unknown', 'processing')"
        " AND COALESCE(message_ids, '[]') != '[]' AND message_ids NOT LIKE '%manual:%'"
        " AND id NOT IN (SELECT order_id FROM ad_checked) ORDER BY id DESC LIMIT ?", (limit,))]


def take_out_ad(con, order, subject=""):
    """Take an order out because its emails are ads. Its emails count as "not relevant"; Put back undoes it."""
    ensure(con)
    ids = json.loads(order.get("message_ids") or "[]")
    con.execute("INSERT OR REPLACE INTO ad_orders VALUES (?, ?, ?, ?)",
                (order["id"], json.dumps(order, default=str), subject, datetime.now(timezone.utc).isoformat()))
    con.executemany("UPDATE emails SET kind = 'other' WHERE message_id = ?", [(i,) for i in ids])
    con.execute("DELETE FROM tracked_orders WHERE id = ?", (order["id"],))
    con.execute("INSERT OR IGNORE INTO ad_checked VALUES (?)", (order["id"],))
    con.commit()
    logger.info("order %s (%s, %s) taken out: its emails are ads (%s)", order["id"], order.get("retailer"),
                order.get("total"), subject[:80])


def mark_ad_checked(con, order_id):
    ensure(con)
    con.execute("INSERT OR IGNORE INTO ad_checked VALUES (?)", (order_id,))
    con.commit()


def ad_orders(con):
    ensure(con)
    out = []
    for r in con.execute("SELECT * FROM ad_orders ORDER BY removed_at DESC"):
        o = json.loads(r["row"])
        out.append({"id": r["order_id"], "retailer": o.get("retailer"), "total": o.get("total"),
                    "currency": o.get("currency"), "subject": r["subject"], "removed_at": r["removed_at"],
                    "items": [i.get("name") for i in json.loads(o.get("items") or "[]") if isinstance(i, dict)]})
    return out


def put_back(con, order_id):
    """It was an order after all: back on Orders as it was, and never taken out again."""
    ensure(con)
    r = con.execute("SELECT row FROM ad_orders WHERE order_id = ?", (order_id,)).fetchone()
    if not r:
        return False
    o = json.loads(r["row"])
    cols = [c[1] for c in con.execute("PRAGMA table_info(tracked_orders)") if c[1] in o]
    con.execute(f"INSERT OR REPLACE INTO tracked_orders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [o[c] for c in cols])
    con.executemany("UPDATE emails SET kind = 'order' WHERE message_id = ?",
                    [(i,) for i in json.loads(o.get("message_ids") or "[]")])
    con.execute("DELETE FROM ad_orders WHERE order_id = ?", (order_id,))
    con.execute("INSERT OR IGNORE INTO ad_checked VALUES (?)", (order_id,))
    con.commit()
    return True


# ---------- the Resale products page (Training) ----------

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for  # noqa: E402

bp = Blueprint("resale", __name__)


def _con():
    return ensure(db.connect(current_app.config.get("DB_PATH")))


@bp.get("/training/resale")
def page():
    c = _con()
    find = getattr(current_app, "resale_state", None) or {"running": False, "last": None, "progress": None}
    return render_template("training_resale.html", words=words(c), defaults=DEFAULT_WORDS, ads=ad_orders(c),
                           find=find, pending=needs_recheck(c))


@bp.post("/training/resale/words")
def words_save():
    c = _con()
    if request.form.get("remove"):
        remove(c, request.form["remove"])
        flash(f"Removed {request.form['remove']}. Orders already saved stay.")
    else:
        word = add(c, request.form.get("word"))
        if not word:
            flash("Type a product word of 3 letters or more.")
        elif getattr(current_app, "start_find_resale", None) and current_app.start_find_resale():
            flash(f"Added {word}. Re-checking a year of mail for receipts that name it.")
        else:
            flash(f"Added {word}. The next sync re-checks a year of mail for receipts that name it.")
    return redirect(url_for("resale.page"))


@bp.post("/training/resale/recheck")
def recheck():
    start = getattr(current_app, "start_find_resale", None)
    flash("Re-checking a year of mail for receipts with your resale items." if start and start(force=True)
          else "Can't start now: a sync or another check is running, or no inbox is set up. Try again after it.")
    return redirect(url_for("resale.page"))


@bp.post("/training/resale/ads/<int:order_id>/back")
def ad_back(order_id):
    flash("Put back on Orders." if put_back(_con(), order_id) else "That one is already back.")
    return redirect(url_for("resale.page"))
