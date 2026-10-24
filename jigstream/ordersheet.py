"""One order at a glance: tap an order on Orders or Shipping and its sheet opens over the page.

The sheet shows the product (with its picture when the Products tab knows it), quick buttons (copy the order and
tracking numbers, open the carrier, share, rename, change the status, delete), the details, where it ships, and a
timeline built from the order's own emails plus every new line the carrier has said about it (tracking_events,
written by carriers.refresh).
"""
import json
import logging
import re
import threading
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import db

logger = logging.getLogger("jigstream.ordersheet")
bp = Blueprint("ordersheet", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracking_events (
    id INTEGER PRIMARY KEY, order_id INTEGER, at TEXT, text TEXT, source TEXT
);
CREATE INDEX IF NOT EXISTS tracking_events_order ON tracking_events (order_id, at);
"""
SHOWN = 10  # timeline lines before "+ N more"


def ensure(con):
    return db.setup_once(con, "ordersheet", lambda c: c.executescript(SCHEMA))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def note_carrier(con, order_id, text):
    """A carrier status line, kept in the order's timeline when it says something new."""
    text = (text or "").strip()
    if not text or text.startswith("Couldn't check"):
        return
    ensure(con)
    last = con.execute("SELECT text FROM tracking_events WHERE order_id=? ORDER BY at DESC, id DESC LIMIT 1",
                       (order_id,)).fetchone()
    if last is None or last[0] != text:
        con.execute("INSERT INTO tracking_events (order_id, at, text, source) VALUES (?,?,?, 'carrier')",
                    (order_id, now(), text[:300]))


def _words(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 1}


def picture(name, products=None):
    """The picture of the product this item is, from the Products tab (best word match), or ""."""
    if products is None:
        from . import products as prod
        products = prod.load().get("products", [])
    want = _words(name)
    if not want:
        return ""
    best, score = "", 0.0
    for p in products:
        img = p.get("image") or ""
        if not img:
            continue
        have = _words(p.get("name"))
        if not have:
            continue
        s = len(want & have) / max(len(have), 1)
        if s > score:
            best, score = img, s
    return best if score >= 0.6 else ""


def _when(text):
    """(sort key, how it reads) for a stored date."""
    try:
        d = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return text or "", text or ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    local = d if len(text) <= 10 else d.astimezone()  # a plain date stays that day
    day = f"{local:%b} {local.day}"  # no %-d: Windows doesn't know it
    shown = f"{day}, {local.year}" if len(text) <= 10 else f"{day}, {local.hour % 12 or 12}:{local:%M %p}"
    return d.astimezone(timezone.utc).isoformat(), shown


def timeline(con, order):
    """[{at, when, text, where, source}] newest first: the order's emails and what the carrier said."""
    ensure(con)
    out = []
    ids = order.get("message_ids") or []
    if ids:
        q = ",".join("?" * len(ids))
        for r in con.execute(f"SELECT subject, date FROM emails WHERE message_id IN ({q})", ids):
            if r["subject"]:
                out.append((r["date"] or "", r["subject"], "Email", "email"))
    for r in con.execute("SELECT at, text FROM tracking_events WHERE order_id=?", (order["id"],)):
        out.append((r["at"] or "", r["text"], order.get("carrier") or "Carrier", "carrier"))
    if order.get("order_date") and not out:
        out.append((order["order_date"], "Ordered", order.get("retailer") or "", "order"))
    events = []
    for at, text, where, source in out:
        key, shown = _when(at)
        events.append({"at": key, "when": shown, "text": text, "where": where, "source": source})
    return sorted(events, key=lambda e: e["at"], reverse=True)


def quantity(order):
    total = 0
    for i in order.get("items") or []:
        try:
            total += int(i.get("quantity") or 1)
        except (TypeError, ValueError):
            total += 1
    return total


def backfill_pictures(config, store, fetch=None, stop=None, batch=40):
    """Orders saved before pictures were kept: their emails are opened once (read-only, during syncs, carrying on
    where the last one stopped) for product photos. Returns how many orders got one."""
    from . import pricecheck
    from .orderstore import with_pictures
    con = pricecheck.ensure(store.db)
    stop = stop or (lambda: False)
    if con.execute("SELECT 1 FROM price_recheck WHERE kind='picture'").fetchone() is None:
        upto = con.execute("SELECT COALESCE(MAX(id), 0) FROM tracked_orders").fetchone()[0]
        con.execute("INSERT INTO price_recheck (kind, upto, next_id) VALUES ('picture', ?, 0)", (upto,))
    upto, next_id = con.execute("SELECT upto, next_id FROM price_recheck WHERE kind='picture'").fetchone()
    found = 0
    while next_id <= upto and not stop():
        rows = con.execute("SELECT id, items, message_ids FROM tracked_orders WHERE id >= ? AND id <= ? ORDER BY id LIMIT ?",
                           (next_id, upto, batch)).fetchall()
        if not rows:
            next_id = upto + 1
            break
        todo = {}
        for r in rows:
            items = json.loads(r["items"] or "[]")
            if items and not all(isinstance(i, dict) and i.get("image") for i in items):
                todo[r["id"]] = (items, [m for m in json.loads(r["message_ids"] or "[]") if m and not m.startswith("manual:")])
        wanted = [m for _, ms in todo.values() for m in ms]
        try:
            emails = pricecheck._open(config, con, wanted, fetch) if wanted else {}
        except Exception as e:  # Gmail unreachable: carry on next sync
            logger.warning("finding product pictures stopped: %s", e)
            break
        for oid, (items, ms) in todo.items():
            pics = [p for m in ms if m in emails for p in emails[m].images]
            if with_pictures(items, pics):
                con.execute("UPDATE tracked_orders SET items=? WHERE id=?", (json.dumps(items), oid))
                found += 1
        next_id = rows[-1]["id"] + 1
        con.execute("UPDATE price_recheck SET next_id=? WHERE kind='picture'", (next_id,))
    con.execute("UPDATE price_recheck SET next_id=? WHERE kind='picture'", (next_id,))
    if found:
        logger.info("product pictures found for %d older order(s)", found)
    return found


def _con():
    return db.connect(current_app.config.get("DB_PATH"))


def _back(order_id=None):
    ref = request.form.get("back") or request.referrer or ""
    if not ref.startswith(request.host_url):
        ref = url_for("orders")
    return ref


@bp.get("/orders/<int:order_id>/sheet")
def sheet(order_id):
    from .app import STATUS_CHOICES
    from .orderstore import Store
    store = Store(_con())
    o = store.order(order_id)
    if o is None:
        return "<p class='muted' style='padding:24px'>That order is gone.</p>", 404
    events = timeline(store.db, o)
    first = (o["items"] or [{}])[0] if o["items"] else {}
    nice = lambda d: _when(d)[1] if d else ""  # noqa: E731
    return render_template("_order_sheet.html", o=o, events=events, shown=SHOWN, qty=quantity(o),
                           date=nice(o["order_date"]), eta=nice(o["estimated_delivery"]),
                           title=first.get("name") or o.get("retailer") or "Order",
                           image=first.get("image") or picture(first.get("name")), choices=STATUS_CHOICES)


@bp.post("/orders/<int:order_id>/rename")
def rename(order_id):
    from .orderstore import Store
    store = Store(_con())
    o = store.order(order_id)
    name = (request.form.get("name") or "").strip()[:200]
    if o and name:
        items = o["items"] or [{}]
        items[0] = dict(items[0], name=name)
        store.db.execute("UPDATE tracked_orders SET items=? WHERE id=?", (json.dumps(items), order_id))
        flash(f"Renamed to {name}.")
    return redirect(_back())


@bp.post("/orders/<int:order_id>/delete")
def delete(order_id):
    from .orderstore import Store
    store = Store(_con())
    o = store.order(order_id)
    if o:
        store.delete_order(order_id)
        logger.info("order %s (%s %s) deleted by hand", order_id, o["retailer"], o["order_number"])
        flash(f"Deleted {o['retailer'] or 'the order'} {o['order_number'] or ''}. Undo brings it back.")
    return redirect(_back())


@bp.post("/orders/<int:order_id>/refresh")
def refresh(order_id):
    import os
    from . import carriers
    if not carriers.available(os.environ):
        flash("Turn on key-free tracking or add a carrier key on the Setup page to check packages.")
        return redirect(_back())
    path = current_app.config.get("DB_PATH")
    threading.Thread(target=lambda: carriers.refresh(db.connect(path), dict(os.environ), only_id=order_id),
                     daemon=True).start()
    flash("Asking the carrier about this package. Open it again in a minute.")
    return redirect(_back())
