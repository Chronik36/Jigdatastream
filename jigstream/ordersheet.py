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
    """Back to the page the sheet was opened on (a path on BotTrack only), else Orders."""
    from .mobile import back_url
    ref = request.form.get("back") or ""
    if not (ref.startswith("/") and not ref.startswith("//")):
        ref = back_url() or url_for("orders")
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
    groups = {}
    for r in store.db.execute("SELECT name, group_name FROM profiles ORDER BY lower(name)"):
        groups.setdefault(r["group_name"] or "No group", []).append(r["name"])
    from . import addresscheck, sales, stuck
    from .addresscheck import _line
    platform = sales.platform_in(store.db, o.get("retailer") or "") or ""
    # The bot checkouts behind this order and the profile it's tied to, with the address check.
    try:
        checkouts = [c for c in stuck.cross_reference(store.db) if c.get("order") and c["order"]["id"] == order_id]
    except Exception:
        checkouts = []
    prof = store.db.execute("SELECT * FROM profiles WHERE name = ?", (o.get("profile_name"),)).fetchone() \
        if o.get("profile_name") else None
    profile = dict(name=prof["name"], group=prof["group_name"], email=prof["email"], address=_line(prof)) if prof else None
    return render_template("_order_sheet.html", o=o, events=events, checkouts=checkouts, profile=profile,
                           wrong=addresscheck.check(store.db, [o]).get(order_id), shown=SHOWN, qty=quantity(o),
                           sale_platform=platform, sale_platforms=sales.platforms(store.db),
                           fee_pct=sales.fee_pct(store.db, platform or "eBay"),
                           bought=[c for c in sales.order_choices(store.db) if c["id"] != order_id],
                           profile_groups=dict(sorted(groups.items(), key=lambda g: g[0].lower())),
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


def _pct(text):
    v = _money(text)
    return v if v is not None and 0 <= v < 100 else None


@bp.post("/orders/<int:order_id>/sale")
def as_sale(order_id):
    """This "order" was a sale (an eBay "you sold it" email read as a purchase): it leaves Orders and becomes a sale
    on Profit goal with what it sold for, the fees and what you got, tied to the order you bought the item with.
    The AI re-reads its emails as a sale with your numbers and learns from it."""
    from . import inbox, sales
    from .config import Config
    from .llm import from_config
    from .orderstore import Store
    store = Store(_con())
    o = store.order(order_id)
    if o is None:
        flash("That order is gone.")
        return redirect(_back())
    f = request.form
    fields = {"platform": (f.get("platform") or "").strip()[:40] or "eBay",
              "item": (f.get("item") or "").strip()[:200] or None, "size": (f.get("size") or "").strip() or None,
              "date": (f.get("date") or "").strip() or None, "sale_price": _money(f.get("sale_price")),
              "fees": _money(f.get("fees")), "payout": _money(f.get("payout")), "fee_pct": _pct(f.get("fee_pct")),
              "cost": _money(f.get("cost")), "order_id": f.get("bought", type=int) or None}
    if fields["sale_price"] is None and fields["payout"] is None:
        flash("Enter what it sold for or what you got.")
        return redirect(_back())
    ids = o.get("message_ids") or []
    mails = [dict(r) for r in store.db.execute(
        "SELECT message_id, account, sender, subject, date FROM emails WHERE message_id IN (%s)"
        % ",".join("?" * len(ids)), ids)] if ids else []
    store.delete_order(order_id)
    sale_id = sales.from_order(store.db, o, fields)
    s = dict(store.db.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone())
    logger.info("order %s (%s) turned into a %s sale %s by hand", order_id, o.get("retailer"), s["platform"], sale_id)
    config = Config.from_env()
    if mails and config.accounts:
        threading.Thread(target=inbox.label_emails, args=(config, Store(_con()), from_config(config), mails, "sale", s),
                         daemon=True).start()
    linked = " Linked to the order you bought it with." if s["order_id"] else ""
    flash(f"Moved to Sales: {s['platform']} sale, you got ${s['payout'] or 0:,.2f}.{linked}"
          + (" The AI re-reads its emails as a sale and learns from it." if mails else ""))
    return redirect(_back())


EDITABLE = ("retailer", "order_number", "order_date", "total", "shipping_name", "shipping_address", "carrier",
            "tracking_number", "estimated_delivery")


def _money(text):
    try:
        return round(float(re.sub(r"[^\d.\-]", "", text or "")), 2)
    except ValueError:
        return None


def _shows(body, field, value):
    """Whether an email shows this value, so the fix is only taught from emails that contain it."""
    text = (body or "").upper()
    if value in (None, ""):
        return False
    if field == "total":
        return f"{value:,.2f}" in text or f"{value:.2f}" in text
    if field == "shipping_address":
        from . import address as A
        from .profilematch import parse_order_address
        p = parse_order_address(value)
        house = (A.base_line1(value).split() or [""])[0]
        return bool(house) and house in text and (not p["zip"] or p["zip"] in text)
    if field == "items":
        words = [w for w in re.findall(r"[A-Z0-9]{3,}", value.upper())][:3]
        return bool(words) and all(w in text for w in words)
    return str(value).upper() in text


def teach(config, order_id, changed, db_path=None):
    """Turn an order fixed by hand into training examples: each of its emails, re-read by the model, with the
    fixed values put in wherever that email shows them. Runs in the background."""
    from .extract import email_prompt, extract_raw
    from .inbox import open_email
    from .llm import from_config
    from .orderstore import Store
    from .training import few_shot
    store = Store(db.connect(db_path))
    o = store.order(order_id)
    if not o or not config.accounts:
        return 0
    llm, examples, taught = from_config(config), few_shot(store), 0
    ids = (o.get("message_ids") or [])[:6]
    accounts = dict(store.db.execute("SELECT message_id, account FROM emails WHERE message_id IN (%s)"
                                     % ",".join("?" * len(ids)), ids).fetchall()) if ids else {}
    label = o.get("shipping_status") if o.get("shipping_status") in ("ordered", "in_transit", "out_for_delivery",
                                                                       "delivered", "cancelled", "returned") else "ordered"
    for mid in ids:
        try:
            email = open_email(config, accounts.get(mid) or o.get("account"), mid)
            if not email:
                continue
            raw, _ = extract_raw(llm, email, config.max_body_chars, examples)
            corrected = dict(raw, kind="order")
            body = f"{email.subject}\n{email.body}"
            for field, value in changed.items():
                if field == "items":
                    if _shows(body, "items", value) and raw.get("items"):
                        corrected["items"] = [dict(raw["items"][0], name=value)] + list(raw["items"][1:])
                elif field == "retailer" or _shows(body, field, value):
                    corrected[field] = value
            if corrected != dict(raw, kind="order") or raw.get("kind") != "order":
                store.add_inbox_example(email, email_prompt(email, config.max_body_chars), raw, corrected, label)
                taught += 1
        except Exception:
            logger.exception("teaching from order %s email failed", order_id)
    logger.info("order %s fixed by hand: %d email(s) became training examples", order_id, taught)
    return taught


@bp.post("/orders/<int:order_id>/edit")
def edit(order_id):
    """Change anything about an order: its details, profile, status, or what its emails are (an order, not an
    order, a botting expense or a sale). Every change teaches the AI from the order's emails."""
    from . import inbox, profilematch
    from .config import Config
    from .llm import from_config
    from .orderstore import Store
    store = Store(_con())
    o = store.order(order_id)
    if o is None:
        flash("That order is gone.")
        return redirect(_back())
    f = request.form
    config = Config.from_env()
    kind = f.get("email_type", "order")
    if kind in ("not_an_order", "expense", "sale"):
        ids = o.get("message_ids") or []
        mails = [dict(r) for r in store.db.execute(
            "SELECT message_id, account, sender, subject, date FROM emails WHERE message_id IN (%s)"
            % ",".join("?" * len(ids)), ids)] if ids else []
        if kind == "not_an_order":
            store.dismiss_order(order_id)
        else:
            store.delete_order(order_id)
        label = {"not_an_order": "other", "expense": "expense", "sale": "sale"}[kind]
        if mails and config.accounts:  # the AI re-reads them as what you said they are, and learns from it
            threading.Thread(target=inbox.label_emails, args=(config, Store(_con()), from_config(config), mails, label),
                             daemon=True).start()
        flash({"not_an_order": "Removed: not an order. ", "expense": "Moved to Expenses. ", "sale": "Moved to Sales. "}[kind]
              + ("The AI re-reads its emails and learns from it." if mails else ""))
        return redirect(_back())
    fields = {}
    for k in EDITABLE:
        if k not in f:
            continue
        v = (f.get(k) or "").strip()
        v = _money(v) if k == "total" else (v or None)
        if v != o.get(k):
            fields[k] = v
    name = (f.get("item_name") or "").strip()
    first = (o["items"] or [{}])[0] if o["items"] else {}
    if name and name != (first or {}).get("name"):
        items = o["items"] or [{}]
        items[0] = dict(items[0] if isinstance(items[0], dict) else {}, name=name)
        fields["items"] = items
    store.update_order(order_id, dict(fields))
    if "profile" in f and (f.get("profile") or "") != (o.get("profile_name") or ""):
        profilematch.set_profile(store.db, order_id, f.get("profile") or "")
    status = f.get("status") or ""
    if status and status != (o.get("shipping_status") or ""):
        current_app.relabel(store, o, status)
    taught = {k: (name if k == "items" else v) for k, v in fields.items()
              if k in ("retailer", "order_number", "total", "shipping_address", "tracking_number", "carrier", "items")}
    if taught and config.accounts:
        threading.Thread(target=teach, args=(config, order_id, taught, current_app.config.get("DB_PATH")),
                         daemon=True).start()
    flash("Saved." + (" The AI re-reads this order's emails and learns from your fixes." if taught else ""))
    return redirect(_back())
