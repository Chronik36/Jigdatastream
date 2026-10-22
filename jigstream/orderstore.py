"""Storage for email-derived orders and botting expenses. Several emails about one order (confirmed, shipped, delivered)
are merged into a single order row."""

import json
import re
import uuid
from datetime import datetime, timezone

from .extract import PROGRESS, STATUSES, status_advances

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_accounts (
    address TEXT PRIMARY KEY,
    last_synced_at TEXT,
    backfilled_at TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    message_id TEXT PRIMARY KEY,
    account TEXT,
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
    tracking_url TEXT,
    carrier TEXT,
    shipping_status TEXT,
    estimated_delivery TEXT,
    last_email_date TEXT,
    message_ids TEXT,
    account TEXT,
    status_manual INTEGER DEFAULT 0
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
    order_number TEXT,
    billing_period TEXT,
    account TEXT
);
"""

# Columns added after the first release; created on older databases at startup.
MIGRATIONS = [("tracked_orders", "status_manual", "INTEGER DEFAULT 0")]

LABEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    order_id INTEGER,
    status TEXT,
    previous_status TEXT,
    created_at TEXT,
    rescan TEXT
);
CREATE TABLE IF NOT EXISTS training_examples (
    id INTEGER PRIMARY KEY,
    label_id INTEGER,
    order_id INTEGER,
    message_id TEXT,
    sender TEXT,
    subject TEXT,
    email_date TEXT,
    prompt TEXT,
    model_output TEXT,
    model_status TEXT,
    corrected TEXT,
    status TEXT,
    model_was_right INTEGER,
    created_at TEXT,
    UNIQUE (label_id, message_id)
);
CREATE TABLE IF NOT EXISTS inbox_labels (
    message_id TEXT PRIMARY KEY,
    account TEXT,
    sender TEXT,
    subject TEXT,
    email_date TEXT,
    label TEXT,
    state TEXT,
    created_at TEXT
);
"""

ORDER_FIELDS = [
    "retailer", "order_number", "order_date", "total", "currency", "shipping_address",
    "tracking_number", "tracking_url", "carrier", "estimated_delivery",
]


