"""Bank and credit card transactions, matched to your orders and botting receipts.

Transactions come from statement files you download from your bank or card site (CSV, OFX or QFX),
or from Plaid if you linked a bank with your own free Plaid keys (see plaid.py). Either way they land
in one table, are de-duplicated, and each charge is matched to the order or receipt it paid for:
same amount to the cent, a date within a few days, and the store's name in the description when it
can tell two candidates apart. You can match, unmatch or ignore any charge by hand, and a hand
choice is never undone by a later import.
"""
import csv
import hashlib
import io
import logging
import re
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger("jigstream.bank")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_accounts (
    id INTEGER PRIMARY KEY,
    source TEXT, name TEXT, institution TEXT, mask TEXT,
    plaid_item_id TEXT, plaid_account_id TEXT UNIQUE, created_at TEXT
);
CREATE TABLE IF NOT EXISTS bank_transactions (
    id INTEGER PRIMARY KEY,
    uid TEXT UNIQUE,
    account_id INTEGER,
    date TEXT,
    description TEXT,
    amount REAL,
    currency TEXT,
    pending INTEGER DEFAULT 0,
    category TEXT,
    source TEXT,
    imported_at TEXT,
    match_kind TEXT,
    match_id INTEGER,
    match_how TEXT
);
CREATE INDEX IF NOT EXISTS bank_tx_match ON bank_transactions (match_kind, match_id);
"""

# How far a card charge may post from the order or receipt date: a day or two early (pre-auth
# dated before the email) up to a week late (pre-orders, slow posting).
DAYS_BEFORE, DAYS_AFTER = 2, 7
NOISE = {"the", "and", "com", "www", "inc", "llc", "ltd", "store", "shop", "online", "order", "orders", "payment",
         "purchase", "pos", "debit", "credit", "card", "sq", "tst", "paypal", "usa", "us", "co"}

DATE_HEADERS = ["transaction date", "trans. date", "trans date", "date", "posted date", "posting date", "post date"]
DESC_HEADERS = ["description", "merchant", "payee", "name", "merchant name", "details", "transaction description",
                "memo", "original description"]
AMOUNT_HEADERS = ["amount", "transaction amount", "amount (usd)", "billing amount"]
DEBIT_HEADERS = ["debit", "debits", "withdrawal", "withdrawals", "charge", "charges", "debit amount", "money out"]
CREDIT_HEADERS = ["credit", "credits", "deposit", "deposits", "payment", "payments", "credit amount", "money in"]
CATEGORY_HEADERS = ["category", "type"]


class ImportProblem(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _setup(con):
    con.executescript(SCHEMA)
    # When the bank gave the charge a time (Plaid does for many cards): Card check ties a charge to a checkout by it.
    if "time" not in {r[1] for r in con.execute("PRAGMA table_info(bank_transactions)")}:
        con.execute("ALTER TABLE bank_transactions ADD COLUMN time TEXT")
    # A card used only for botting: its charges with no receipt or order still count as botting spend.
    if "all_botting" not in {r[1] for r in con.execute("PRAGMA table_info(bank_accounts)")}:
        con.execute("ALTER TABLE bank_accounts ADD COLUMN all_botting INTEGER")


def ensure(con):
    from . import db
    return db.setup_once(con, __name__, _setup)


# ---------- reading statement files ----------

def parse_date(value):
    s = (value or "").strip()
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)
    if re.fullmatch(r"\d{8}.*", s):  # OFX: 20261015 or 20261015120000[-5:EST]
        try:
            return datetime.strptime(s[:8], "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d", "%m-%d-%Y", "%m-%d-%y", "%d %b %Y", "%b %d, %Y",
                "%d-%b-%Y", "%d-%b-%y", "%B %d, %Y"):
        try:
            return datetime.strptime(s[:20].split("T")[0].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount(value):
    s = (value or "").strip()
    if not s:
        return None
    negative = (s.startswith("(") and s.endswith(")")) or "-" in s
    s = re.sub(r"[^\d.]", "", s)
    if not s or s == ".":
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negative else n


def _find(headers, wanted):
    for w in wanted:
        if w in headers:
            return headers.index(w)
    return None


def parse_csv(text, charges="auto"):
    """A bank or card CSV -> (account name or None, [rows]). Each row has date, description, amount
    (positive = money spent) and category. charges is 'negative', 'positive' or 'auto': how the file
    shows a purchase when it has a single Amount column."""
    lines = text.lstrip("﻿").splitlines()
    # Some banks put a few lines of account details above the real header.
    start = next((i for i, line in enumerate(lines[:30])
                  if _header_ok([h.strip().lower() for h in next(csv.reader([line]), [])])), None)
    if start is None:
        raise ImportProblem("couldn't find the Date / Description / Amount columns in that CSV")
    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    headers = [h.strip().lower() for h in next(reader)]
    di, ni = _find(headers, DATE_HEADERS), _find(headers, DESC_HEADERS)
    ai, dbi, ci = _find(headers, AMOUNT_HEADERS), _find(headers, DEBIT_HEADERS), _find(headers, CREDIT_HEADERS)
    cat = _find(headers, CATEGORY_HEADERS)
    rows = []
    for rec in reader:
        if not any(x.strip() for x in rec):
            continue
        get = lambda i: rec[i] if i is not None and i < len(rec) else ""
        day = parse_date(get(di))
        if not day:
            continue
        if ai is not None and get(ai).strip():
            amount = parse_amount(get(ai))
        else:
            out, into = parse_amount(get(dbi)), parse_amount(get(ci))
            if out is None and into is None:
                continue
            amount = abs(out or 0) - abs(into or 0)  # already "positive = spent"
            rows.append({"date": day, "description": get(ni).strip(), "amount": round(amount, 2),
                         "category": get(cat).strip() or None, "signed": False})
            continue
        if amount is None:
            continue
        rows.append({"date": day, "description": get(ni).strip(), "amount": amount,
                     "category": get(cat).strip() or None, "signed": True})
    signed = [r for r in rows if r.pop("signed")]
    if signed:
        if charges == "auto":
            # On most card exports purchases outnumber payments, so the common sign is a purchase.
            negatives = sum(1 for r in signed if r["amount"] < 0)
            charges = "negative" if negatives > len(signed) / 2 else "positive"
        for r in signed:
            r["amount"] = round(-r["amount"] if charges == "negative" else r["amount"], 2)
    return None, rows


def _header_ok(headers):
    has_amount = _find(headers, AMOUNT_HEADERS) is not None or _find(headers, DEBIT_HEADERS) is not None
    return _find(headers, DATE_HEADERS) is not None and _find(headers, DESC_HEADERS) is not None and has_amount


def _tag(block, name):
    m = re.search(rf"<{name}>\s*([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else ""


def parse_ofx(text):
    """OFX or QFX (Quicken / Money download), both the old SGML kind and the XML kind."""
    acct = _tag(text, "ACCTID")
    org = _tag(text, "ORG")
    rows = []
    for block in re.findall(r"<STMTTRN>(.*?)(?=</STMTTRN>|<STMTTRN>|</BANKTRANLIST>)", text, re.S | re.I):
        day = parse_date(_tag(block, "DTPOSTED") or _tag(block, "DTUSER"))
        amount = parse_amount(_tag(block, "TRNAMT"))
        if not day or amount is None:
            continue
        name, memo = _tag(block, "NAME"), _tag(block, "MEMO")
        desc = name if not memo or memo in name else f"{name} {memo}".strip()
        rows.append({"date": day, "description": _unescape(desc), "amount": round(-amount, 2),  # OFX: debits < 0
                     "category": None, "fitid": _tag(block, "FITID") or None})
    if not rows and "<OFX>" not in text.upper():
        raise ImportProblem("that doesn't look like an OFX or QFX file")
    name = " ".join(x for x in (org, f"…{acct[-4:]}" if acct else "") if x) or None
    return name, rows


def _unescape(s):
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'").replace("&quot;", '"')


def parse_file(filename, data, charges="auto"):
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    if ext in ("ofx", "qfx", "qbo") or "<STMTTRN>" in text.upper()[:200000]:
        return parse_ofx(text)
    return parse_csv(text, charges)


# ---------- storing ----------

def account_for(con, name, source="file", institution=None, mask=None, plaid_item_id=None, plaid_account_id=None):
    ensure(con)
    if plaid_account_id:
        row = con.execute("SELECT id FROM bank_accounts WHERE plaid_account_id = ?", (plaid_account_id,)).fetchone()
        if row:
            con.execute("UPDATE bank_accounts SET name = ?, institution = ?, mask = ? WHERE id = ?",
                        (name, institution, mask, row[0]))
            return row[0]
    else:
        row = con.execute("SELECT id FROM bank_accounts WHERE source = ? AND name = ?", (source, name)).fetchone()
        if row:
            return row[0]
    cur = con.execute("INSERT INTO bank_accounts (source, name, institution, mask, plaid_item_id, plaid_account_id,"
                      " created_at) VALUES (?,?,?,?,?,?,?)",
                      (source, name, institution, mask, plaid_item_id, plaid_account_id, _now()))
    return cur.lastrowid


def import_rows(con, account_id, rows, source):
    """Add rows to an account, skipping ones already imported. Returns (added, skipped)."""
    ensure(con)
    seen, added, skipped = {}, 0, 0
    for r in rows:
        if r.get("uid"):
            uid = r["uid"]
        elif r.get("fitid"):
            uid = f"ofx:{account_id}:{r['fitid']}"
        else:
            # The same charge twice on one day (two $200 orders) is two rows: count repeats in the file.
            desc = re.sub(r"\s+", " ", r["description"].lower())
            base = f"{account_id}|{r['date']}|{r['amount']:.2f}|{desc}"
            seen[base] = seen.get(base, 0) + 1
            uid = "csv:" + hashlib.sha256(f"{base}|{seen[base]}".encode()).hexdigest()[:32]
        cur = con.execute(
            "INSERT OR IGNORE INTO bank_transactions (uid, account_id, date, description, amount, currency, pending,"
            " category, source, imported_at, time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, account_id, r["date"], r["description"], r["amount"], r.get("currency") or "USD",
             int(bool(r.get("pending"))), r.get("category"), source, _now(), r.get("time")))
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
            if r.get("uid"):  # Plaid sends changed transactions again: keep the newest details
                con.execute("UPDATE bank_transactions SET date = ?, description = ?, amount = ?, pending = ?,"
                            " category = ?, time = COALESCE(?, time) WHERE uid = ?",
                            (r["date"], r["description"], r["amount"], int(bool(r.get("pending"))),
                             r.get("category"), r.get("time"), uid))
    con.commit()
    return added, skipped


def import_file(con, filename, data, account_name=None, charges="auto"):
    """One uploaded statement file -> (account name, added, skipped, matched)."""
    found_name, rows = parse_file(filename, data, charges)
    name = (account_name or "").strip() or found_name or re.sub(r"\.[^.]+$", "", filename or "Statement")
    if not rows:
        raise ImportProblem("no transactions found in that file")
    account_id = account_for(con, name)
    added, skipped = import_rows(con, account_id, rows, f"file:{filename}")
    matched = auto_match(con)
    logger.info("imported %s into %s: %d new, %d already there, %d matched", filename, name, added, skipped, matched)
    return name, added, skipped, matched


# ---------- matching ----------

def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in NOISE}


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


@lru_cache(maxsize=100000)  # pure; Stuck and Card check ask it for every refund and checkout pair
def names_agree(description, store_name):
    """'NIKE.COM 8005551234 OR' vs 'Nike' -> True. Bank descriptions are cut short and mashed together,
    so a store word appearing anywhere in the squashed description counts."""
    squashed = _squash(description)
    words = _words(store_name)
    return bool(words) and any(w in squashed for w in words)


def _day(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def candidates(con, tx, include_claimed=False):
    """Orders and receipts this charge could be, best first: [(kind, row, name_agrees, days)]."""
    ensure(con)
    when = _day(tx["date"])
    if not when or tx["amount"] is None:
        return []
    claimed = set() if include_claimed else {
        (r[0], r[1]) for r in con.execute(
            "SELECT match_kind, match_id FROM bank_transactions WHERE match_kind IS NOT NULL AND id != ?", (tx["id"],))}
    lo, hi = (when - timedelta(days=DAYS_AFTER)).isoformat(), (when + timedelta(days=DAYS_BEFORE)).isoformat()
    amount = round(abs(tx["amount"]), 2)
    found = []
    for kind, sql, name_col in (
            ("order", "SELECT id, retailer, order_number, COALESCE(order_date, substr(last_email_date, 1, 10)) AS day,"
                      " total AS amount FROM tracked_orders WHERE ROUND(total, 2) = ? AND"
                      " COALESCE(order_date, substr(last_email_date, 1, 10)) BETWEEN ? AND ?", "retailer"),
            ("expense", "SELECT id, vendor, description, date AS day, amount FROM expenses"
                        " WHERE ROUND(amount, 2) = ? AND date BETWEEN ? AND ?", "vendor")):
        for row in con.execute(sql, (amount, lo, hi)):
            if (kind, row["id"]) in claimed:
                continue
            days = abs((when - _day(row["day"])).days) if _day(row["day"]) else 99
            found.append((kind, dict(row), names_agree(tx["description"], row[name_col]), days))
    found.sort(key=lambda c: (not c[2], c[3]))
    return found


def fill_amounts_from_charges(con):
    """A receipt email with no total BotTrack could read takes its amount from the card charge it was paid with:
    the one unmatched charge whose description names the vendor, dated from DAYS_BEFORE days before the email to
    DAYS_AFTER days after. The charge is matched to it. Returns how many were filled."""
    ensure(con)
    charges = [dict(t) for t in con.execute(
        "SELECT * FROM bank_transactions WHERE match_kind IS NULL AND COALESCE(match_how, '') != 'ignored'"
        " AND amount > 0 AND pending = 0")]
    taken, filled = set(), 0
    for e in con.execute("SELECT id, vendor, date FROM expenses WHERE amount IS NULL AND date IS NOT NULL"
                         " ORDER BY date").fetchall():
        when = _day(e["date"])
        if not when or not e["vendor"]:
            continue
        found = sorted(((abs((_day(t["date"]) - when).days), t) for t in charges
                        if t["id"] not in taken and _day(t["date"])
                        and -DAYS_BEFORE <= (_day(t["date"]) - when).days <= DAYS_AFTER
                        and names_agree(t["description"], e["vendor"])), key=lambda f: f[0])
        if not found or (len(found) > 1 and found[0][0] == found[1][0]
                         and round(found[0][1]["amount"], 2) != round(found[1][1]["amount"], 2)):
            continue  # nothing, or two different amounts equally likely: leave it for you
        t = found[0][1]
        con.execute("UPDATE expenses SET amount = ?, currency = COALESCE(currency, ?) WHERE id = ?",
                    (t["amount"], t["currency"] or "USD", e["id"]))
        con.execute("UPDATE bank_transactions SET match_kind = 'expense', match_id = ?, match_how = 'amount from charge'"
                    " WHERE id = ?", (e["id"], t["id"]))
        taken.add(t["id"])
        filled += 1
    con.commit()
    return filled


REFUND_DAYS = 90
PAYMENT = re.compile(r"payment|autopay|thank you|transfer|interest|cash ?back|reward|statement credit", re.I)


def match_expense_refunds(con):
    """Money back for a botting receipt (a returned server, a refunded plan): a credit on your statement naming
    the vendor, up to REFUND_DAYS after the receipt and no more than it cost, is tied to it and taken off what it
    cost. Returns how many were tied."""
    from . import ledger
    ledger.ensure(con)
    expenses = [dict(e) for e in con.execute("SELECT id, vendor, amount, date FROM expenses WHERE amount > 0"
                                             " AND refunded IS NULL AND date IS NOT NULL ORDER BY date")]
    tied = 0
    for t in con.execute("SELECT * FROM bank_transactions WHERE amount < 0 AND match_kind IS NULL"
                         " AND COALESCE(match_how, '') != 'ignored' ORDER BY date").fetchall():
        when, back = _day(t["date"]), round(-t["amount"], 2)
        if not when or PAYMENT.search(t["description"] or ""):
            continue
        fits = [e for e in expenses if e["vendor"] and names_agree(t["description"], e["vendor"]) and _day(e["date"])
                and 0 <= (when - _day(e["date"])).days <= REFUND_DAYS and back <= round(e["amount"], 2) + 0.01]
        if not fits:
            continue
        e = min(fits, key=lambda e: (abs(e["amount"] - back), (when - _day(e["date"])).days))
        con.execute("UPDATE expenses SET refunded = ? WHERE id = ?", (back, e["id"]))
        con.execute("UPDATE bank_transactions SET match_kind = 'expense_refund', match_id = ?, match_how = 'auto'"
                    " WHERE id = ?", (e["id"], t["id"]))
        expenses.remove(e)
        tied += 1
    con.commit()
    return tied


def botting_card_spend(con, year=None):
    """Charges on cards you use only for botting that no receipt or order explains, minus refunds on them
    (card payments and rewards aside): botting spend the emails don't show."""
    ensure(con)
    where = "a.all_botting = 1 AND t.match_kind IS NULL AND COALESCE(t.match_how, '') != 'ignored' AND t.pending = 0"
    args = ()
    if year:
        where += " AND substr(t.date, 1, 4) = ?"
        args = (str(year),)
    rows = con.execute(f"SELECT t.* FROM bank_transactions t JOIN bank_accounts a ON a.id = t.account_id WHERE {where}",
                       args).fetchall()
    from . import ledger
    if not rows:
        return 0
    close = ledger.close_matches(ledger.ensure(con))  # a few dollars off a receipt: that receipt already counts it
    return round(sum(t["amount"] for t in rows if t["id"] not in close
                     and (t["amount"] > 0 or not PAYMENT.search(t["description"] or ""))), 2)


