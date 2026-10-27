"""Prices held against the emails they came from.

Many stores draw the cents small next to the dollars ($45<sup>00</sup>). Before the email reader put the dot back,
the AI saw "$4500" and saved 4500. Two fixes live here:

- Re-check: every order and receipt saved before the fix is opened again (read-only, during a sync, once). When
  the saved price isn't in any of its emails but the same number with the decimal put back is, the price is fixed.
  Each fix is listed in price_fixes.
- Fix it by hand: change a price on Go through and the order's emails are read again by the AI and kept as
  examples with the right price, so it learns how that store writes its prices.
"""
import json
import logging
import threading
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, request, url_for

from . import db
from .extract import amounts_in, email_prompt, extract_raw, find_total

logger = logging.getLogger("jigstream.pricecheck")
bp = Blueprint("pricecheck", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_recheck (kind TEXT PRIMARY KEY, upto INTEGER, next_id INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS price_fixes (
    id INTEGER PRIMARY KEY, kind TEXT, row_id INTEGER, old REAL, new REAL, how TEXT, at TEXT
);
"""
CONFIRMATION = ("order", "confirm", "receipt", "thank", "purchase", "invoice", "payment")


def ensure(con):
    return db.setup_once(con, "pricecheck", lambda c: c.executescript(SCHEMA))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fixed_price(saved, emails):
    """(the right price, how) for a saved price, from its emails; (None, "") when it stands."""
    if saved is None or not emails:
        return None, ""
    shown = [amounts_in(e.body) for e in emails]
    if not any(shown) or any(round(saved, 2) in s for s in shown):
        return None, ""
    if any(round(saved / 100, 2) in s for s in shown):
        return round(saved / 100, 2), "decimal"
    for e, s in zip(emails, shown):
        if s and any(w in (e.subject or "").lower() for w in CONFIRMATION):
            found = find_total(e.body)
            if found is not None and found != saved:
                return found, "total line"
    return None, ""


def _fixed_items(items, emails):
    shown = set().union(*(amounts_in(e.body) for e in emails)) if emails else set()
    changed = False
    for it in items:
        p = it.get("price") if isinstance(it, dict) else None
        if isinstance(p, (int, float)) and round(p, 2) not in shown and round(p / 100, 2) in shown:
            it["price"], changed = round(p / 100, 2), True
    return changed


def _accounts_for(con, ids):
    q = ",".join("?" * len(ids))
    return {r["message_id"]: r["account"] for r in con.execute(f"SELECT message_id, account FROM emails WHERE message_id IN ({q})", ids)}


def _open(config, con, ids, fetch=None):
    """{message_id: Email} for these ids, one sign-in per inbox."""
    from .mail import fetch_many
    fetch = fetch or fetch_many
    passwords = dict(config.accounts)
    by_acct = {}
    for mid, acct in _accounts_for(con, ids).items():
        if acct in passwords:
            by_acct.setdefault(acct, []).append(mid)
    out = {}
    for acct, mids in by_acct.items():
        for mid, email in fetch(config, acct, passwords[acct], mids):
            if email is not None:
                out[mid] = email
    return out


def recheck(config, store, fetch=None, stop=None, batch=40):
    """Re-check prices saved before the fix, a batch of rows at a time, carrying on where the last sync stopped.
    Returns how many prices were fixed."""
    con = ensure(store.db)
    stop = stop or (lambda: False)
    fixed = 0
    for kind, table, price in (("order", "tracked_orders", "total"), ("expense", "expenses", "amount")):
        if con.execute("SELECT 1 FROM price_recheck WHERE kind=?", (kind,)).fetchone() is None:
            upto = con.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            con.execute("INSERT INTO price_recheck (kind, upto, next_id) VALUES (?,?,0)", (kind, upto))
            con.commit()
        upto, next_id = con.execute("SELECT upto, next_id FROM price_recheck WHERE kind=?", (kind,)).fetchone()
        while next_id <= upto and not stop():
            cols = "id, total, message_ids, items, COALESCE(total_manual, 0) AS manual" if kind == "order" else \
                "id, amount, message_id, COALESCE(amount_manual, 0) AS manual"
            rows = con.execute(f"SELECT {cols} FROM {table} WHERE id >= ? AND id <= ? AND {price} IS NOT NULL "
                               "ORDER BY id LIMIT ?", (next_id, upto, batch)).fetchall()
            if not rows:
                next_id = upto + 1
                break
            ids = {r["id"]: (json.loads(r["message_ids"] or "[]") if kind == "order" else [r["message_id"]]) for r in rows}
            wanted = [m for ms in ids.values() for m in ms if m and not m.startswith("manual:")]
            try:
                emails = _open(config, con, wanted, fetch) if wanted else {}
            except Exception as e:  # Gmail unreachable: try again next sync from the same place
                logger.warning("re-checking prices stopped: %s", e)
                return fixed
            for r in rows:
                if r["manual"]:
                    continue
                mails = [emails[m] for m in ids[r["id"]] if m in emails]
                new, how = fixed_price(r[price], mails)
                if kind == "order":
                    items = json.loads(r["items"] or "[]")
                    if _fixed_items(items, mails):
                        con.execute("UPDATE tracked_orders SET items=? WHERE id=?", (json.dumps(items), r["id"]))
                if new is not None:
                    con.execute(f"UPDATE {table} SET {price}=? WHERE id=?", (new, r["id"]))
                    con.execute("INSERT INTO price_fixes (kind, row_id, old, new, how, at) VALUES (?,?,?,?,?,?)",
                                (kind, r["id"], r[price], new, how, now()))
                    logger.info("%s %s: price %.2f -> %.2f (%s)", kind, r["id"], r[price], new, how)
                    fixed += 1
            next_id = rows[-1]["id"] + 1
            con.execute("UPDATE price_recheck SET next_id=? WHERE kind=?", (next_id, kind))
            con.commit()
    if fixed:
        logger.info("re-checked saved prices: %d fixed", fixed)
    return fixed


def set_price(store, kind, row_id, value):
    """A price changed by hand: kept (a sync never changes it again) and listed with the fixes."""
    con = ensure(store.db)
    table, col, flag = ("tracked_orders", "total", "total_manual") if kind == "order" else ("expenses", "amount", "amount_manual")
    row = con.execute(f"SELECT {col} FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        return None
    con.execute(f"UPDATE {table} SET {col}=?, {flag}=1 WHERE id=?", (value, row_id))
    con.execute("INSERT INTO price_fixes (kind, row_id, old, new, how, at) VALUES (?,?,?,?,'by hand',?)",
                (kind, row_id, row[0], value, now()))
    con.commit()
    return row[0]


def learn(config, store, llm, kind, row_id, value, fetch=None):
    """The AI reads the emails behind a hand-fixed price again; each one that shows the price (or the order's
    confirmation) is kept as an example with the right price. Returns a one-line result."""
    from .training import few_shot
    con = store.db
    if kind == "order":
        row = con.execute("SELECT message_ids FROM tracked_orders WHERE id=?", (row_id,)).fetchone()
        ids = json.loads(row["message_ids"] or "[]") if row else []
    else:
        row = con.execute("SELECT message_id FROM expenses WHERE id=?", (row_id,)).fetchone()
        ids = [row["message_id"]] if row else []
    ids = [m for m in ids if m and not m.startswith("manual:")]
    emails = _open(config, con, ids, fetch) if ids else {}
    if not emails:
        return "Price saved. Its emails couldn't be opened, so there was nothing to learn from."
    examples, kept, said = few_shot(store), 0, []
    for mid in ids:
        email = emails.get(mid)
        if email is None:
            continue
        shown = amounts_in(email.body)
        if shown and round(value, 2) not in shown and not any(w in (email.subject or "").lower() for w in CONFIRMATION):
            continue  # a shipping update that never states the price teaches nothing about it
        prompt = email_prompt(email, config.max_body_chars)
        raw, _ = extract_raw(llm, email, config.max_body_chars, examples)
        if raw.get("kind") not in ("order", "botting_expense"):
            raw = dict(raw, kind="order" if kind == "order" else "botting_expense")
        corrected = dict(raw, total=value)
        store.add_price_example(email, prompt, raw, corrected)
        said.append(raw.get("total"))
        kept += 1
    msg = f"The AI re-read {kept} email{'s' if kept != 1 else ''} and keeps {value:.2f} as the right price."
    if said:
        msg += f" It had read {', '.join(str(s) for s in said)}."
    logger.info("price fix on %s %s: %s", kind, row_id, msg)
    return msg


def _learn_later(app, kind, row_id, value):
    from .config import Config
    from .llm import from_config
    from .orderstore import Store

    def run():
        with app.app_context():
            try:
                config = Config.from_env()
                if config.accounts:
                    learn(config, Store(db.connect(app.config.get("DB_PATH"))), from_config(config), kind, row_id, value)
            except Exception:
                logger.exception("learning from a price fix failed")
    threading.Thread(target=run, daemon=True).start()


@bp.post("/prices/<kind>/<int:row_id>")
def change(kind, row_id):
    from .orderstore import Store
    from .mobile import back_url
    back = request.form.get("back") or back_url()
    if kind not in ("order", "expense"):
        return redirect(back or url_for("orders"))
    try:
        value = round(float(str(request.form.get("price", "")).replace("$", "").replace(",", "").strip()), 2)
    except ValueError:
        flash("Type the price as a number, like 45.00.")
        return redirect(back or url_for("orders"))
    if value < 0 or value > 1_000_000:
        flash("That price doesn't look right.")
        return redirect(back or url_for("orders"))
    old = set_price(Store(db.connect(current_app.config.get("DB_PATH"))), kind, row_id, value)
    if old is None:
        flash("That one is gone.")
    else:
        _learn_later(current_app._get_current_object(), kind, row_id, value)
        flash(f"Price changed to ${value:,.2f}" + (f" (was ${old:,.2f})" if old is not None else "") +
              ". The AI is re-reading its emails to learn how this store writes prices.")
    return redirect(back or url_for("orders"))
