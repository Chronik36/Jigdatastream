"""Sales and the profit goal: what you sold, what each flip made, and whether the month covers your expenses.

Sales come from sale and payout emails (StockX, GOAT, eBay, Tradepost, Mercari, Vinted, Alias, and any
platform you add or teach on the Inbox), from payout deposits on your bank statements, or typed in.
Each sale is linked to the order you bought the item with, so its profit is the payout minus what you
paid; with no order you can type the cost (MSRP, or what you paid in store).

A month's profit is the profit on items sold that month minus that month's botting expenses. Break-even
is when flips cover the expenses; the goal is break-even plus the profit you want each month.
"""
import calendar
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from . import budget, ledger
from .extract import SELL_PLATFORMS

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY,
    message_id TEXT UNIQUE,
    platform TEXT, item TEXT, size TEXT,
    sale_price REAL, fees REAL, payout REAL, currency TEXT,
    date TEXT, order_number TEXT, account TEXT,
    order_id INTEGER, cost REAL, link_manual INTEGER DEFAULT 0,
    counted INTEGER DEFAULT 1, source TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sell_platforms (name TEXT PRIMARY KEY, created_at TEXT);
CREATE TABLE IF NOT EXISTS profit_settings (key TEXT PRIMARY KEY, value TEXT);
"""
# Words too common in product names to tie a sale to an order on their own.
COMMON = {"the", "and", "for", "with", "size", "mens", "womens", "men", "women", "kids", "new", "box", "pack",
          "edition", "color", "black", "white", "red", "blue", "grey", "gray", "low", "high", "mid", "retro", "og"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure(con):
    budget.ensure(con)
    con.executescript(SCHEMA)
    con.commit()
    return con


# ---------- platforms and settings ----------

def custom_platforms(con):
    ensure(con)
    return [r[0] for r in con.execute("SELECT name FROM sell_platforms ORDER BY name COLLATE NOCASE")]


def platforms(con):
    return list(dict.fromkeys([*SELL_PLATFORMS, *custom_platforms(con)]))


def add_platform(con, name):
    name = re.sub(r"\s+", " ", (name or "").strip())[:40]
    if name and name.lower() not in {p.lower() for p in platforms(con)}:
        con.execute("INSERT OR IGNORE INTO sell_platforms VALUES (?, ?)", (name, _now()))
        con.commit()
    return name


def remove_platform(con, name):
    ensure(con)
    con.execute("DELETE FROM sell_platforms WHERE name = ?", (name,))
    con.commit()


def with_terms(query, words):
    """Add words to a Gmail search like '(a OR b) -in:spam' so the sync also finds their emails."""
    extra = [f'"{w}"' if " " in w else w for w in words if w and w.lower() not in query.lower()]
    if not extra:
        return query
    i = query.rfind(")")
    if query.startswith("(") and i > 0:
        return f"{query[:i]} OR {' OR '.join(extra)}{query[i:]}"
    return f"{query} OR {' OR '.join(extra)}"


def setting(con, key, default=None):
    ensure(con)
    r = con.execute("SELECT value FROM profit_settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(con, key, value):
    ensure(con)
    con.execute("INSERT OR REPLACE INTO profit_settings VALUES (?, ?)", (key, value))
    con.commit()


def goal(con):
    try:
        return float(setting(con, "monthly_goal") or 0)
    except ValueError:
        return 0.0


# ---------- saving ----------

def _key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def save_email(con, email, result):
    """A sale or payout email. A second email about the same sale (sold, then paid out) fills in the first."""
    ensure(con)
    existing = None
    if result.get("order_number"):
        existing = next((r for r in con.execute("SELECT * FROM sales WHERE order_number IS NOT NULL")
                         if _key(r["order_number"]) == _key(result["order_number"])
                         and _key(r["platform"]) == _key(result["platform"])), None)
    if existing:
        fields = {k: result[k] for k in ("item", "size", "sale_price", "fees", "payout")
                  if result.get(k) is not None and existing[k] is None}
        if fields:
            con.execute(f"UPDATE sales SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        (*fields.values(), existing["id"]))
        con.commit()
        return existing["id"]
    # A payout with no item is usually a lump sum of sales already counted one by one.
    counted = 1
    if not result.get("item") and con.execute(
            "SELECT 1 FROM sales WHERE item IS NOT NULL AND lower(platform) = lower(?)", (result["platform"],)).fetchone():
        counted = 0
    cur = con.execute(
        "INSERT OR REPLACE INTO sales (message_id, platform, item, size, sale_price, fees, payout, currency, date,"
        " order_number, account, counted, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'email', ?)",
        (email.message_id, result["platform"], result.get("item"), result.get("size"), result.get("sale_price"),
         result.get("fees"), result.get("payout"), result.get("currency") or "USD", result.get("date"),
         result.get("order_number"), email.account, counted, _now()))
    con.commit()
    link_orders(con)
    return cur.lastrowid


def add(con, fields, source="manual"):
    ensure(con)
    cols = ["platform", "item", "size", "sale_price", "fees", "payout", "currency", "date", "order_number", "cost",
            "order_id"]
    vals = {k: fields.get(k) for k in cols}
    vals["currency"] = vals["currency"] or "USD"
    cur = con.execute(
        f"INSERT INTO sales (message_id, account, source, link_manual, created_at, {', '.join(cols)})"
        f" VALUES (?,?,?,?,?,{','.join('?' * len(cols))})",
        (f"{source}:{uuid.uuid4().hex}", {"manual": "added by hand", "in_person": "in person"}.get(source, "bank statement"), source,
         1 if fields.get("order_id") else 0, _now(), *vals.values()))
    con.commit()
    link_orders(con)
    return cur.lastrowid


def update(con, sale_id, fields):
    ensure(con)
    allowed = {k: v for k, v in fields.items() if k in ("platform", "item", "size", "sale_price", "fees", "payout",
                                                        "date", "cost", "order_id", "counted")}
    if "order_id" in allowed:
        allowed["link_manual"] = 1
    if allowed:
        con.execute(f"UPDATE sales SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?",
                    (*allowed.values(), sale_id))
        con.commit()


def delete(con, sale_id):
    ensure(con)
    con.execute("DELETE FROM sales WHERE id = ?", (sale_id,))
    con.commit()


def add_deposit(con, tx_id, platform=None):
    """A payout deposit on a bank statement becomes a sale (the money in), and the deposit is marked used."""
    ensure(con)
    tx = con.execute("SELECT * FROM bank_transactions WHERE id = ?", (tx_id,)).fetchone()
    if not tx or tx["amount"] >= 0:
        return None
    sid = add(con, {"platform": platform or platform_in(con, tx["description"]) or tx["description"],
                    "payout": round(-tx["amount"], 2), "date": tx["date"], "currency": tx["currency"]}, "bank")
    con.execute("UPDATE bank_transactions SET match_kind = 'sale', match_id = ?, match_how = 'manual' WHERE id = ?",
                (sid, tx_id))
    con.commit()
    return sid


def platform_in(con, text):
    squashed = _key(text)
    return next((p for p in platforms(con) if len(_key(p)) >= 4 and _key(p) in squashed), None)


def match_deposits(con):
    """A payout deposit on your statement that is a sale already counted from its payout email (same platform,
    same amount, landing from a day before the email to ten days after) is tied to that sale, so it's never
    offered as a new sale. Returns how many were tied."""
    ensure(con)
    taken = {r[0] for r in con.execute("SELECT match_id FROM bank_transactions WHERE match_kind = 'sale'")}
    sales = [dict(r) for r in con.execute("SELECT * FROM sales WHERE date IS NOT NULL AND counted = 1")]
    tied = 0
    for t in con.execute("SELECT * FROM bank_transactions WHERE amount < 0 AND match_kind IS NULL"
                         " AND COALESCE(match_how, '') != 'ignored' ORDER BY date").fetchall():
        p = platform_in(con, t["description"])
        try:
            day = date.fromisoformat(t["date"][:10])
        except (TypeError, ValueError):
            continue
        if not p:
            continue
        for sale in sales:
            if sale["id"] in taken or _key(sale["platform"] or "") != _key(p) or \
                    round(money_in(sale), 2) != round(-t["amount"], 2):
                continue
            try:
                gap = (day - date.fromisoformat(sale["date"][:10])).days
            except ValueError:
                continue
            if -1 <= gap <= 10:
                con.execute("UPDATE bank_transactions SET match_kind = 'sale', match_id = ?, match_how = 'auto'"
                            " WHERE id = ?", (sale["id"], t["id"]))
                taken.add(sale["id"])
                tied += 1
                break
    con.commit()
    return tied


def year_income(con, year):
    """Money in from sales in one year (payouts, else price minus fees)."""
    ensure(con)
    return sum(money_in(dict(r)) for r in con.execute(
        "SELECT payout, sale_price, fees FROM sales WHERE counted = 1 AND substr(date, 1, 4) = ?", (str(year),)))


def deposits(con, limit=20):
    """Money in on your statements from a selling platform, not yet counted as a sale."""
    ensure(con)
    match_deposits(con)
    out = []
    for t in con.execute("SELECT t.*, a.name AS account_name FROM bank_transactions t LEFT JOIN bank_accounts a"
                         " ON a.id = t.account_id WHERE t.amount < 0 AND t.match_kind IS NULL"
                         " AND COALESCE(t.match_how, '') != 'ignored' ORDER BY t.date DESC"):
        p = platform_in(con, t["description"])
        if p:
            out.append(dict(t, platform=p))
        if len(out) >= limit:
            break
    return out


# ---------- linking sales to the orders that bought them ----------

def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in COMMON}


def _item_cost(order, words):
    """(how well the order's best item matches, that item's price or None)."""
    items = json.loads(order["items"] or "[]")
    best = (0, None)
    for it in items:
        common = words & _words(it.get("name"))
        score = len(common) / max(1, min(len(words), len(_words(it.get("name")))))
        if len(common) >= 2 and score > best[0]:
            price = it.get("price")
            if price is None and len(items) == 1:
                price = order["total"]
            best = (score, price)
    return best


def link_orders(con):
    """Tie each sale (not linked by hand) to the most recent earlier order with a matching item that no other
    sale has claimed, and take its cost from the order."""
    claimed = {r[0] for r in con.execute("SELECT order_id FROM sales WHERE order_id IS NOT NULL AND link_manual = 1")}
    orders = [dict(r) for r in con.execute(
        "SELECT id, retailer, order_number, items, total, COALESCE(order_date, substr(last_email_date, 1, 10)) AS day"
        " FROM tracked_orders WHERE COALESCE(shipping_status, '') NOT IN ('cancelled', 'returned', 'refunded',"
        " 'not_an_order') ORDER BY day DESC")]
    for s in con.execute("SELECT * FROM sales WHERE link_manual = 0 AND item IS NOT NULL ORDER BY date"):
        words = _words(s["item"])
        if len(words) < 2:
            continue
        pick = None
        for o in orders:
            if o["id"] in claimed or (s["date"] and o["day"] and o["day"] > s["date"]):
                continue
            score, price = _item_cost(o, words)
            if score >= .6:
                pick = (o, price)
                break
        if pick:
            claimed.add(pick[0]["id"])
            con.execute("UPDATE sales SET order_id = ?, cost = COALESCE(cost, ?) WHERE id = ?",
                        (pick[0]["id"], pick[1], s["id"]))
    con.commit()


# ---------- reading back ----------

def money_in(s):
    """What a sale brings in: the payout, else the price minus fees, else the price."""
    if s["payout"] is not None:
        return s["payout"]
    if s["sale_price"] is not None:
        return s["sale_price"] - (s["fees"] or 0)
    return 0


def rows(con, month=None):
    ensure(con)
    q = ("SELECT s.*, o.retailer AS order_retailer, o.order_number AS order_ref, o.total AS order_total"
         " FROM sales s LEFT JOIN tracked_orders o ON o.id = s.order_id")
    out = []
    for r in con.execute(q + (" WHERE substr(s.date, 1, 7) = ?" if month else "") + " ORDER BY s.date DESC, s.id DESC",
                         (month,) if month else ()):
        d = dict(r)
        d["money_in"] = money_in(d)
        d["profit"] = round(d["money_in"] - d["cost"], 2) if d["cost"] is not None and d["item"] else None
        out.append(d)
    return out


def month_numbers(con, month, today=None):
    """Sales, flip profit, expenses (with renewals still due this month) and the goal for one month."""
    ensure(con)
    today = today or date.today()
    sold = [s for s in rows(con, month) if s["counted"]]
    view = budget.month_view(con, month, today)
    income = sum(s["money_in"] for s in sold)
    costs = sum(s["cost"] or 0 for s in sold)
    flips = income - costs
    expenses, due = view["spent"], view["to_renew"]
    target = goal(con)
    priced = [s for s in sold if s["profit"] is not None]
    recent = [s for s in rows(con) if s["counted"] and s["profit"] is not None
              and s["date"] and s["date"] >= (today - timedelta(days=90)).isoformat()]
    avg = sum(s["profit"] for s in recent) / len(recent) if recent else None
    need = max(0.0, expenses + due + target - flips)
    bought = con.execute("SELECT SUM(total) FROM tracked_orders WHERE COALESCE(shipping_status, '') NOT IN"
                         " ('cancelled', 'not_an_order') AND substr(COALESCE(order_date, last_email_date), 1, 7) = ?",
                         (month,)).fetchone()[0] or 0
    return {"month": month, "name": budget.month_name(month), "current": view["current"], "sales": len(sold),
            "income": income, "costs": costs, "flips": flips, "expenses": expenses, "due": due,
            "break_even": expenses + due, "goal": target, "target": expenses + due + target,
            "net": flips - expenses, "need": need, "avg": avg, "more": (int(-(-need // avg)) if avg and avg > 0
                                                                      and need else None),
            "unpriced": len(sold) - len(priced), "bought": bought}


def history(con, n=12, today=None, width=480, height=220):
    months = ledger.last_months(n, today)
    vals = []
    for m in months:
        x = month_numbers(con, m, today)
        vals.append({"month": m, "label": ledger.MONTH_NAMES[int(m[5:]) - 1], "name": x["name"], "net": x["net"],
                     "flips": x["flips"], "expenses": x["expenses"]})
    top, bottom_v = max([v["net"] for v in vals] + [0]), min([v["net"] for v in vals] + [0])
    step = ledger.nice_max((top - bottom_v) / 4 or 100)
    hi, lo = -(-top // step) * step, (bottom_v // step) * step
    if hi == lo:
        hi = lo + step
    left, right, pad_top, bottom = 52, 8, 10, 26
    plot_h, slot = height - pad_top - bottom, (width - left - right) / n
    y = lambda v: round(pad_top + (hi - v) / (hi - lo) * plot_h, 1)
    for i, v in enumerate(vals):
        v.update(x=round(left + i * slot + slot * .22, 1), w=round(slot * .56, 1), y=min(y(v["net"]), y(0)),
                 h=round(abs(y(v["net"]) - y(0)), 1), color="#3ddc84" if v["net"] >= 0 else "#ff6b7a")
    grid = [{"v": lo + k * step, "y": y(lo + k * step)} for k in range(int(round((hi - lo) / step)) + 1)]
    return {"rows": vals, "grid": grid, "zero": y(0), "width": width, "height": height, "left": left,
            "right": right}


def unsold(con, limit=50):
    """Orders not cancelled and not tied to a sale: inventory still to flip, newest first."""
    ensure(con)
    out = [dict(r) for r in con.execute(
        "SELECT id, retailer, order_number, items, total, shipping_status,"
        " COALESCE(order_date, substr(last_email_date, 1, 10)) AS day FROM tracked_orders"
        " WHERE COALESCE(shipping_status, '') NOT IN ('cancelled', 'returned', 'refunded', 'not_an_order')"
        " AND id NOT IN (SELECT order_id FROM sales WHERE order_id IS NOT NULL) ORDER BY day DESC LIMIT ?",
        (limit,))]
    for o in out:
        names = [i.get("name") for i in json.loads(o["items"] or "[]") if i.get("name")]
        o["names"], o["first"] = ", ".join(names)[:60], names[0] if names else None
    return out


def order_choices(con, limit=300, only=None):
    """Orders to pick as "bought with" for a sale, newest first; only= one order by id."""
    ensure(con)
    out = []
    where, args = ("id = ?", (only,)) if only else (
        "COALESCE(shipping_status, '') NOT IN ('cancelled', 'not_an_order')", ())
    for r in con.execute("SELECT id, retailer, order_number, items, total,"
                         " COALESCE(order_date, substr(last_email_date, 1, 10)) AS day FROM tracked_orders"
                         f" WHERE {where} ORDER BY day DESC LIMIT ?", (*args, limit)):
        items = [i.get("name") for i in json.loads(r["items"] or "[]") if i.get("name")]
        names = ", ".join(items)[:70]
        out.append({"id": r["id"], "first": items[0] if items else None, "label": f"{r['day'] or ''} · {r['retailer'] or ''} {r['order_number'] or ''}"
                                            f" · {names or 'items unknown'} · ${r['total'] or 0:,.2f}"})
    return out


def days_in(month):
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