def auto_match(con):
    """Match every charge nobody has decided on yet. A match needs one clear winner: the store name
    agreeing, or the only order/receipt of that exact amount near that date. Receipts with no amount take
    theirs from their charge first (fill_amounts_from_charges). Returns how many matched."""
    ensure(con)
    fill_amounts_from_charges(con)
    match_expense_refunds(con)
    rows = con.execute("SELECT * FROM bank_transactions WHERE match_how IS NULL AND amount > 0 AND pending = 0"
                       " ORDER BY date").fetchall()
    matched = 0
    for tx in rows:
        found = candidates(con, tx)
        if not found:
            continue
        best = found[0]
        named = [c for c in found if c[2]]
        pool = named or found
        # Two receipts alike in every way (two identical proxy plans the same day) are interchangeable.
        alike = len(pool) > 1 and _same(pool[0], pool[1])
        clear = len(pool) == 1 or pool[0][3] < pool[1][3] or alike
        if not named and len(found) > 1 and not alike:
            clear = False
        if not clear:
            continue
        con.execute("UPDATE bank_transactions SET match_kind = ?, match_id = ?, match_how = 'auto' WHERE id = ?",
                    (best[0], best[1]["id"], tx["id"]))
        matched += 1
    con.commit()
    return matched


def _same(a, b):
    name = lambda c: _squash(c[1].get("retailer") or c[1].get("vendor"))
    return a[0] == b[0] and name(a) == name(b) and a[1]["day"] == b[1]["day"]


