"""The Expenses ledger: card charges, email receipts and orders side by side.

Each botting receipt and each card charge lands in one of a few states: matched to each other, a charge
with no receipt, a receipt with no charge, a close amount (a charge a few dollars off a receipt, from
currency fees or tax added at checkout), or a receipt you paid another way (PayPal balance, crypto, a
card you never imported). A receipt only counts as missing its charge when your imported statements
cover its date, so receipts from before your first import are never flagged.
"""
from collections import defaultdict
from datetime import date, timedelta

from . import bank
from .orderstore import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_rejects (
    tx_id INTEGER, kind TEXT, target_id INTEGER,
    PRIMARY KEY (tx_id, kind, target_id)
);
"""
MIGRATIONS = [("expenses", "paid_with", "TEXT")]

PAY_METHODS = ["PayPal balance", "Crypto", "Cash", "Gift card", "Card not imported", "Other"]
STATES = {
    "matched": "Matched",
    "charge_only": "Charge, no receipt",
    "receipt_only": "Receipt, no charge",
    "close": "Close amount",
    "other_way": "Paid another way",
    "no_statement": "No statement yet",
}
# Shown as filters on the Ledger tab, in this order. "No statement yet" only appears under Everything.
FILTERS = ["all", "matched", "charge_only", "receipt_only", "close", "other_way"]
NEEDS_LOOK = ("charge_only", "receipt_only", "close")

# A close amount: within this many dollars, or this share of the amount, whichever is bigger.
CLOSE_DOLLARS, CLOSE_SHARE = 3.0, 0.06

CATEGORY_COLORS = {"proxy": "#3dd8ef", "bot": "#3ddc84", "cook_group": "#9b8cff", "captcha": "#ff8a3d",
                   "server": "#2f86f6", "aco_service": "#f5c84c", "account": "#f06fc2", "other": "#8592bd"}


def ensure(con):
    Store(con)
    bank.ensure(con)
    con.executescript(SCHEMA)
    for table, column, kind in MIGRATIONS:
        if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    con.commit()
    return con


def coverage(con):
    """(first, last) dates your imported statements cover, or (None, None) before any import."""
    r = con.execute("SELECT MIN(date), MAX(date) FROM bank_transactions").fetchone()
    return r[0], r[1]


def is_close(a, b):
    diff = abs(round(a, 2) - round(b, 2))
    return 0.005 < diff <= max(CLOSE_DOLLARS, CLOSE_SHARE * max(a, b))


def close_matches(con):
    """{tx_id: (kind, target row)} for unmatched charges that are a few dollars off an unmatched receipt or
    order from the same store near the same date. Each receipt or order is offered to one charge only."""
    claimed = {(r[0], r[1]) for r in con.execute(
        "SELECT match_kind, match_id FROM bank_transactions WHERE match_kind IS NOT NULL")}
    rejected = {tuple(r) for r in con.execute("SELECT tx_id, kind, target_id FROM ledger_rejects")}
    other_way = {r[0] for r in con.execute("SELECT id FROM expenses WHERE paid_with IS NOT NULL")}
    targets = [("expense", dict(r)) for r in con.execute(
        "SELECT id, vendor AS name, description AS ref, date AS day, amount, category FROM expenses"
        " WHERE amount > 0 AND date IS NOT NULL")]
    targets += [("order", dict(r)) for r in con.execute(
        "SELECT id, retailer AS name, order_number AS ref, COALESCE(order_date, substr(last_email_date, 1, 10)) AS day,"
        " total AS amount FROM tracked_orders WHERE total > 0"
        " AND COALESCE(shipping_status, '') NOT IN ('cancelled', 'not_an_order')")]
    targets = [(k, t) for k, t in targets if (k, t["id"]) not in claimed
               and not (k == "expense" and t["id"] in other_way) and bank._day(t["day"])]
    found = []
    for tx in con.execute("SELECT * FROM bank_transactions WHERE match_kind IS NULL AND COALESCE(match_how, '')"
                          " != 'ignored' AND amount > 0 AND pending = 0"):
        when = bank._day(tx["date"])
        if not when:
            continue
        for kind, t in targets:
            days = (when - bank._day(t["day"])).days
            if (-bank.DAYS_BEFORE <= days <= bank.DAYS_AFTER and is_close(tx["amount"], t["amount"])
                    and (tx["id"], kind, t["id"]) not in rejected and bank.names_agree(tx["description"], t["name"])):
                found.append((abs(days), abs(tx["amount"] - t["amount"]), tx["id"], kind, t))
    out, used = {}, set()
    for _, _, tx_id, kind, t in sorted(found, key=lambda f: f[:2]):
        if tx_id not in out and (kind, t["id"]) not in used:
            out[tx_id] = (kind, t)
            used.add((kind, t["id"]))
    return out


def rows(con):
    """Every botting receipt and every card charge you haven't ignored, newest first, each with its state."""
    ensure(con)
    first, last = coverage(con)
    close = close_matches(con)
    close_targets = {(k, t["id"]): tx_id for tx_id, (k, t) in close.items()}
    charges = {r["id"]: dict(r) for r in con.execute(
        "SELECT t.*, a.name AS account_name FROM bank_transactions t LEFT JOIN bank_accounts a ON a.id = t.account_id"
        " WHERE t.amount > 0 AND COALESCE(t.match_how, '') != 'ignored'")}
    paid = {(t["match_kind"], t["match_id"]): t for t in charges.values() if t["match_kind"]}
    out = []
    for e in con.execute("SELECT * FROM expenses ORDER BY date DESC"):
        e = dict(e)
        row = {"kind": "expense", "date": e["date"], "name": e["vendor"], "detail": e["description"],
               "category": e["category"], "amount": e["amount"], "currency": e["currency"], "expense": e,
               "charge": paid.get(("expense", e["id"])), "inbox": e["account"]}
        if row["charge"]:
            row["state"] = "matched"
        elif e["paid_with"]:
            row["state"] = "other_way"
        elif ("expense", e["id"]) in close_targets:
            continue  # shown once, on its charge's row
        elif first and e["date"] and first <= e["date"] <= last:
            row["state"] = "receipt_only"
        else:
            row["state"] = "no_statement"
        out.append(row)
    orders = {o["id"]: dict(o) for o in con.execute(
        "SELECT id, retailer, order_number, shipping_status, total, currency FROM tracked_orders WHERE id IN"
        " (SELECT match_id FROM bank_transactions WHERE match_kind = 'order')"
        f" OR id IN ({','.join(str(t['id']) for k, t in close.values() if k == 'order') or 'NULL'})")}
    for t in charges.values():
        if t["match_kind"] == "expense":
            continue  # already on its receipt's row
        row = {"kind": "charge", "date": t["date"], "name": t["description"], "detail": None, "category": None,
               "amount": t["amount"], "currency": t["currency"], "charge": t, "inbox": None}
        if t["match_kind"] == "order":
            o = orders.get(t["match_id"]) or {}
            row.update(kind="order", name=o.get("retailer") or t["description"], order=o, state="matched",
                       detail=f"Order {o['order_number']}" if o.get("order_number") else "Order")
        elif t["id"] in close:
            kind, target = close[t["id"]]
            row.update(state="close", suggestion={"kind": kind, **target})
            if kind == "expense":
                e = dict(con.execute("SELECT * FROM expenses WHERE id = ?", (target["id"],)).fetchone())
                row.update(kind="expense", name=e["vendor"], detail=e["description"], category=e["category"],
                           amount=e["amount"], currency=e["currency"], expense=e, inbox=e["account"])
            else:
                o = orders.get(target["id"]) or {}
                row.update(kind="order", name=target["name"], order=o, amount=target["amount"],
                           detail=f"Order {target['ref']}" if target["ref"] else "Order")
        else:
            row["state"] = "charge_only"
        out.append(row)
    out.sort(key=lambda r: r["date"] or "", reverse=True)
    return out


