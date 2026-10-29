"""Storage for email-derived orders and botting expenses. Several emails about one order (confirmed, shipped, delivered)
are merged into a single order row."""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

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
MIGRATIONS = [("tracked_orders", "status_manual", "INTEGER DEFAULT 0"),
              ("tracked_orders", "carrier_status", "TEXT"), ("tracked_orders", "carrier_checked_at", "TEXT"),
              ("expenses", "amount_checked", "INTEGER DEFAULT 0"),
              ("tracked_orders", "shipping_name", "TEXT"), ("tracked_orders", "recipient_email", "TEXT"),
              ("tracked_orders", "profile_name", "TEXT"), ("tracked_orders", "profile_match", "TEXT"),
              ("tracked_orders", "profile_candidates", "TEXT"), ("tracked_orders", "profile_manual", "INTEGER DEFAULT 0"),
              ("expenses", "refunded", "REAL"),
              # a price changed by hand (pricecheck.py): never changed by a sync or the price re-check
              ("tracked_orders", "total_manual", "INTEGER DEFAULT 0"), ("expenses", "amount_manual", "INTEGER DEFAULT 0"),
              ("expenses", "reviewed", "INTEGER DEFAULT 0"),  # gone through on Expenses > Review
              # 1 = you said the address it shipped to is fine (addresscheck.py)
              ("tracked_orders", "address_ok", "INTEGER DEFAULT 0"),
              # why it was marked cancelled: JSON list of "cancel email", "<bot> decline", "card refund" (cancels.py)
              ("tracked_orders", "cancel_why", "TEXT")]

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
    "retailer", "order_number", "order_date", "total", "currency", "shipping_address", "shipping_name",
    "recipient_email", "tracking_number", "tracking_url", "carrier", "estimated_delivery",
]


# Lookups done on every sync and page load. Created after MIGRATIONS, since some columns are added there.
INDEXES = """
CREATE INDEX IF NOT EXISTS tracked_orders_number ON tracked_orders (order_number);
CREATE INDEX IF NOT EXISTS tracked_orders_tracking ON tracked_orders (tracking_number);
CREATE INDEX IF NOT EXISTS tracked_orders_profile ON tracked_orders (profile_name);
CREATE INDEX IF NOT EXISTS expenses_date ON expenses (date);
CREATE INDEX IF NOT EXISTS labels_order ON labels (order_id);
"""


def _setup(con):
    con.executescript(SCHEMA)
    con.executescript(LABEL_SCHEMA)
    for table, column, kind in MIGRATIONS:
        if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    con.executescript(INDEXES)
    con.commit()
    _reread_cancels(con)


def _reread_cancels(con):
    """Once: forget cancel emails that were skipped (no price in them) or that the model called not relevant,
    so the next sync reads them again and finds the order by the number they show."""
    from types import SimpleNamespace
    from .extract import cancel_signal
    con.execute("CREATE TABLE IF NOT EXISTS store_fixes (name TEXT PRIMARY KEY)")
    if con.execute("SELECT 1 FROM store_fixes WHERE name = 'reread_cancels'").fetchone():
        return
    ids = [r[0] for r in con.execute("SELECT message_id, subject FROM emails WHERE kind IN ('skipped', 'other')")
           if cancel_signal(SimpleNamespace(subject=r[1] or "", body="")) == "cancelled"]
    for i in range(0, len(ids), 500):
        con.execute("DELETE FROM emails WHERE message_id IN (%s)" % ",".join("?" * len(ids[i:i + 500])), ids[i:i + 500])
    con.execute("INSERT INTO store_fixes VALUES ('reread_cancels')")
    con.commit()


def _order_dict(row):
    d = dict(row)
    d["items"] = json.loads(d["items"] or "[]")
    d["message_ids"] = json.loads(d["message_ids"] or "[]")
    d["profile_candidates"] = json.loads(d["profile_candidates"] or "[]")
    return d


PRODUCT_PICS = re.compile(r"scene7\.com/is/image/Target|media-amazon\.com/images/I/|walmartimages\.com|bbystatic\.com|"
                          r"cdn\.shopify\.com/s/files|shopify\.com/cdn|pokemoncenter|gamestop|images\.footlocker|"
                          r"nike\.com/is/image|static\.nike\.com|samsclub|costco", re.I)


def _words(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 1}