def set_match(con, tx_id, kind=None, match_id=None, how="manual"):
    """Your choice for one charge: match it (kind + id), unmatch it (no kind), or ignore it ('ignored')."""
    ensure(con)
    if kind not in (None, "order", "expense"):
        return
    if kind:  # one charge per order or receipt
        con.execute("UPDATE bank_transactions SET match_kind = NULL, match_id = NULL, match_how = 'manual'"
                    " WHERE match_kind = ? AND match_id = ? AND id != ?", (kind, match_id, tx_id))
    con.execute("UPDATE bank_transactions SET match_kind = ?, match_id = ?, match_how = ? WHERE id = ?",
                (kind, match_id if kind else None, how, tx_id))
    con.commit()


def reset_match(con, tx_id):
    """Forget your choice so the automatic matching decides again."""
    ensure(con)
    con.execute("UPDATE bank_transactions SET match_kind = NULL, match_id = NULL, match_how = NULL WHERE id = ?",
                (tx_id,))
    con.commit()
    auto_match(con)


def add_as_expense(con, tx_id, vendor, category):
    """A card charge with no email receipt (a proxy plan paid on a site that sends none) becomes a botting
    expense, matched to the charge."""
    ensure(con)
    tx = con.execute("SELECT * FROM bank_transactions WHERE id = ?", (tx_id,)).fetchone()
    if not tx:
        return None
    cur = con.execute(
        "INSERT OR REPLACE INTO expenses (message_id, vendor, category, description, amount, currency, date,"
        " order_number, billing_period, account) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"bank:{tx['uid']}", vendor or tx["description"], category, tx["description"], tx["amount"],
         tx["currency"] or "USD", tx["date"], None, None, "bank statement"))
    set_match(con, tx_id, "expense", cur.lastrowid)
    return cur.lastrowid