def counts(items):
    c = defaultdict(int)
    for r in items:
        c[r["state"]] += 1
    c["all"] = len(items)
    return dict(c)


def confirm_close(con, tx_id, kind, target_id):
    bank.set_match(con, tx_id, kind, target_id)


def reject_close(con, tx_id, kind, target_id):
    ensure(con)
    con.execute("INSERT OR IGNORE INTO ledger_rejects VALUES (?,?,?)", (tx_id, kind, target_id))
    con.commit()


def set_paid_with(con, expense_id, method):
    ensure(con)
    con.execute("UPDATE expenses SET paid_with = ? WHERE id = ?", ((method or "").strip()[:60] or None, expense_id))
    con.commit()


# ---------- Overview numbers ----------

def month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def last_months(n, today=None):
    """['2025-11', ..., '2026-10']: the n months ending with this one."""
    today = today or date.today()
    y, m, out = today.year, today.month, []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def by_month(con, months):
    """{month: {category: amount}} for botting expenses in those months."""
    ensure(con)
    out = {m: defaultdict(float) for m in months}
    for r in con.execute("SELECT substr(date, 1, 7) AS m, category, SUM(amount) AS total FROM expenses"
                         " WHERE amount IS NOT NULL AND substr(date, 1, 7) BETWEEN ? AND ? GROUP BY m, category",
                         (months[0], months[-1])):
        if r["m"] in out:
            out[r["m"]][r["category"] or "other"] += r["total"] or 0
    return out


