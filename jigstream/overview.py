"""The Dashboard's numbers for one year: what was really spent, orders that stuck, and how checkouts went."""
from datetime import date

from . import sales, stuck
from .ledger import CATEGORY_COLORS
from .orderstore import NOT_SPENT

ORDER_DAY = "substr(COALESCE(order_date, last_email_date), 1, 4)"


def years(con):
    """Years that have orders, expenses or checkouts, newest first (this year always included)."""
    found = {str(date.today().year)}
    for q in (f"SELECT DISTINCT {ORDER_DAY} FROM tracked_orders", "SELECT DISTINCT substr(date, 1, 4) FROM expenses",
              "SELECT DISTINCT substr(ts, 1, 4) FROM checkouts"):
        found.update(r[0] for r in con.execute(q) if r[0] and r[0].isdigit())
    return sorted(found, reverse=True)


def _rate(good, total):
    return round(100 * good / total) if total else 0


def summary(con, year):
    year = str(year)
    marks = ",".join("?" * len(NOT_SPENT))
    kept = con.execute(f"SELECT COUNT(*), COALESCE(SUM(total), 0) FROM tracked_orders WHERE {ORDER_DAY} = ?"
                       f" AND COALESCE(shipping_status, '') NOT IN ({marks})", (year, *NOT_SPENT)).fetchone()
    dropped = con.execute(f"SELECT COUNT(*) FROM tracked_orders WHERE {ORDER_DAY} = ?"
                          " AND shipping_status IN ('cancelled', 'returned', 'refunded')", (year,)).fetchone()[0]
    cats = [dict(r) for r in con.execute(
        "SELECT category, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n FROM expenses WHERE substr(date, 1, 4) = ?"
        " GROUP BY category ORDER BY total DESC", (year,))]
    for c in cats:
        c["color"] = CATEGORY_COLORS.get(c["category"], "#8592bd")
    expense_total = sum(c["total"] for c in cats)

    # A "successful" checkout only counts once no cancel email, bot cancel or refund came for it.
    real = {"emailed", "no_email"}
    wins = [r for r in stuck.cross_reference(con) if (r.get("ts") or "")[:4] == year
            and r["state"] != "not_order"]
    lost = con.execute("SELECT status, site, bot FROM checkouts WHERE status != 'success' AND substr(ts, 1, 4) = ?",
                       (year,)).fetchall()
    by = {"site": {}, "bot": {}}
    for kind in by:
        for r in wins:
            row = by[kind].setdefault(r.get(kind) or "?", {"good": 0, "cancelled": 0, "failed": 0})
            row["good" if r["state"] in real else "cancelled"] += 1
        for r in lost:
            by[kind].setdefault(r[kind] or "?", {"good": 0, "cancelled": 0, "failed": 0})["failed"] += 1
    boards = {}
    for kind, rows in by.items():
        out = []
        for name, r in rows.items():
            n = r["good"] + r["cancelled"] + r["failed"]
            out.append(dict(r, name=name, n=n, rate=_rate(r["good"], n)))
        boards[kind] = sorted(out, key=lambda r: (-r["n"], r["name"]))[:8]
    good = sum(1 for r in wins if r["state"] in real)
    cancelled = len(wins) - good
    return {
        "year": year, "spend": kept[1], "orders": kept[0], "orders_dropped": dropped,
        "expenses": expense_total, "categories": cats, "total_out": kept[1] + expense_total,
        "checkouts": len(wins) + len(lost), "good": good, "cancelled": cancelled, "failed": len(lost),
        "rate": _rate(good, len(wins) + len(lost)), "sites": boards["site"], "bots": boards["bot"],
        "income": sales.year_income(con, year),
    }