def delete_account(con, account_id):
    ensure(con)
    con.execute("DELETE FROM expenses WHERE message_id IN (SELECT 'bank:' || uid FROM bank_transactions"
                " WHERE account_id = ?)", (account_id,))
    con.execute("DELETE FROM bank_transactions WHERE account_id = ?", (account_id,))
    con.execute("DELETE FROM bank_accounts WHERE id = ?", (account_id,))
    con.commit()


# ---------- reading back ----------

def accounts(con):
    ensure(con)
    return [dict(r) for r in con.execute(
        "SELECT a.*, COUNT(t.id) AS count, MIN(t.date) AS first, MAX(t.date) AS last"
        " FROM bank_accounts a LEFT JOIN bank_transactions t ON t.account_id = a.id GROUP BY a.id ORDER BY a.name")]


def transactions(con, show="all", account_id=None, limit=2000):
    """Charges with what each matched. show: all, matched, unmatched, ignored, refunds."""
    ensure(con)
    where, args = [], []
    if account_id:
        where.append("t.account_id = ?")
        args.append(account_id)
    where.append({"matched": "t.match_kind IS NOT NULL",
                  "orders": "t.match_kind = 'order'", "receipts": "t.match_kind = 'expense'",
                  "unmatched": "t.match_kind IS NULL AND COALESCE(t.match_how, '') != 'ignored' AND t.amount > 0",
                  "ignored": "t.match_how = 'ignored'",
                  "refunds": "t.amount < 0"}.get(show, "1"))
    q = ("SELECT t.*, a.name AS account_name,"
         " o.retailer AS order_retailer, o.order_number AS order_number, o.shipping_status AS order_status,"
         " e.vendor AS expense_vendor, e.category AS expense_category, e.description AS expense_description"
         " FROM bank_transactions t LEFT JOIN bank_accounts a ON a.id = t.account_id"
         " LEFT JOIN tracked_orders o ON t.match_kind = 'order' AND o.id = t.match_id"
         " LEFT JOIN expenses e ON t.match_kind = 'expense' AND e.id = t.match_id"
         f" WHERE {' AND '.join(where)} ORDER BY t.date DESC, t.id DESC LIMIT ?")
    return [dict(r) for r in con.execute(q, (*args, limit))]