def nice_max(v):
    """A round top for a chart axis: 1, 2, 2.5 or 5 times a power of ten, at or above v."""
    if v <= 0:
        return 100
    step = 10 ** (len(str(int(v))) - 1)
    for k in (1, 2, 2.5, 5, 10):
        if k * step >= v:
            return k * step
    return 10 * step


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_chart(con, n=12, today=None, width=720, height=260):
    """Stacked bars of spend per category per month, laid out for an inline SVG."""
    months = last_months(n, today)
    data = by_month(con, months)
    top = nice_max(max((sum(v.values()) for v in data.values()), default=0))
    left, right, pad_top, bottom = 52, 8, 10, 26
    plot_w, plot_h = width - left - right, height - pad_top - bottom
    y = lambda v: pad_top + plot_h - v / top * plot_h
    slot = plot_w / n
    bars = []
    for i, m in enumerate(months):
        acc, segs = 0.0, []
        for cat, amount in sorted(data[m].items(), key=lambda kv: list(CATEGORY_COLORS).index(kv[0])
                                  if kv[0] in CATEGORY_COLORS else 99):
            if amount <= 0:
                continue
            segs.append({"cat": cat, "amount": amount, "y": round(y(acc + amount), 1),
                         "h": round(y(acc) - y(acc + amount), 1), "color": CATEGORY_COLORS.get(cat, "#8592bd")})
            acc += amount
        bars.append({"month": m, "label": MONTH_NAMES[int(m[5:]) - 1], "x": round(left + i * slot + slot * .2, 1),
                     "w": round(slot * .6, 1), "total": acc, "segs": segs})
    grid = [{"v": top * k / 4, "y": round(y(top * k / 4), 1)} for k in range(5)]
    cats = sorted({c for v in data.values() for c, a in v.items() if a > 0},
                  key=lambda c: list(CATEGORY_COLORS).index(c) if c in CATEGORY_COLORS else 99)
    return {"bars": bars, "grid": grid, "width": width, "height": height, "left": left, "right": right,
            "base": pad_top + plot_h, "legend": [(c, CATEGORY_COLORS.get(c, "#8592bd")) for c in cats]}


def overview(con, today=None):
    ensure(con)
    today = today or date.today()
    this_m = month_key(today)
    prev_m = month_key(today.replace(day=1) - timedelta(days=1))
    spent = {r["m"]: r["total"] or 0 for r in con.execute(
        "SELECT substr(date, 1, 7) AS m, COALESCE(SUM(amount), 0) AS total FROM expenses WHERE substr(date, 1, 7) IN (?, ?)"
        " GROUP BY m", (this_m, prev_m))}
    year_ago = (today - timedelta(days=365)).isoformat()
    last_year = con.execute("SELECT SUM(amount) FROM expenses WHERE date > ?", (year_ago,)).fetchone()[0] or 0
    cats = [dict(r) for r in con.execute(
        "SELECT category, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n FROM expenses WHERE substr(date, 1, 7) = ?"
        " GROUP BY category ORDER BY total DESC", (this_m,))]
    for c in cats:
        c["color"] = CATEGORY_COLORS.get(c["category"], "#8592bd")
    since = (today - timedelta(days=90)).isoformat()
    vendors = [dict(r) for r in con.execute(
        "SELECT vendor, category, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n FROM expenses WHERE date > ?"
        " GROUP BY lower(vendor) ORDER BY total DESC LIMIT 8", (since,))]
    return {"month": this_m, "month_name": MONTH_NAMES[today.month - 1], "this_month": spent.get(this_m, 0),
            "last_month": spent.get(prev_m, 0), "last_year": last_year, "categories": cats, "vendors": vendors,
            "day": today.day}
