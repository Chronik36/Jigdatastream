"""Go through: step through the orders, receipts and checkouts behind a number, one at a time, with everything tied
to each (checkouts, card charges, profile, address, email) and the pickers that correct it and teach the AI.

Opened from the Spend breakdown (a store's orders, a vendor's receipts, the biggest orders) and from Profile orders
(a profile's orders and checkouts). Tick some in the list to go through only those. "Looks right" marks an item
checked so the list shows what's left; a final status (delivered, cancelled) confirmed this way also becomes a
training example, like a correction does.
"""
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from . import db, drop, lookup, profilematch
from .extract import EXPENSE_CATEGORIES
from .orderstore import NOT_SPENT, Store

bp = Blueprint("review", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_marks (
    kind TEXT, item_id INTEGER, verdict TEXT, at TEXT, PRIMARY KEY (kind, item_id)
);
"""
KINDS = {"o": "order", "e": "expense", "c": "checkout"}
# Confirming one of these by hand is safe to learn from: it won't move on by itself later.
FINAL = ("delivered", "cancelled", "returned", "refunded")
ORDER_DAY = "substr(COALESCE(order_date, last_email_date), 1, 4)"


def ensure(con):
    return db.setup_once(con, "review", lambda c: c.executescript(SCHEMA))


def _con():
    con = ensure(db.connect(current_app.config.get("DB_PATH")))
    Store(con)  # makes sure the order columns exist
    return con


def source(con, src, name="", year=""):
    """The items behind one number, as (title, ["o12", "e5", "c3", ...])."""
    kept = f"COALESCE(shipping_status, '') NOT IN ({','.join('?' * len(NOT_SPENT))})"
    if src == "store":
        ids = [f"o{r[0]}" for r in con.execute(
            f"SELECT id FROM tracked_orders WHERE lower(COALESCE(retailer, '?')) = lower(?) AND {ORDER_DAY} = ? AND {kept}"
            " ORDER BY COALESCE(order_date, last_email_date) DESC", (name, str(year), *NOT_SPENT))]
        return f"{name} orders in {year}", ids
    if src == "vendor":
        ids = [f"e{r[0]}" for r in con.execute(
            "SELECT id FROM expenses WHERE lower(vendor) = lower(?) AND substr(date, 1, 4) = ? ORDER BY date DESC",
            (name, str(year)))]
        return f"{name} receipts in {year}", ids
    if src == "biggest":
        ids = [f"o{r[0]}" for r in con.execute(
            f"SELECT id FROM tracked_orders WHERE {ORDER_DAY} = ? AND {kept} AND total IS NOT NULL ORDER BY total DESC LIMIT 30",
            (str(year), *NOT_SPENT))]
        return f"Biggest orders in {year}", ids
    if src == "order" and name.isdigit():
        o = con.execute("SELECT retailer, order_number FROM tracked_orders WHERE id = ?", (int(name),)).fetchone()
        if not o:
            abort(404)
        tied = [f"c{c['id']}" for c in drop.rows(con) if c.get("order") and c["order"]["id"] == int(name)]
        return f"{o['retailer'] or 'Order'} {o['order_number'] or ''} and what it's tied to", [f"o{name}", *tied]
    if src == "day":  # a drop day: its orders, then every checkout that day or tied to one of them
        from .dropdays import day_of, label
        from .orderstore import Store
        orders = [o["id"] for o in Store(con).orders() if day_of(o) == name]
        mine = set(orders)
        tied = [f"c{c['id']}" for c in drop.rows(con)
                if c["day"] == name or (c.get("order") and c["order"]["id"] in mine)]
        return f"Drop of {label(name)}: orders and checkouts", [f"o{i}" for i in orders] + tied
    if src == "proxy":
        ids = [f"c{c['id']}" for c in drop.rows(con) if c["proxy"] == name]
        tied = {c["order"]["id"] for c in drop.rows(con) if c["proxy"] == name and c.get("order")}
        return f"Checkouts through {name}", ids + [f"o{i}" for i in sorted(tied)]
    if src == "profile":
        profilematch.match_orders(con)
        ids = [f"o{r[0]}" for r in con.execute(
            "SELECT id FROM tracked_orders WHERE profile_name = ? ORDER BY COALESCE(order_date, last_email_date) DESC", (name,))]
        ids += [f"c{c['id']}" for c in lookup.profile_checkouts(con, name)]
        return f"{name}: orders and checkouts", ids
    abort(404)


def marks(con):
    return {f"{k[0]}{i}": v for k, i, v in con.execute("SELECT kind, item_id, verdict FROM review_marks")}


def mark(con, key, verdict="ok"):
    kind, item = KINDS[key[0]], int(key[1:])
    con.execute("INSERT OR REPLACE INTO review_marks (kind, item_id, verdict, at) VALUES (?,?,?,?)",
                (kind, item, verdict, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    con.commit()


def charges(con, kind, item_id):
    return [dict(r) for r in con.execute(
        "SELECT t.*, a.name AS account_name, a.mask FROM bank_transactions t LEFT JOIN bank_accounts a ON a.id = t.account_id"
        " WHERE t.match_kind = ? AND t.match_id = ? ORDER BY t.date", (kind, item_id))]


def _num(v):
    """A webhook's price as a number ("$49.99" -> 49.99), or None."""
    try:
        return float(str(v).replace("$", "").replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


def summaries(con, keys, checkouts):
    """A one-line label for each item in the list."""
    out = {}
    orders = [k for k in keys if k[0] == "o"]
    expenses = [k for k in keys if k[0] == "e"]
    for chunk in (orders[i:i + 500] for i in range(0, len(orders), 500)):
        for r in con.execute(f"SELECT id, retailer, order_number, order_date, total, shipping_status FROM tracked_orders"
                             f" WHERE id IN ({','.join('?' * len(chunk))})", [int(k[1:]) for k in chunk]):
            out[f"o{r['id']}"] = {"kind": "Order", "title": r["retailer"] or "Order", "sub": r["order_number"] or "",
                                  "date": r["order_date"] or "", "amount": r["total"], "status": r["shipping_status"] or "unknown"}
    for chunk in (expenses[i:i + 500] for i in range(0, len(expenses), 500)):
        for r in con.execute(f"SELECT id, vendor, description, date, amount, category FROM expenses"
                             f" WHERE id IN ({','.join('?' * len(chunk))})", [int(k[1:]) for k in chunk]):
            out[f"e{r['id']}"] = {"kind": "Receipt", "title": r["vendor"] or "Receipt", "sub": r["description"] or "",
                                  "date": r["date"] or "", "amount": r["amount"], "status": r["category"] or "other"}
    for c in checkouts.values():
        out[f"c{c['id']}"] = {"kind": "Checkout", "title": c["site"] or "Checkout", "sub": c["product"] or "",
                              "date": c["day"] or "", "amount": _num(c.get("price")), "status": c["outcome"]}
    return out


def detail(con, key, checkouts):
    """Everything about one item and what it's tied to."""
    kind, item_id = key[0], int(key[1:])
    if kind == "o":
        o = Store(con).order(item_id)
        if not o:
            return None
        tied = [c for c in checkouts.values() if c.get("order") and c["order"]["id"] == item_id]
        mails = [dict(r) for r in con.execute(
            f"SELECT sender, subject, date, account FROM emails WHERE message_id IN ({','.join('?' * len(o['message_ids']))})"
            " ORDER BY date", o["message_ids"])] if o["message_ids"] else []
        return {"kind": "order", "o": o, "checkouts": tied, "charges": charges(con, "order", item_id), "emails": mails}
    if kind == "e":
        e = Store(con).expense(item_id)
        if not e:
            return None
        mail = con.execute("SELECT sender, subject, date, account FROM emails WHERE message_id = ?", (e["message_id"],)).fetchone()
        return {"kind": "expense", "e": e, "charges": charges(con, "expense", item_id), "emails": [dict(mail)] if mail else []}
    if kind == "c":
        c = checkouts.get(item_id)
        if not c:
            return None
        return {"kind": "checkout", "c": c, "charges": charges(con, "checkout", item_id)}
    return None


@bp.get("/review")
def page():
    con = _con()
    a = request.args
    src, name, year = a.get("src", ""), a.get("name", ""), a.get("year", "")
    title, keys = source(con, src, name, year)
    picked = [k for k in (a.get("ids") or "").split(",") if k and k[0] in KINDS and k[1:].isdigit()]
    if picked:
        keys = [k for k in keys if k in set(picked)] or picked
    checkouts = {c["id"]: c for c in drop.rows(con)} if any(k[0] == "c" for k in keys) or any(k[0] == "o" for k in keys) else {}
    done = marks(con)
    at = a.get("at") if a.get("at") in keys else (next((k for k in keys if k not in done), keys[0]) if keys else None)
    i = keys.index(at) if at else 0
    base = {"src": src, "name": name, "year": year, **({"ids": ",".join(picked)} if picked else {})}
    return render_template("review.html", title=title, keys=keys, labels=summaries(con, keys, checkouts), done=done,
                           at=at, i=i, d=detail(con, at, checkouts) if at else None, base=base, picked=picked,
                           prev_key=keys[i - 1] if at and i > 0 else None, next_key=keys[i + 1] if at and i + 1 < len(keys) else None,
                           next_open=next((k for k in keys[i + 1:] + keys[:i] if k not in done), None) if at else None,
                           categories=EXPENSE_CATEGORIES,
                           statuses=["ordered", "in_transit", "out_for_delivery", "delivered", "cancelled", "returned"],
                           moves=["expense", "sale"],
                           profile_names=[r[0] for r in con.execute("SELECT name FROM profiles ORDER BY name")])


@bp.post("/review/mark")
def mark_item():
    con = _con()
    key, back = request.form.get("key", ""), request.form.get("back", "")
    if not (key[:1] in KINDS and key[1:].isdigit()):
        abort(400)
    if request.form.get("undo"):
        con.execute("DELETE FROM review_marks WHERE kind = ? AND item_id = ?", (KINDS[key[0]], int(key[1:])))
        con.commit()
    else:
        mark(con, key)
        if key[0] == "o":
            o = Store(con).order(int(key[1:]))
            if o and (o["shipping_status"] or "") in FINAL and not o["status_manual"]:
                current_app.relabel(Store(con), o, o["shipping_status"])  # confirmed by hand: learn from its emails
    nxt = request.form.get("next", "")
    return redirect(back + (f"&at={nxt}" if nxt else "") if back.startswith("/review?") else url_for("review.page"))


@bp.post("/review/expense/<int:expense_id>/category")
def expense_category(expense_id):
    con = _con()
    cat = request.form.get("category", "")
    if cat in EXPENSE_CATEGORIES:
        con.execute("UPDATE expenses SET category = ? WHERE id = ?", (cat, expense_id))
        con.commit()
        mark(con, f"e{expense_id}", "fixed")
        flash(f"Saved as {cat.replace('_', ' ')}.")
    back = request.form.get("back", "")
    return redirect(back if back.startswith("/") else url_for("review.page"))
