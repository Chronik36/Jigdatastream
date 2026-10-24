"""Monthly budgets for botting expenses, per category.

A budget is a monthly amount per category. Changing it applies from the current month on, so earlier
months are still compared with the budget you had then. For the month in progress the page shows what is
spent, what tracked subscriptions will still renew before the month ends, and where that leaves you.
"""
import calendar
import csv
import io
from datetime import date

from . import ledger, recurring
from .extract import EXPENSE_CATEGORIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS budgets (
    category TEXT, month_from TEXT, amount REAL,
    PRIMARY KEY (category, month_from)
);
"""


def ensure(con):
    recurring.ensure(con)
    con.executescript(SCHEMA)
    return con


def month_of(d):
    return f"{d.year:04d}-{d.month:02d}"


def shift(month, n):
    y, m = int(month[:4]), int(month[5:7]) - 1 + n
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def month_name(month):
    return f"{calendar.month_name[int(month[5:7])]} {month[:4]}"


def budgets(con, month):
    """{category: amount} in force during that month."""
    ensure(con)
    out = {}
    for r in con.execute("SELECT category, amount FROM budgets b WHERE month_from = (SELECT MAX(month_from) FROM"
                         " budgets WHERE category = b.category AND month_from <= ?)", (month,)):
        if r["amount"]:
            out[r["category"]] = r["amount"]
    return out


def set_budgets(con, amounts, today=None):
    """Save {category: amount or None} from this month on."""
    ensure(con)
    month = month_of(today or date.today())
    for category, amount in amounts.items():
        con.execute("INSERT OR REPLACE INTO budgets VALUES (?,?,?)",
                    (category, month, round(amount, 2) if amount and amount > 0 else 0))
    con.commit()


def spent(con, month):
    ensure(con)
    return {r["category"] or "other": r["total"] or 0 for r in con.execute(
        "SELECT category, SUM(amount - COALESCE(refunded, 0)) AS total FROM expenses WHERE substr(date, 1, 7) = ? GROUP BY category", (month,))}


def month_view(con, month, today=None):
    """Spent, budget and (for the month in progress) renewals still to come, per category."""
    ensure(con)
    today = today or date.today()
    plan, used = budgets(con, month), spent(con, month)
    current = month == month_of(today)
    to_renew = {}
    renewals = []
    if current:
        last_day = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        for d, s in recurring.upcoming(recurring.subscriptions(con, today), today, (last_day - today).days):
            if d > today or s["state"] != "bad":
                amount = s["last_amount"] if s["last_amount"] is not None else (s["amount"] or 0)
                to_renew[s["category"]] = to_renew.get(s["category"], 0) + amount
                renewals.append((d, s))
    cats = [c for c in EXPENSE_CATEGORIES if c in plan or c in used or c in to_renew]
    cats += [c for c in list(plan) + list(used) if c not in cats]
    rows = []
    for c in cats:
        b, s, r = plan.get(c, 0), used.get(c, 0), to_renew.get(c, 0)
        rows.append({"category": c, "budget": b, "spent": s, "to_renew": r, "projected": s + r,
                     "pct": (s / b * 100) if b else None, "color": ledger.CATEGORY_COLORS.get(c, "#8592bd"),
                     "state": _state(s + r, b)})
    total_b, total_s, total_r = sum(plan.values()), sum(used.values()), sum(to_renew.values())
    days = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
    return {"month": month, "name": month_name(month), "current": current, "rows": rows, "budget": total_b,
            "spent": total_s, "to_renew": total_r, "projected": total_s + total_r, "left": total_b - total_s - total_r,
            "pct": (total_s / total_b * 100) if total_b else None, "state": _state(total_s + total_r, total_b),
            "day": today.day if current else days, "days": days, "renewals": renewals}


def _state(amount, budget):
    if not budget:
        return "none"
    if amount > budget:
        return "over"
    return "near" if amount >= budget * .9 else "ok"


def history(con, n=12, today=None, width=720, height=230):
    """Each of the last n months: spent and budget, with bar and budget-line positions for an SVG."""
    ensure(con)
    months = ledger.last_months(n, today)
    rows = []
    for m in months:
        b, s = sum(budgets(con, m).values()), sum(spent(con, m).values())
        rows.append({"month": m, "label": ledger.MONTH_NAMES[int(m[5:]) - 1], "name": month_name(m),
                     "spent": s, "budget": b, "diff": s - b if b else None})
    top = ledger.nice_max(max([max(r["spent"], r["budget"]) for r in rows] + [0]))
    left, right, pad_top, bottom = 52, 8, 10, 26
    plot_h, slot = height - pad_top - bottom, (width - left - right) / n
    y = lambda v: round(pad_top + plot_h - v / top * plot_h, 1)
    for i, r in enumerate(rows):
        r.update(x=round(left + i * slot + slot * .2, 1), w=round(slot * .6, 1), y=y(r["spent"]),
                 h=round(y(0) - y(r["spent"]), 1), by=y(r["budget"]) if r["budget"] else None,
                 bx1=round(left + i * slot + 3, 1), bx2=round(left + (i + 1) * slot - 3, 1),
                 color="#ff6b7a" if r["budget"] and r["spent"] > r["budget"] else "#3dd8ef")
    grid = [{"v": top * k / 4, "y": y(top * k / 4)} for k in range(5)]
    return {"rows": rows, "grid": grid, "width": width, "height": height, "left": left, "right": right}


def export_csv(con, month=None):
    ensure(con)
    q = "SELECT date, vendor, category, description, amount, currency, billing_period, paid_with, account FROM expenses"
    rows = con.execute(q + (" WHERE substr(date, 1, 7) = ?" if month else "") + " ORDER BY date",
                       (month,) if month else ())
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Vendor", "Category", "Description", "Amount", "Currency", "Billing", "Paid with", "Inbox"])
    for r in rows:
        w.writerow(list(r))
    return out.getvalue()
