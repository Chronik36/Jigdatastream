"""The Inbox page: browse your mail inside BotTrack and label emails to teach the model.

Listing only reads headers. Opening or labelling an email downloads it read-only. A labelled
email is read by the model, its answer is corrected to your label, and the pair is saved as a
training example (shown to the model before new emails, and exported for fine-tuning). The label
is also applied, so a "Delivered" email updates its order, an "Expense" email lands on the
Botting expenses page, and a "Sale / payout" email counts toward the Profit goal.
"""
import logging

from .extract import email_prompt, extract_raw, normalize
from .mail import fetch_by_message_id, search_headers
from .training import few_shot

logger = logging.getLogger("jigstream.inbox")

# Label -> what the model should have answered.
LABELS = {
    "ordered": ("Order placed", {"kind": "order", "shipping_status": "ordered"}),
    "in_transit": ("Shipped / In transit", {"kind": "order", "shipping_status": "in_transit"}),
    "out_for_delivery": ("Out for delivery", {"kind": "order", "shipping_status": "out_for_delivery"}),
    "delivered": ("Delivered", {"kind": "order", "shipping_status": "delivered"}),
    "cancelled": ("Cancelled", {"kind": "order", "shipping_status": "cancelled"}),
    "returned": ("Returned / refunded", {"kind": "order", "shipping_status": "returned"}),
    "expense": ("Botting expense", {"kind": "botting_expense"}),
    "sale": ("Sale / payout", {"kind": "sale"}),
    "other": ("Not an order", {"kind": "other"}),
}
QUICK_FILTERS = {
    "Purchases": "(order OR receipt OR invoice OR shipped OR delivered OR cancelled OR subscription)",
    "Shipping": "(shipped OR tracking OR \"in transit\" OR \"out for delivery\" OR delivered)",
    "Cancellations": "(cancelled OR canceled OR cancellation OR refund)",
    "Sales": "(sold OR payout OR \"paid out\" OR \"you made the sale\" OR tradepost OR \"trade post\" OR buylist"
             " OR \"trade-in\" OR \"offer accepted\")",
    "Spam": "in:spam",
    "Everything": "",
    "AI skipped": None,  # read from BotTrack's own records, not a Gmail search
}
SKIPPED = "AI skipped"


def skipped(store, limit=50, offset=0):
    """Emails the AI read and decided were neither an order nor an expense (or that had no purchase
    words at all), newest first, leaving out ones you've already labelled. Returns (rows, next offset)."""
    rows = store.db.execute("SELECT e.message_id, e.account, e.sender, e.subject, e.date, e.kind FROM emails e"
                            " WHERE e.kind IN ('other', 'skipped', 'not_an_order')"
                            " AND NOT EXISTS (SELECT 1 FROM inbox_labels l WHERE l.message_id = e.message_id)"
                            " ORDER BY e.date DESC LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
    return [dict(r, spam=False) for r in rows[:limit]], (offset + limit if len(rows) > limit else None)


def corrected_answer(raw, label):
    """The model's own answer with your label applied; for 'not an order' only the kind remains."""
    target = LABELS[label][1]
    if target["kind"] == "other":
        return {"kind": "other"}
    out = dict(raw)
    out.update(target)
    if target["kind"] in ("botting_expense", "sale"):
        out.pop("shipping_status", None)
    return out


_first_pages = {}  # (query, inboxes, limit) -> (time, result): coming back to Inbox shows it at once
FIRST_PAGE_SECONDS = 45


def search(config, query, limit=50, after=None):
    """One page of header rows from every inbox for a Gmail search, newest first.
    after is the cursor from the previous page: {address: {"f", "u"}} where each inbox left off, or
    "done" once it has nothing more. Returns (rows, errors, next cursor or None at the end)."""
    import time
    key = (query, tuple(a for a, _ in config.accounts), limit)
    if not after:
        hit = _first_pages.get(key)
        if hit and time.monotonic() - hit[0] < FIRST_PAGE_SECONDS:
            return hit[1]
        result = _search(config, query, limit, None)
        if not result[1]:
            _first_pages[key] = (time.monotonic(), result)
        return result
    return _search(config, query, limit, after)


def _search(config, query, limit, after):
    from concurrent.futures import ThreadPoolExecutor
    after = after or {}
    accounts = [a for a in config.accounts if after.get(a[0]) != "done"]
    found, errors = {}, []

    def one(account):
        address, password = account
        try:
            return address, search_headers(config, address, password, query, limit, after.get(address)), None
        except Exception as e:
            logger.warning("inbox search failed for %s: %s", address, e)
            return address, None, f"{address}: {e}"
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(accounts) or 1))) as pool:  # all inboxes at once
        for address, rows, error in pool.map(one, accounts):
            if error:
                errors.append(error)
            else:
                found[address] = rows
    # Merge newest first, taking each inbox's rows in its own order, so every inbox's next page starts
    # right after the last row it gave here.
    taken, pos = [], {a: 0 for a in found}
    while len(taken) < limit:
        live = [a for a in found if pos[a] < len(found[a])]
        if not live:
            break
        a = max(live, key=lambda a: found[a][pos[a]]["date"] or "")
        taken.append(found[a][pos[a]])
        pos[a] += 1
    nxt = {a: c for a, c in after.items() if a not in found}  # failed inboxes retry from where they were
    for a, rows in found.items():
        if pos[a]:
            nxt[a] = {"f": rows[pos[a] - 1]["f"], "u": rows[pos[a] - 1]["uid"]}
        elif after.get(a):
            nxt[a] = after[a]
        if pos[a] == len(rows) and len(rows) < limit:
            nxt[a] = "done"  # it gave everything it had
    more = any(c != "done" for c in nxt.values()) or any(a not in nxt for a, _ in config.accounts)
    return taken, errors, (nxt if more and taken else None)


