"""Storage for email-derived orders and botting expenses. Several emails about one order (confirmed, shipped, delivered)
are merged into a single order row."""

import json
import re
from datetime import datetime, timezone

from .extract import STATUSES

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    message_id TEXT PRIMARY KEY,
    sender TEXT,
    subject TEXT,
    date TEXT,
    kind TEXT,
    error TEXT,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS tracked_orders (
    id INTEGER PRIMARY KEY,
    merge_key TEXT UNIQUE,
    retailer TEXT,
    order_number TEXT,
    order_date TEXT,
    items TEXT,
    total REAL,
    currency TEXT,
    shipping_address TEXT,
    tracking_number TEXT,
    carrier TEXT,
    shipping_status TEXT,
    estimated_delivery TEXT,
    last_email_date TEXT,
    message_ids TEXT
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY,
    message_id TEXT UNIQUE,
    vendor TEXT,
    category TEXT,
    description TEXT,
    amount REAL,
    currency TEXT,
    date TEXT,
    order_number TEXT
);
"""

ORDER_FIELDS = [
    "retailer", "order_number", "order_date", "total", "currency", "shipping_address",
    "tracking_number", "carrier", "estimated_delivery",
]


def _key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Store:
    def __init__(self, con):
        self.db = con
        self.db.executescript(SCHEMA)

    def seen_ids(self):
        """Message ids already handled; failed ones are retried on the next sync."""
        return {r[0] for r in self.db.execute("SELECT message_id FROM emails WHERE error IS NULL")}

    def record_email(self, email, kind, error=None):
        self.db.execute(
            "INSERT OR REPLACE INTO emails VALUES (?,?,?,?,?,?,?)",
            (email.message_id, email.sender, email.subject, email.date, kind, error,
             datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

    def save(self, email, result):
        kind = result["kind"]
        if kind == "order":
            self._save_order(email, result)
        elif kind == "botting_expense":
            self.db.execute(
                "INSERT OR REPLACE INTO expenses"
                " (message_id, vendor, category, description, amount, currency, date, order_number)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (email.message_id, result["vendor"], result["category"], result["description"],
                 result["amount"], result["currency"], result["date"], result["order_number"]),
            )
        self.record_email(email, kind)

    def _find_order(self, result, email):
        if result["order_number"]:
            row = self.db.execute(
                "SELECT * FROM tracked_orders WHERE merge_key = ?",
                (f"{_key(result['retailer'])}:{_key(result['order_number'])}",),
            ).fetchone()
            if row:
                return row
            # Same order number from a differently-named sender (e.g. "Nike" vs "Nike.com").
            row = self.db.execute(
                "SELECT * FROM tracked_orders WHERE order_number = ?", (result["order_number"],)
            ).fetchone()
            if row:
                return row
        if result["tracking_number"]:
            return self.db.execute(
                "SELECT * FROM tracked_orders WHERE tracking_number = ?", (result["tracking_number"],)
            ).fetchone()
        return None

    def _save_order(self, email, result):
        row = self._find_order(result, email)
        if row is None:
            if result["order_number"]:
                merge_key = f"{_key(result['retailer'])}:{_key(result['order_number'])}"
            else:
                merge_key = f"msg:{email.message_id}"
            self.db.execute(
                "INSERT INTO tracked_orders (merge_key, retailer, order_number, order_date, items, total,"
                " currency, shipping_address, tracking_number, carrier, shipping_status,"
                " estimated_delivery, last_email_date, message_ids)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (merge_key, result["retailer"], result["order_number"], result["order_date"],
                 json.dumps(result["items"]), result["total"], result["currency"],
                 result["shipping_address"], result["tracking_number"], result["carrier"],
                 result["shipping_status"], result["estimated_delivery"], email.date,
                 json.dumps([email.message_id])),
            )
            self.db.commit()
            return

        updates = {}
        newer = (email.date or "") >= (row["last_email_date"] or "")
        for field in ORDER_FIELDS:
            value = result[field]
            if value is None:
                continue
            # Newer emails win (an updated delivery date); older ones only fill gaps.
            if row[field] is None or (newer and field in ("tracking_number", "carrier", "estimated_delivery")):
                updates[field] = value
        if result["items"] and not json.loads(row["items"] or "[]"):
            updates["items"] = json.dumps(result["items"])
        status = result["shipping_status"]
        if status != "unknown" and (newer or row["shipping_status"] == "unknown"):
            updates["shipping_status"] = status
        if newer:
            updates["last_email_date"] = email.date
        ids = json.loads(row["message_ids"] or "[]")
        if email.message_id not in ids:
            updates["message_ids"] = json.dumps(ids + [email.message_id])
        if updates:
            cols = ", ".join(f"{k} = ?" for k in updates)
            self.db.execute(f"UPDATE tracked_orders SET {cols} WHERE id = ?", (*updates.values(), row["id"]))
            self.db.commit()

    def orders(self):
        rows = self.db.execute(
            "SELECT * FROM tracked_orders ORDER BY COALESCE(order_date, last_email_date) DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d["items"] or "[]")
            d["message_ids"] = json.loads(d["message_ids"] or "[]")
            out.append(d)
        return out

    def expenses(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM expenses ORDER BY date DESC")]

    def stats(self):
        orders = self.orders()
        by_status = {s: 0 for s in STATUSES}
        for o in orders:
            by_status[o["shipping_status"] or "unknown"] += 1
        expenses = self.expenses()
        by_category = {}
        by_month = {}
        for e in expenses:
            amount = e["amount"] or 0
            by_category[e["category"]] = by_category.get(e["category"], 0) + amount
            month = (e["date"] or "")[:7] or "unknown"
            by_month[month] = by_month.get(month, 0) + amount
        return {
            "order_count": len(orders),
            "order_total": sum(o["total"] or 0 for o in orders),
            "in_flight": sum(by_status[s] for s in ("shipped", "in_transit", "out_for_delivery")),
            "by_status": {k: v for k, v in by_status.items() if v},
            "expense_total": sum(by_category.values()),
            "expense_by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
            "expense_by_month": dict(sorted(by_month.items(), reverse=True)),
        }