def drop_suggested_items(con):
    """Orders saved before suggested products were filtered: drop cut-off names ("...") listed beside full ones.
    Returns how many orders changed."""
    from .extract import drop_suggested
    n = 0
    for row in con.execute("SELECT id, items FROM tracked_orders WHERE items LIKE '%...%' OR items LIKE '%…%'").fetchall():
        items = json.loads(row[1] or "[]")
        kept = drop_suggested([i for i in items if isinstance(i, dict) and i.get("name")])
        if len(kept) < len(items):
            con.execute("UPDATE tracked_orders SET items=? WHERE id=?", (json.dumps(kept), row[0]))
            n += 1
    con.commit()
    return n


def with_pictures(items, images):
    """Items with the product photo from their email: the picture whose alt text names the item, or for a
    one-item order the store's own product picture. Returns True when one was added."""
    added = False
    for item in items or []:
        if not isinstance(item, dict) or item.get("image") or not images:
            continue
        want = _words(item.get("name"))
        best, score = "", 0.0
        for alt, src in images:
            have = _words(alt)
            if want and have:
                s = len(want & have) / min(len(want), len(have))
                if s > score:
                    best, score = src, s
        if score < 0.5:
            best = ""
            if len(items) == 1:
                best = next((src for _, src in images if PRODUCT_PICS.search(src)), "")
        if best:
            item["image"], added = best, True
    return added


def _key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


NOT_SPENT = ("cancelled", "returned", "refunded", "not_an_order")  # orders that didn't stick
CANCEL_WINDOW_DAYS = 60  # store + To address matching; an order number matches at any age


