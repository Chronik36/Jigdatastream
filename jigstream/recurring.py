"""Recurring botting expenses: proxy plans, bot renewals, cook group memberships, servers.

A subscription is a vendor you pay on a schedule: weekly, monthly, every 3 months, yearly, or every N days.
Receipts from that vendor at about the same price are linked to it as they arrive, which moves the next
renewal date forward. A renewal that never shows up, a price that changed, or a charge after you cancelled
is flagged. Vendors you pay on a schedule but haven't tracked yet are suggested from repeat receipts and
repeat card charges, with no vendor list.
"""
import calendar
import re
from datetime import date, datetime, timedelta, timezone
from statistics import median

from . import bank, ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY,
    vendor TEXT, vkey TEXT, category TEXT, amount REAL, currency TEXT,
    period TEXT, every_days INTEGER,
    next_due TEXT, last_paid TEXT, last_amount REAL, previous_amount REAL,
    status TEXT DEFAULT 'active', cancelled_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS recurring_dismissed (vkey TEXT PRIMARY KEY, created_at TEXT);
"""
MIGRATIONS = [("expenses", "subscription_id", "INTEGER")]

PERIODS = {"weekly": "Weekly", "monthly": "Monthly", "quarterly": "Every 3 months", "yearly": "Yearly",
           "custom": "Custom"}
# Per-month cost of one payment, for the monthly run rate.
PER_MONTH = {"weekly": 52 / 12, "monthly": 1, "quarterly": 1 / 3, "yearly": 1 / 12}
# Days a renewal may be late before it counts as missed (posting delays, a retry a few days later).
GRACE = {"weekly": 2, "monthly": 5, "quarterly": 7, "yearly": 10, "custom": 5}
# How far a receipt may be from the plan's price and still be the same plan (price changes, taxes).
SAME_PLAN = 0.35
# Gaps between payments that read as each standard schedule.
WINDOWS = [("weekly", 6, 8), ("monthly", 26, 35), ("quarterly", 84, 98), ("yearly", 350, 380)]
DUE_SOON_DAYS = 7


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure(con):
    ledger.ensure(con)
    con.executescript(SCHEMA)
    for table, column, kind in MIGRATIONS:
        if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    con.commit()
    return con


def vkey(name):
    """'Packet Proxies', 'PACKETPROXIES.COM 8885551234' -> 'packetproxies'."""
    s = bank._squash(re.sub(r"\d{3,}", "", name or ""))
    return re.sub(r"(com|io|net|gg|llc|inc)$", "", s) or s


def _day(s):
    return bank._day(s)


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def advance(d, period, every_days=None):
    if period == "weekly":
        return d + timedelta(days=7)
    if period == "monthly":
        return add_months(d, 1)
    if period == "quarterly":
        return add_months(d, 3)
    if period == "yearly":
        return add_months(d, 12)
    return d + timedelta(days=max(1, int(every_days or 30)))


def per_month(sub):
    amount = sub["last_amount"] if sub["last_amount"] is not None else sub["amount"]
    if sub["period"] == "custom":
        return (amount or 0) * 30.44 / max(1, sub["every_days"] or 30)
    return (amount or 0) * PER_MONTH.get(sub["period"], 1)


def label(sub):
    if sub["period"] == "custom":
        return f"Every {sub['every_days']} days"
    return PERIODS.get(sub["period"], sub["period"])


def same_plan(expected, amount):
    return expected is None or amount is None or abs(amount - expected) <= SAME_PLAN * max(expected, amount)


# ---------- tracking ----------

def track(con, vendor, period, every_days=None, category=None, amount=None, expense_id=None, tx_id=None,
          next_due=None):
    """Start tracking a subscription. Built from an expense row or a card charge, or typed in. Past
    receipts from the same vendor at about the same price are linked to it."""
    ensure(con)
    if period not in PERIODS:
        return None
    if period == "custom" and not every_days:
        every_days = 30
    if tx_id:  # a card charge with no receipt: keep it (and its earlier twins) as expenses first
        tx = con.execute("SELECT * FROM bank_transactions WHERE id = ?", (tx_id,)).fetchone()
        if tx:
            vendor = vendor or tx["description"]
            amount = amount if amount is not None else tx["amount"]
            for t in _charge_twins(con, tx):
                bank.add_as_expense(con, t["id"], vendor, category or "other")
    if expense_id:
        e = con.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if e:
            vendor = vendor or e["vendor"]
            category = category or e["category"]
            amount = amount if amount is not None else e["amount"]
    if not vendor:
        return None
    cur = con.execute(
        "INSERT INTO subscriptions (vendor, vkey, category, amount, currency, period, every_days, next_due, status,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,'active',?)",
        (vendor, vkey(vendor), category or "other", amount, "USD", period,
         every_days if period == "custom" else None, next_due, _now()))
    sid = cur.lastrowid
    if expense_id:
        con.execute("UPDATE expenses SET subscription_id = ? WHERE id = ?", (sid, expense_id))
    con.execute("DELETE FROM recurring_dismissed WHERE vkey = ?", (vkey(vendor),))
    con.commit()
    refresh(con, keep_next_due=bool(next_due))
    return sid


def _charge_twins(con, tx):
    """This charge and earlier unmatched charges with the same description and about the same amount."""
    key = vkey(tx["description"])
    return [t for t in con.execute(
        "SELECT * FROM bank_transactions WHERE match_kind IS NULL AND COALESCE(match_how, '') != 'ignored'"
        " AND amount > 0 ORDER BY date") if vkey(t["description"]) == key and same_plan(tx["amount"], t["amount"])]


def update(con, sub_id, fields):
    ensure(con)
    allowed = {k: v for k, v in fields.items() if k in ("vendor", "category", "amount", "period", "every_days",
                                                        "next_due")}
    if "vendor" in allowed:
        allowed["vkey"] = vkey(allowed["vendor"])
    if allowed:
        con.execute(f"UPDATE subscriptions SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?",
                    (*allowed.values(), sub_id))
        con.commit()
    refresh(con, keep_next_due="next_due" in allowed and allowed["next_due"])


def cancel(con, sub_id, today=None):
    ensure(con)
    con.execute("UPDATE subscriptions SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                ((today or date.today()).isoformat(), sub_id))
    con.commit()


def reactivate(con, sub_id):
    ensure(con)
    con.execute("UPDATE subscriptions SET status = 'active', cancelled_at = NULL WHERE id = ?", (sub_id,))
    con.commit()
    refresh(con)


def delete(con, sub_id):
    ensure(con)
    con.execute("UPDATE expenses SET subscription_id = NULL WHERE subscription_id = ?", (sub_id,))
    con.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    con.commit()


def dismiss(con, key):
    ensure(con)
    con.execute("INSERT OR REPLACE INTO recurring_dismissed VALUES (?, ?)", (key, _now()))
    con.commit()


def refresh(con, keep_next_due=False):
    """Link new receipts to their subscriptions and work out each one's last payment and next renewal.
    Runs after every sync and whenever the Recurring tab opens."""
    ensure(con)
    subs = [dict(r) for r in con.execute("SELECT * FROM subscriptions ORDER BY id")]
    loose = [dict(r) for r in con.execute(
        "SELECT id, vendor, amount, date FROM expenses WHERE subscription_id IS NULL AND date IS NOT NULL ORDER BY date")]
    for s in subs:
        for e in loose:
            if e.get("taken") or vkey(e["vendor"]) != s["vkey"] or not same_plan(s["amount"], e["amount"]):
                continue
            con.execute("UPDATE expenses SET subscription_id = ? WHERE id = ?", (s["id"], e["id"]))
            e["taken"] = True
        paid = con.execute("SELECT date, amount FROM expenses WHERE subscription_id = ? AND date IS NOT NULL"
                           " ORDER BY date DESC, id DESC LIMIT 2", (s["id"],)).fetchall()
        last = paid[0] if paid else None
        fields = {"last_paid": last["date"] if last else None, "last_amount": last["amount"] if last else None,
                  "previous_amount": paid[1]["amount"] if len(paid) > 1 else None}
        if last and _day(last["date"]):
            due = advance(_day(last["date"]), s["period"], s["every_days"]).isoformat()
            # A hand-set renewal date stands until a payment after it arrives.
            if not (keep_next_due and s["next_due"] and s["next_due"] > last["date"]):
                fields["next_due"] = due
        if last and last["amount"]:
            fields["amount"] = last["amount"]  # follow the plan's current price
        con.execute(f"UPDATE subscriptions SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                    (*fields.values(), s["id"]))
    con.commit()


# ---------- reading back ----------

def state(sub, today=None):
    """(state, text) for the status pill."""
    today = today or date.today()
    due = _day(sub["next_due"])
    if sub["status"] == "cancelled":
        if sub["last_paid"] and sub["cancelled_at"] and sub["last_paid"] > sub["cancelled_at"]:
            return "bad", f"Charged after you cancelled ({sub['last_paid']})"
        return "off", f"Cancelled {sub['cancelled_at'] or ''}".strip()
    if due and today > due + timedelta(days=GRACE.get(sub["period"], 5)):
        return "bad", f"Missed: no payment for {due.isoformat()}"
    last, prev = sub["last_amount"], sub["previous_amount"]
    if last is not None and prev is not None and abs(last - prev) >= 0.01:
        return "warn", f"Price went {'up' if last > prev else 'down'} ${abs(last - prev):,.2f}"
    if due and (due - today).days <= DUE_SOON_DAYS:
        return "soon", "Renews soon"
    return "ok", "Active"


def subscriptions(con, today=None):
    ensure(con)
    today = today or date.today()
    out = []
    for r in con.execute("SELECT s.*, (SELECT COUNT(*) FROM expenses e WHERE e.subscription_id = s.id) AS payments"
                         " FROM subscriptions s ORDER BY s.status = 'cancelled', s.next_due IS NULL, s.next_due"):
        s = dict(r)
        s["label"], s["per_month"] = label(s), per_month(s) if s["status"] == "active" else 0
        s["state"], s["state_text"] = state(s, today)
        due = _day(s["next_due"])
        s["days"] = (due - today).days if due else None
        s["color"] = ledger.CATEGORY_COLORS.get(s["category"], "#8592bd")
        out.append(s)
    return out


def upcoming(subs, today=None, days=30):
    """[(date, sub)] renewals of active subscriptions from today through the next `days` days."""
    today = today or date.today()
    end, out = today + timedelta(days=days), []
    for s in subs:
        due = _day(s["next_due"])
        if s["status"] != "active" or not due:
            continue
        if due < today:
            out.append((today, s))  # a late or missed renewal shows on today, so it isn't lost
            while due <= today:
                due = advance(due, s["period"], s["every_days"])
        while due <= end:
            out.append((due, s))
            due = advance(due, s["period"], s["every_days"])
    return sorted(out, key=lambda x: (x[0], x[1]["vendor"] or ""))


def calendar_weeks(subs, today=None, weeks=5):
    """Weeks of days from this week's Sunday, each day with the renewals due on it."""
    today = today or date.today()
    start = today - timedelta(days=(today.weekday() + 1) % 7)
    due = {}
    for d, s in upcoming(subs, today, weeks * 7):
        due.setdefault(d, []).append(s)
    return [[{"day": start + timedelta(days=w * 7 + i), "subs": due.get(start + timedelta(days=w * 7 + i), [])}
             for i in range(7)] for w in range(weeks)]