def stats(con):
    ensure(con)
    r = con.execute(
        "SELECT COUNT(*) n, SUM(amount > 0) charges,"
        " SUM(CASE WHEN amount > 0 THEN amount END) spent,"
        " SUM(match_kind = 'order') orders, SUM(match_kind = 'expense') expenses,"
        " SUM(match_kind IS NULL AND COALESCE(match_how, '') != 'ignored' AND amount > 0) unmatched,"
        " SUM(CASE WHEN match_kind IS NOT NULL THEN amount END) matched_amount"
        " FROM bank_transactions").fetchone()
    return {k: (r[k] or 0) for k in r.keys()}


def search_targets(con, text, limit=40):
    """Orders and receipts for the manual Match box, by store, order number or amount."""
    ensure(con)
    like = f"%{(text or '').strip()}%"
    amount = parse_amount(text or "")
    out = []
    for r in con.execute(
            "SELECT id, retailer AS name, order_number AS ref, order_date AS day, total AS amount FROM tracked_orders"
            " WHERE retailer LIKE ? OR order_number LIKE ? OR ROUND(total, 2) = ? ORDER BY order_date DESC LIMIT ?",
            (like, like, round(abs(amount), 2) if amount is not None else -1, limit)):
        out.append({"kind": "order", **dict(r)})
    for r in con.execute(
            "SELECT id, vendor AS name, description AS ref, date AS day, amount FROM expenses"
            " WHERE vendor LIKE ? OR description LIKE ? OR ROUND(amount, 2) = ? ORDER BY date DESC LIMIT ?",
            (like, like, round(abs(amount), 2) if amount is not None else -1, limit)):
        out.append({"kind": "expense", **dict(r)})
    return out