class Store:
    def __init__(self, con):
        from . import db
        self.db = db.setup_once(con, "orderstore", _setup)

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

    def order_named_in(self, email):
        """The one open order whose order number is written in this email (subject or body), however it's
        spaced or dashed, or None."""
        text = f"{email.subject}\n{(email.body or '')[:20000]}"
        hits = {}
        for row in self.db.execute("SELECT * FROM tracked_orders WHERE order_number IS NOT NULL AND order_number != ''"
                                   " AND COALESCE(shipping_status, '') NOT IN ('cancelled', 'refunded', 'returned')"):
            number = _key(row["order_number"])
            if len(number) < 5 or not re.search(r"\d", number) or number in hits:
                continue
            spaced = r"[\s#-]?".join(re.escape(c) for c in number)
            if re.search(rf"(?<![A-Za-z0-9]){spaced}(?![A-Za-z0-9])", text, re.I):
                hits[number] = row
        # one order named; a longer number that contains a shorter one wins
        best = [n for n in hits if not any(n != m and n in m for m in hits)]
        return hits[best[0]] if len(best) == 1 else None

    def save(self, email, result):
        kind = result["kind"]
        if kind == "other":
            from .extract import cancel_signal
            if cancel_signal(email) == "cancelled":
                row = self.order_named_in(email)
                if row is not None:  # a cancel the model filed as a plain notice, naming an order we have
                    result = {"kind": "order", "retailer": row["retailer"], "order_number": row["order_number"],
                              "order_date": None, "items": [], "total": None, "currency": row["currency"],
                              "shipping_address": None, "shipping_name": None, "recipient_email": None,
                              "tracking_number": None, "tracking_url": None, "carrier": None,
                              "shipping_status": "cancelled", "estimated_delivery": None}
                    kind = "order"
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
        elif kind == "sale":
            from . import sales
            sales.save_email(self.db, email, result)
        self.record_email(email, kind)

    def _find_order(self, result, email):
        if result["order_number"]:
            row = self.db.execute(
                "SELECT * FROM tracked_orders WHERE merge_key = ?",
                (f"{_key(result['retailer'])}:{_key(result['order_number'])}",),
            ).fetchone()
            if row:
                return row
            # Same order number from a differently-named sender ("Nike" vs "Nike.com"), or written
            # differently ("#112-334" vs "112334").
            want = _key(result["order_number"])
            for row in self.db.execute("SELECT * FROM tracked_orders WHERE order_number IS NOT NULL"):
                if _key(row["order_number"]) == want:
                    return row
        if result["tracking_number"]:
            row = self.db.execute(
                "SELECT * FROM tracked_orders WHERE tracking_number = ?", (result["tracking_number"],)
            ).fetchone()
            if row:
                return row
        if result["shipping_status"] in ("cancelled", "refunded"):
            # the order number the cancel shows, even when the model copied it wrong or not at all
            return self.order_named_in(email) or self._order_for_cancel(result, email)
        return self._cancel_for_order(result, email)

    def _same_buyer_orders(self, result, email, where):
        from .stuck import same_site, when
        to, sent = result.get("recipient_email"), when(email.date)
        if not to or not sent:
            return []
        picks = []
        for row in self.db.execute(f"SELECT * FROM tracked_orders WHERE lower(recipient_email) = ? AND {where}",
                                   (to.lower(),)):
            if not same_site(row["retailer"], result["retailer"]):
                continue
            if result["order_number"] and row["order_number"] and \
                    _key(row["order_number"]) != _key(result["order_number"]):
                continue  # a different order from the same store
            placed = when(row["last_email_date"]) or when(row["order_date"])
            if placed:
                picks.append((row, sent - placed))
        return picks

    def _order_for_cancel(self, result, email):
        """A cancel email with no order number we already know: the open order from the same store sent to
        the same address (the To address) from CANCEL_WINDOW_DAYS before it to a day after (the cancel can land
        before the confirmation, or a month later for drops like Pokemon Center). With several, the one whose
        items the cancel names; None when that isn't clear."""
        picks = [r for r, gap in self._same_buyer_orders(
            result, email, "COALESCE(shipping_status, '') NOT IN ('cancelled', 'refunded', 'returned', 'delivered')")
            if -timedelta(days=1) <= gap <= timedelta(days=CANCEL_WINDOW_DAYS)]
        if len(picks) > 1:
            names = {_key(i.get("name"))[:40] for i in result.get("items") or [] if i.get("name")}
            picks = [r for r in picks if names & {_key(i.get("name"))[:40] for i in json.loads(r["items"] or "[]")
                                                  if isinstance(i, dict) and i.get("name")}]
        return picks[0] if len(picks) == 1 else None

    def _cancel_for_order(self, result, email):
        """An order confirmation that arrives after its cancel: the one cancel saved on its own (no order
        number, or this one) from the same store to the same address within a day."""
        picks = [r for r, gap in self._same_buyer_orders(
            result, email, "shipping_status = 'cancelled' AND (order_number IS NULL OR order_number = '')"
            " AND merge_key LIKE 'msg:%'") if abs(gap) <= timedelta(days=1)]
        return picks[0] if len(picks) == 1 else None

    def _save_order(self, email, result):
        with_pictures(result.get("items"), getattr(email, "images", None))
        row = self._find_order(result, email)
        if row is None:
            if result["order_number"]:
                merge_key = f"{_key(result['retailer'])}:{_key(result['order_number'])}"
            else:
                merge_key = f"msg:{email.message_id}"
            self.db.execute(
                "INSERT INTO tracked_orders (merge_key, retailer, order_number, order_date, items, total,"
                " currency, shipping_address, tracking_number, tracking_url, carrier, shipping_status,"
                " estimated_delivery, last_email_date, message_ids, account, shipping_name, recipient_email)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (merge_key, result["retailer"], result["order_number"], result["order_date"],
                 json.dumps(result["items"]), result["total"], result["currency"],
                 result["shipping_address"], result["tracking_number"], result.get("tracking_url"),
                 result["carrier"],
                 result["shipping_status"], result["estimated_delivery"], email.date,
                 json.dumps([email.message_id]), email.account, result.get("shipping_name"),
                 result.get("recipient_email")),
            )
            self.db.commit()
            if result["shipping_status"] not in (None, "ordered", "unknown"):
                from . import notify
                notify.order_moved(dict(result, id=self.db.execute("SELECT last_insert_rowid()").fetchone()[0]),
                                   None, result["shipping_status"], email.date)
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
        else:
            items = json.loads(row["items"] or "[]")
            if with_pictures(items, getattr(email, "images", None)):  # a later email (shipped) shows the product
                updates["items"] = json.dumps(items)
        status = result["shipping_status"]
        old = row["shipping_status"]
        if row["status_manual"]:
            status = None  # a status you set by hand only changes when you change it
        # Newer emails can cancel or advance an order; an older email arriving late can only
        # move it forward (e.g. a missed "shipped" email after "ordered").
        # A cancel, return or refund is final: an older email read later (the order confirmation after
        # the cancel) never undoes it.
        # The cancel can also arrive first, so a confirmation after it ("ordered", "processing") never
        # reopens it either; only real shipping news, a refund or a return moves it on.
        final = old in ("cancelled", "returned", "refunded") and (
            not newer or (old == "cancelled" and status not in ("shipped", "in_transit", "out_for_delivery",
                                                                "delivered", "refunded", "returned")))
        # A cancel of an order that hasn't shipped always counts, whichever email is older: the cancel often lands
        # a minute before the confirmation, and dates from forwarded mail carry other time zones (or none), so
        # "newer" can't be trusted here.
        unshipped_cancel = status == "cancelled" and old in (None, "unknown", "ordered", "processing", "exception")
        if status_advances(old, status) and not final and (newer or unshipped_cancel or status in PROGRESS
                                                           or old in (None, "unknown")):
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
        if updates.get("shipping_status") and updates["shipping_status"] != old:
            from . import notify
            notify.order_moved(dict(dict(row), **updates), old, updates["shipping_status"], email.date)

    def repair_cancels(self):
        """Fix orders read before cancel emails were checked by rule: an order whose latest email is a
        cancel is marked cancelled, and a cancel email saved as its own order is folded into the order it
        cancels. Statuses you set by hand are left alone. Returns (marked, merged)."""
        from types import SimpleNamespace
        from .extract import cancel_signal
        from .stuck import when as stuck_when
        subjects = {r["message_id"]: (r["subject"] or "", r["date"] or "") for r in
                    self.db.execute("SELECT message_id, subject, date FROM emails WHERE kind = 'order'")}
        marked = merged = 0
        for row in self.db.execute("SELECT * FROM tracked_orders WHERE COALESCE(status_manual, 0) = 0").fetchall():
            ids = [i for i in json.loads(row["message_ids"] or "[]") if i in subjects]
            if not ids:
                continue
            latest = max(ids, key=lambda i: stuck_when(subjects[i][1]) or stuck_when("1970-01-01"))
            status = row["shipping_status"]
            # Before it ships, any of its emails being a cancel counts (the cancel can be read before or after the
            # confirmation); after that only the latest email says what happened.
            unshipped = (status or "unknown") in ("unknown", "ordered", "processing", "exception")
            signal = "cancelled" if any(
                cancel_signal(SimpleNamespace(subject=subjects[i][0], body="")) == "cancelled"
                for i in (ids if unshipped else [latest])) else None
            if signal == "cancelled" and status != "cancelled":
                self.db.execute("UPDATE tracked_orders SET shipping_status = 'cancelled' WHERE id = ?", (row["id"],))
                marked += 1
                status = "cancelled"
            if status != "cancelled" or len(ids) != 1:
                continue
            # A lone cancel email: does it belong to an order we already have?
            result = {"order_number": row["order_number"], "retailer": row["retailer"],
                      "recipient_email": row["recipient_email"]}
            email = SimpleNamespace(date=subjects[latest][1])
            target = None
            if row["order_number"]:
                for other in self.db.execute("SELECT * FROM tracked_orders WHERE id != ? AND order_number IS NOT NULL",
                                             (row["id"],)):
                    if _key(other["order_number"]) == _key(row["order_number"]):
                        target = other
                        break
            if target is None:
                target = self._order_for_cancel(result, email)
                target = target if target is not None and target["id"] != row["id"] else None
            if target is None:
                continue
            ids = json.loads(target["message_ids"] or "[]") + [i for i in json.loads(row["message_ids"] or "[]")]
            self.db.execute("DELETE FROM tracked_orders WHERE id = ?", (row["id"],))
            self.db.execute("UPDATE tracked_orders SET message_ids = ?, shipping_status = CASE WHEN"
                            " COALESCE(status_manual, 0) = 1 THEN shipping_status ELSE 'cancelled' END,"
                            " last_email_date = MAX(COALESCE(last_email_date, ''), ?) WHERE id = ?",
                            (json.dumps(list(dict.fromkeys(ids))), subjects[latest][1], target["id"]))
            merged += 1
        self.db.commit()
        # Then the bot's decline webhooks and card refunds, which cancel an order with no cancel email too.
        from . import cancels
        marked += cancels.reconcile(self.db)
        return marked, merged

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
             fields.get("estimated_delivery"), "[]", fields.get("account") or "added by hand"))
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
        if "tracking_number" in fields and fields["tracking_number"] != row["tracking_number"]:
            fields["tracking_url"] = None  # a link from the email only fits the old number
        if "total" in fields and fields["total"] != row["total"]:
            fields["total_manual"] = 1  # typed in: the price re-check leaves it alone
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE tracked_orders SET {cols} WHERE id = ?", (*fields.values(), order_id))
            self.db.commit()
        if status and status != row["shipping_status"]:
            label = self.set_status_manually(order_id, status)
            self.set_rescan_state(label, "Set from your phone")
        return True

    def dismiss_order(self, order_id):
        """'Not an order': remove a scraped order and mark its emails as nothing to keep. The emails stay
        recorded as read, so syncs don't pick them up again. Returns them for training."""
        row = self.db.execute("SELECT message_ids FROM tracked_orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            return []
        ids = json.loads(row["message_ids"] or "[]")
        mails = [dict(r) for r in self.db.execute(
            "SELECT message_id, account, sender, subject, date FROM emails WHERE message_id IN (%s)"
            % ",".join("?" * len(ids)), ids)] if ids else []
        self.db.executemany("UPDATE emails SET kind = 'not_an_order' WHERE message_id = ?", [(i,) for i in ids])
        self.db.execute("DELETE FROM tracked_orders WHERE id = ?", (order_id,))
        self.db.commit()
        return mails

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
        was = self.db.execute("SELECT amount FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if "amount" in fields and was is not None and fields["amount"] != was[0]:
            fields = dict(fields, amount_manual=1)  # typed in: the price re-check leaves it alone
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

    def add_price_example(self, email, prompt, raw, corrected):
        """An email whose price was fixed by hand: the answer with the right price."""
        self.db.execute("DELETE FROM training_examples WHERE label_id IS NULL AND message_id = ?", (email.message_id,))
        right = raw.get("total") is not None and abs(float(raw.get("total") or 0) - corrected["total"]) < 0.01
        self.db.execute(
            "INSERT INTO training_examples (label_id, order_id, message_id, sender, subject, email_date,"
            " prompt, model_output, model_status, corrected, status, model_was_right, created_at)"
            " VALUES (NULL,NULL,?,?,?,?,?,?,?,?,'price',?,?)",
            (email.message_id, email.sender, email.subject, email.date, prompt, json.dumps(raw),
             raw.get("shipping_status") or raw.get("kind"), json.dumps(corrected), int(right),
             datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def examples(self, only_corrected=True):
        q = "SELECT * FROM training_examples" + (" WHERE corrected IS NOT NULL" if only_corrected else "")
        return [dict(r) for r in self.db.execute(q + " ORDER BY id DESC")]

    def labels(self):
        """Latest correction per order."""
        return {r["order_id"]: dict(r) for r in self.db.execute("SELECT * FROM labels ORDER BY id")}

    def order(self, order_id):
        row = self.db.execute("SELECT * FROM tracked_orders WHERE id = ?", (order_id,)).fetchone()
        return _order_dict(row) if row else None

    def orders(self):
        rows = self.db.execute(
            "SELECT * FROM tracked_orders ORDER BY COALESCE(order_date, last_email_date) DESC"
        ).fetchall()
        return [_order_dict(r) for r in rows]

    def expenses(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM expenses ORDER BY date DESC")]

    def stats(self, orders=None, expenses=None):
        """Totals for the stat tiles. Pass rows a page already loaded to skip reading them again."""
        orders = self.orders() if orders is None else orders
        by_status = {s: 0 for s in STATUSES}
        for o in orders:
            st = o["shipping_status"] or "unknown"
            by_status[st] = by_status.get(st, 0) + 1
        expenses = self.expenses() if expenses is None else expenses
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
            # Spend counts only orders that stuck: a cancelled, refunded or returned order cost nothing.
            "order_total": sum(o["total"] or 0 for o in orders if o["shipping_status"] not in NOT_SPENT),
            "not_spent": sum(by_status.get(s, 0) for s in NOT_SPENT),
            "in_flight": sum(by_status[s] for s in ("shipped", "in_transit", "out_for_delivery")),
            "by_status": {k: v for k, v in by_status.items() if v},
            "expense_total": sum(by_category.values()),
            "expense_by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
            "expense_by_month": dict(sorted(by_month.items(), reverse=True)),
            "expense_by_vendor": dict(sorted(by_vendor.items(), key=lambda kv: -kv[1])),
        }