def _key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Store:
    def __init__(self, con):
        self.db = con
        self.db.executescript(SCHEMA)
        self.db.executescript(LABEL_SCHEMA)
        for table, column, kind in MIGRATIONS:
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.db.commit()

    def seen_ids(self):
        """Message ids already handled; failed ones are retried on the next sync."""
        return {r[0] for r in self.db.execute("SELECT message_id FROM emails WHERE error IS NULL")}

    def record_email(self, email, kind, error=None):
        self.db.execute(
            "INSERT OR REPLACE INTO emails VALUES (?,?,?,?,?,?,?,?)",
            (email.message_id, email.account, email.sender, email.subject, email.date, kind, error,
             datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

    def account_state(self, address):
        row = self.db.execute("SELECT * FROM mail_accounts WHERE address = ?", (address,)).fetchone()
        return dict(row) if row else None

    def mark_synced(self, address, started_at, full=False):
        """Record a finished sync. started_at is when it began, so mail that
        arrived during a long sync is picked up next time."""
        self.db.execute("INSERT OR IGNORE INTO mail_accounts (address) VALUES (?)", (address,))
        self.db.execute("UPDATE mail_accounts SET last_synced_at = ? WHERE address = ?", (started_at, address))
        if full:
            self.db.execute("UPDATE mail_accounts SET backfilled_at = ? WHERE address = ?", (started_at, address))
        self.db.commit()

    def accounts(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM mail_accounts ORDER BY address")]

    def save(self, email, result):
        kind = result["kind"]
        if kind == "order":
            self._save_order(email, result)
        elif kind == "botting_expense":
            self.db.execute(
                "INSERT OR REPLACE INTO expenses"
                " (message_id, vendor, category, description, amount, currency, date, order_number,"
                " billing_period, account) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (email.message_id, result["vendor"], result["category"], result["description"],
                 result["amount"], result["currency"], result["date"], result["order_number"],
                 result.get("billing_period"), email.account),
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
                " currency, shipping_address, tracking_number, tracking_url, carrier, shipping_status,"
                " estimated_delivery, last_email_date, message_ids, account)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (merge_key, result["retailer"], result["order_number"], result["order_date"],
                 json.dumps(result["items"]), result["total"], result["currency"],
                 result["shipping_address"], result["tracking_number"], result.get("tracking_url"),
                 result["carrier"],
                 result["shipping_status"], result["estimated_delivery"], email.date,
                 json.dumps([email.message_id]), email.account),
            )
            self.db.commit()
            return

        updates = {}
        newer = (email.date or "") >= (row["last_email_date"] or "")
        for field in ORDER_FIELDS:
            value = result.get(field)
            if value is None:
                continue
            # Newer emails win (an updated delivery date); older ones only fill gaps.
            if row[field] is None or (newer and field in ("tracking_number", "tracking_url", "carrier", "estimated_delivery")):
                updates[field] = value
        if updates.get("tracking_number", row["tracking_number"]) != row["tracking_number"]:
            updates["tracking_url"] = result.get("tracking_url")  # a link for the old number no longer fits
        if result["items"] and not json.loads(row["items"] or "[]"):
            updates["items"] = json.dumps(result["items"])
        status = result["shipping_status"]
        old = row["shipping_status"]
        if row["status_manual"]:
            status = None  # a status you set by hand only changes when you change it
        # Newer emails can cancel or advance an order; an older email arriving late can only
        # move it forward (e.g. a missed "shipped" email after "ordered").
        if status_advances(old, status) and (newer or status in PROGRESS or old in (None, "unknown")):
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

    def set_status_manually(self, order_id, status):
        """Record a correction from the Orders page. Returns the label id."""
        row = self.db.execute("SELECT shipping_status FROM tracked_orders WHERE id = ?", (order_id,)).fetchone()
        if row is None or status not in STATUSES:
            return None
        self.db.execute("UPDATE tracked_orders SET shipping_status = ?, status_manual = 1 WHERE id = ?",
                        (status, order_id))
        cur = self.db.execute(
            "INSERT INTO labels (order_id, status, previous_status, created_at, rescan) VALUES (?,?,?,?,?)",
            (order_id, status, row["shipping_status"], datetime.now(timezone.utc).isoformat(), "pending"))
        self.db.commit()
        return cur.lastrowid

    # Hand edits from the phone pages. Orders and expenses typed in by hand get a "manual:" key so a
    # later sync never mistakes them for an email it already read.
    def add_order(self, fields):
        number = fields.get("order_number")
        key = f"{_key(fields.get('retailer'))}:{_key(number)}" if number else f"manual:{uuid.uuid4().hex}"
        if self.db.execute("SELECT 1 FROM tracked_orders WHERE merge_key = ?", (key,)).fetchone():
            return None
        cur = self.db.execute(
            "INSERT INTO tracked_orders (merge_key, retailer, order_number, order_date, items, total, currency,"
            " shipping_address, tracking_number, carrier, shipping_status, estimated_delivery, message_ids,"
            " account, status_manual) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (key, fields.get("retailer"), number, fields.get("order_date"), json.dumps(fields.get("items") or []),
             fields.get("total"), fields.get("currency") or "USD", fields.get("shipping_address"),
             fields.get("tracking_number"), fields.get("carrier"), fields.get("shipping_status") or "ordered",
             fields.get("estimated_delivery"), "[]", "added by hand"))
        self.db.commit()
        return cur.lastrowid

    def update_order(self, order_id, fields):
        """Save edited order fields. A changed status goes through set_status_manually so syncs keep it."""
        row = self.db.execute("SELECT * FROM tracked_orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            return False
        status = fields.pop("shipping_status", None)
        if "items" in fields:
            fields["items"] = json.dumps(fields["items"])
        if fields.get("tracking_number") != row["tracking_number"]:
            fields["tracking_url"] = None  # a link from the email only fits the old number
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE tracked_orders SET {cols} WHERE id = ?", (*fields.values(), order_id))
            self.db.commit()
        if status and status != row["shipping_status"]:
            label = self.set_status_manually(order_id, status)
            self.set_rescan_state(label, "Set from your phone")
        return True

    def delete_order(self, order_id):
        self.db.execute("DELETE FROM tracked_orders WHERE id = ?", (order_id,))
        self.db.commit()

    def expense(self, expense_id):
        row = self.db.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        return dict(row) if row else None

    def add_expense(self, fields):
        cols = ["message_id", "account", *fields]
        cur = self.db.execute(f"INSERT INTO expenses ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                              (f"manual:{uuid.uuid4().hex}", "added by hand", *fields.values()))
        self.db.commit()
        return cur.lastrowid

    def update_expense(self, expense_id, fields):
        self.db.execute(f"UPDATE expenses SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        (*fields.values(), expense_id))
        self.db.commit()

    def delete_expense(self, expense_id):
        self.db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        self.db.commit()

    def set_rescan_state(self, label_id, text):
        self.db.execute("UPDATE labels SET rescan = ? WHERE id = ?", (text, label_id))
        self.db.commit()

    def add_example(self, label_id, order_id, email, prompt, raw, status, matched):
        """Keep one email from a labelled order as a training example. For emails about the status
        you chose, the corrected answer is the model's own answer with that status."""
        corrected = dict(raw)
        if matched:
            corrected.update({"kind": "order", "shipping_status": status})
        self.db.execute(
            "INSERT OR REPLACE INTO training_examples (label_id, order_id, message_id, sender, subject, email_date,"
            " prompt, model_output, model_status, corrected, status, model_was_right, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (label_id, order_id, email.message_id, email.sender, email.subject, email.date, prompt,
             json.dumps(raw), raw.get("shipping_status"), json.dumps(corrected) if matched else None,
             status, int(raw.get("shipping_status") == status) if matched else None,
             datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def set_inbox_label(self, row, label, state):
        self.db.execute(
            "INSERT OR REPLACE INTO inbox_labels VALUES (?,?,?,?,?,?,?,?)",
            (row["message_id"], row["account"], row.get("sender"), row.get("subject"), row.get("date"),
             label, state, datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def inbox_labels(self):
        return {r["message_id"]: dict(r) for r in self.db.execute("SELECT * FROM inbox_labels")}

    def add_inbox_example(self, email, prompt, raw, corrected, label):
        """An email labelled on the Inbox page: the answer the model should have given."""
        self.db.execute("DELETE FROM training_examples WHERE label_id IS NULL AND message_id = ?", (email.message_id,))
        right = raw.get("kind") == corrected.get("kind") and (
            corrected.get("kind") != "order" or raw.get("shipping_status") == corrected.get("shipping_status"))
        self.db.execute(
            "INSERT INTO training_examples (label_id, order_id, message_id, sender, subject, email_date,"
            " prompt, model_output, model_status, corrected, status, model_was_right, created_at)"
            " VALUES (NULL,NULL,?,?,?,?,?,?,?,?,?,?,?)",
            (email.message_id, email.sender, email.subject, email.date, prompt, json.dumps(raw),
             raw.get("shipping_status") or raw.get("kind"), json.dumps(corrected), label, int(right),
             datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def examples(self, only_corrected=True):
        q = "SELECT * FROM training_examples" + (" WHERE corrected IS NOT NULL" if only_corrected else "")
        return [dict(r) for r in self.db.execute(q + " ORDER BY id DESC")]

    def labels(self):
        """Latest correction per order."""
        return {r["order_id"]: dict(r) for r in self.db.execute("SELECT * FROM labels ORDER BY id")}

    def order(self, order_id):
        return next((o for o in self.orders() if o["id"] == order_id), None)

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
        by_vendor = {}
        for e in expenses:
            amount = e["amount"] or 0
            by_category[e["category"]] = by_category.get(e["category"], 0) + amount
            by_vendor[e["vendor"] or "?"] = by_vendor.get(e["vendor"] or "?", 0) + amount
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
            "expense_by_vendor": dict(sorted(by_vendor.items(), key=lambda kv: -kv[1])),
        }