def totals(subs, today=None):
    today = today or date.today()
    soon = upcoming(subs, today, 30)
    return {"per_month": sum(s["per_month"] for s in subs), "per_year": sum(s["per_month"] for s in subs) * 12,
            "active": sum(s["status"] == "active" for s in subs),
            "next30": sum((s["last_amount"] or s["amount"] or 0) for _, s in soon), "next30_n": len(soon),
            "next": soon[0] if soon else None, "alerts": sum(s["state"] in ("bad", "warn") for s in subs)}


# ---------- suggestions ----------

def _schedule(days):
    """The schedule a run of payment dates follows, or None: (period, every_days)."""
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    if not gaps:
        return None
    mid = median(gaps)
    for period, lo, hi in WINDOWS:
        if all(lo <= g <= hi for g in gaps):
            return period, None
    if len(gaps) >= 2 and 10 <= mid <= 400 and all(abs(g - mid) <= max(2, mid * .12) for g in gaps):
        return "custom", int(round(mid))
    return None


def suggestions(con, today=None):
    """Vendors that look like a schedule but aren't tracked: from receipts, then from card charges."""
    ensure(con)
    today = today or date.today()
    tracked = {r[0] for r in con.execute("SELECT vkey FROM subscriptions")}
    skip = tracked | {r[0] for r in con.execute("SELECT vkey FROM recurring_dismissed")}
    groups = {}
    for e in con.execute("SELECT id, vendor, category, amount, date, billing_period FROM expenses"
                         " WHERE subscription_id IS NULL AND date IS NOT NULL AND amount > 0 ORDER BY date"):
        groups.setdefault(vkey(e["vendor"]), []).append(dict(e, kind="expense", name=e["vendor"]))
    charges = {}
    for t in con.execute("SELECT id, description, amount, date FROM bank_transactions WHERE match_kind IS NULL"
                         " AND COALESCE(match_how, '') != 'ignored' AND amount > 0 AND pending = 0 ORDER BY date"):
        charges.setdefault(vkey(t["description"]), []).append(dict(t, kind="charge", name=t["description"]))
    out = []
    for source in (groups, charges):
        for key, items in source.items():
            if not key or key in skip:
                continue
            latest = items[-1]
            plan = [i for i in items if same_plan(latest["amount"], i["amount"])]
            days = sorted({_day(i["date"]) for i in plan if _day(i["date"])})
            found = _schedule(days[-6:]) if len(days) >= 2 else None
            if not found and latest.get("billing_period") in ("weekly", "monthly", "yearly"):
                found = (latest["billing_period"], None)  # the receipt itself says it renews
            if not found:
                continue
            period, every = found
            last = days[-1] if days else _day(latest["date"])
            if (today - last).days > 2 * (every or {"weekly": 7, "monthly": 31, "quarterly": 92}.get(period, 366)) + 7:
                continue  # stopped long ago
            out.append({"key": key, "name": latest["name"], "kind": latest["kind"], "id": latest["id"],
                        "category": latest.get("category") or "other", "amount": latest["amount"], "period": period,
                        "every_days": every, "dates": [d.isoformat() for d in days[-4:]], "count": len(plan),
                        "label": label({"period": period, "every_days": every})})
            skip.add(key)
    return out