def open_email(config, account, message_id):
    password = dict(config.accounts).get(account)
    return fetch_by_message_id(config, account, password, message_id) if password else None


def _sale_fix(email, sale):
    """The parts of a hand-typed sale this email shows, as the AI should have read them."""
    from .ordersheet import _shows
    body = f"{email.subject}\n{email.body}"
    out = {"platform": sale["platform"]} if sale.get("platform") else {}
    for k in ("sale_price", "fees", "payout"):
        if sale.get(k) is not None and _shows(body, "total", sale[k]):
            out[k] = sale[k]
    if sale.get("item") and _shows(body, "items", sale["item"]):
        out["items"] = [{"name": sale["item"]}]
    return out


def label_emails(config, store, llm, picks, label, sale=None):
    """picks: [{"account", "message_id", "sender", "subject", "date"}]. Runs in the background.
    sale: a sale you typed for these emails (sales.from_order): {"id", "platform", "item", "sale_price", "fees",
    "payout"}. The AI learns the platform and every number the email shows, and the emails add no second sale."""
    from . import sales
    examples, platforms = few_shot(store), sales.platforms(store.db)
    for row in picks:
        store.set_inbox_label(row, label, "working")
        try:
            email = open_email(config, row["account"], row["message_id"])
            if not email:
                store.set_inbox_label(row, label, "not found in Gmail")
                continue
            prompt = email_prompt(email, config.max_body_chars, platforms)
            raw, _ = extract_raw(llm, email, config.max_body_chars, examples, platforms)
            corrected = corrected_answer(raw, label)
            if sale:
                corrected.update(_sale_fix(email, sale))
            store.add_inbox_example(email, prompt, raw, corrected, label)
            result = normalize(corrected, email)
            store.save(email, result)
            if sale and result["kind"] == "sale":
                sales.keep_one(store.db, sale["id"], email.message_id)
            if result["kind"] != "botting_expense":  # e.g. a receipt the AI wrongly filed as an expense
                store.db.execute("DELETE FROM expenses WHERE message_id = ?", (email.message_id,))
                store.db.commit()
            if result["kind"] != "sale":
                sales.ensure(store.db).execute("DELETE FROM sales WHERE message_id = ?", (email.message_id,))
                store.db.commit()
            was = raw.get("shipping_status") if raw.get("kind") == "order" else raw.get("kind")
            store.set_inbox_label(row, label, f"saved (the AI had said {was or 'nothing'})")
            logger.info("inbox label %s on %s: model said %s", label, email.subject[:80], was)
        except Exception as e:
            logger.exception("inbox label failed for %s", row.get("subject"))
            store.set_inbox_label(row, label, f"failed: {e}")


def fill_missing_amounts(config, store, limit=200, recheck=False):
    """Re-open expenses saved without an amount and read the total straight from the email.
    Reminders and unpaid invoices that were filed as expenses are removed. Each email is tried once,
    unless recheck is set (the Find missing amounts button); one that couldn't be opened is tried again."""
    from .extract import NOT_A_CHARGE, PAID_WORDS, find_total
    rows = store.db.execute("SELECT e.id, e.message_id, e.account, e.vendor, e.description, m.subject FROM expenses e"
                            " LEFT JOIN emails m ON m.message_id = e.message_id"
                            " WHERE e.amount IS NULL AND e.message_id NOT LIKE 'manual:%'"
                            + ("" if recheck else " AND COALESCE(e.amount_checked, 0) = 0") + " LIMIT ?",
                            (limit,)).fetchall()
    filled = removed = 0
    for r in rows:
        subject = r["subject"] or r["description"] or ""
        if NOT_A_CHARGE.search(subject) and not PAID_WORDS.search(subject):
            store.db.execute("DELETE FROM expenses WHERE id = ?", (r["id"],))
            store.db.execute("UPDATE emails SET kind = 'other' WHERE message_id = ?", (r["message_id"],))
            removed += 1
            continue
        try:
            email = open_email(config, r["account"], r["message_id"])
        except Exception as e:
            logger.warning("couldn't re-open the %s expense email: %s", r["vendor"], e)
            continue  # tried again next sync
        amount = find_total(email.body) if email else None
        if not email:
            logger.info("expense %s (%s): email not found in %s", r["id"], r["vendor"], r["account"] or "any inbox")
        elif amount is None:
            logger.info("expense %s (%s): no total found in the email (%d characters)",
                        r["id"], r["vendor"], len(email.body))
        store.db.execute("UPDATE expenses SET amount = COALESCE(?, amount), amount_checked = 1 WHERE id = ?",
                         (amount, r["id"]))
        store.db.commit()  # before opening the next email from Gmail
        store.db.commit()
        filled += amount is not None
    store.db.commit()
    if rows:
        logger.info("missing expense amounts: %d filled, %d reminders removed, %d checked", filled, removed, len(rows))
    return filled, removed
