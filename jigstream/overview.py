"""The Dashboard's numbers for one year: what was really spent, orders that stuck, and how checkouts went."""
from datetime import date

from . import bank, sales, stuck
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
        "SELECT category, COALESCE(SUM(amount - COALESCE(refunded, 0)), 0) AS total, COUNT(*) AS n FROM expenses WHERE substr(date, 1, 4) = ?"
        " GROUP BY category ORDER BY total DESC", (year,))]
    for c in cats:
        c["color"] = CATEGORY_COLORS.get(c["category"], "#8592bd")
    card_only = bank.botting_card_spend(con, year)
    if card_only:
        cats.append({"category": "card_charges", "total": card_only, "n": None, "color": "#8592bd"})
        cats.sort(key=lambda c: -c["total"])
    expense_total = sum(c["total"] for c in cats)

    # A "successful" checkout only counts once no cancel email, bot cancel or refund came for it.
    real = {"emailed", "no_email"}
    wins = [r for r in stuck.cross_reference(con) if (r.get("ts") or "")[:4] == year
            and r["state"] != "not_order"]
    lost = con.execute("SELECT status, site, bot FROM checkouts WHERE status IN ('decline', 'failure') AND substr(ts, 1, 4) = ?",
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


def spend_breakdown(con, year):
    """Where a year's spend comes from: orders by store, the biggest orders, botting receipts by vendor, and
    charges on botting-only cards, so a number that looks too big can be traced and corrected."""
    year = str(year)
    marks = ",".join("?" * len(NOT_SPENT))
    kept = f"{ORDER_DAY} = ? AND COALESCE(shipping_status, '') NOT IN ({marks})"
    stores = [dict(r) for r in con.execute(
        f"SELECT COALESCE(retailer, '?') AS name, COUNT(*) AS n, SUM(total) AS total FROM tracked_orders WHERE {kept}"
        " GROUP BY lower(COALESCE(retailer, '?')) ORDER BY total DESC LIMIT 25", (year, *NOT_SPENT))]
    biggest = [dict(r) for r in con.execute(
        f"SELECT id, retailer, order_number, order_date, total, currency, shipping_status, account FROM tracked_orders"
        f" WHERE {kept} AND total IS NOT NULL ORDER BY total DESC LIMIT 30", (year, *NOT_SPENT))]
    vendors = [dict(r) for r in con.execute(
        "SELECT vendor AS name, category, COUNT(*) AS n, SUM(amount - COALESCE(refunded, 0)) AS total,"
        " SUM(COALESCE(refunded, 0)) AS refunded FROM expenses WHERE substr(date, 1, 4) = ?"
        " GROUP BY lower(vendor) ORDER BY total DESC LIMIT 25", (year,))]
    no_total = con.execute(f"SELECT COUNT(*) FROM tracked_orders WHERE {kept} AND total IS NULL",
                           (year, *NOT_SPENT)).fetchone()[0]
    return {"year": year, "stores": stores, "biggest": biggest, "vendors": vendors, "no_total": no_total,
            "cards": bank.botting_card_spend(con, year)}
