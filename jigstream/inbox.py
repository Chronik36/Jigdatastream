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
    "Sales": "(sold OR payout OR \"paid out\" OR \"you made the sale\")",
    "Spam": "in:spam",
    "Everything": "",
    "AI skipped": None,  # read from BotTrack's own records, not a Gmail search
}
SKIPPED = "AI skipped"


def skipped(store, limit=200):
    """Emails the AI read and decided were neither an order nor an expense (or that had no purchase
    words at all), newest first, leaving out ones you've already labelled."""
    mine = store.inbox_labels()
    rows = store.db.execute("SELECT message_id, account, sender, subject, date, kind FROM emails"
                            " WHERE kind IN ('other', 'skipped', 'not_an_order') ORDER BY date DESC LIMIT ?",
                            (limit * 2,)).fetchall()
    return [dict(r, spam=False) for r in rows if r["message_id"] not in mine][:limit]


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


def search(config, query, limit=50):
    """Header rows from every inbox for a Gmail search, newest first."""
    from concurrent.futures import ThreadPoolExecutor
    rows, errors = [], []

    def one(account):
        address, password = account
        try:
            return search_headers(config, address, password, query, limit), None
        except Exception as e:
            logger.warning("inbox search failed for %s: %s", address, e)
            return [], f"{address}: {e}"
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(config.accounts)))) as pool:  # all inboxes at once
        for found, error in pool.map(one, config.accounts):
            rows += found
            errors += [error] if error else []
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows[:limit], errors


def open_email(config, account, message_id):
    password = dict(config.accounts).get(account)
    return fetch_by_message_id(config, account, password, message_id) if password else None


def label_emails(config, store, llm, picks, label):
    """picks: [{"account", "message_id", "sender", "subject", "date"}]. Runs in the background."""
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
            store.add_inbox_example(email, prompt, raw, corrected, label)
            result = normalize(corrected, email)
            store.save(email, result)
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


def fill_missing_amounts(config, store, limit=200):
    """Re-open expenses saved without an amount and read the total straight from the email.
    Reminders and unpaid invoices that were filed as expenses are removed. Each email is tried once."""
    from .extract import NOT_A_CHARGE, PAID_WORDS, find_total
    rows = store.db.execute("SELECT e.id, e.message_id, e.account, m.subject FROM expenses e"
                            " LEFT JOIN emails m ON m.message_id = e.message_id"
                            " WHERE e.amount IS NULL AND COALESCE(e.amount_checked, 0) = 0 LIMIT ?", (limit,)).fetchall()
    filled = removed = 0
    for r in rows:
        subject = r["subject"] or ""
        if NOT_A_CHARGE.search(subject) and not PAID_WORDS.search(subject):
            store.db.execute("DELETE FROM expenses WHERE id = ?", (r["id"],))
            store.db.execute("UPDATE emails SET kind = 'other' WHERE message_id = ?", (r["message_id"],))
            removed += 1
            continue
        amount = None
        try:
            email = open_email(config, r["account"], r["message_id"])
            amount = find_total(email.body) if email else None
        except Exception as e:
            logger.warning("couldn't re-open expense email %s: %s", r["message_id"], e)
        store.db.execute("UPDATE expenses SET amount = COALESCE(?, amount), amount_checked = 1 WHERE id = ?",
                         (amount, r["id"]))
        filled += amount is not None
    store.db.commit()
    if rows:
        logger.info("missing expense amounts: %d filled, %d reminders removed, %d checked", filled, removed, len(rows))
    return filled, removed
