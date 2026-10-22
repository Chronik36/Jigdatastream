"""The Inbox page: browse your mail inside BotTrack and label emails to teach the model.

Listing only reads headers. Opening or labelling an email downloads it read-only. A labelled
email is read by the model, its answer is corrected to your label, and the pair is saved as a
training example (shown to the model before new emails, and exported for fine-tuning). The label
is also applied, so a "Delivered" email updates its order and an "Expense" email lands on the
Botting expenses page.
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
    "other": ("Not an order", {"kind": "other"}),
}
QUICK_FILTERS = {
    "Purchases": "(order OR receipt OR invoice OR shipped OR delivered OR cancelled OR subscription)",
    "Shipping": "(shipped OR tracking OR \"in transit\" OR \"out for delivery\" OR delivered)",
    "Cancellations": "(cancelled OR canceled OR cancellation OR refund)",
    "Spam": "in:spam",
    "Everything": "",
}


def corrected_answer(raw, label):
    """The model's own answer with your label applied; for 'not an order' only the kind remains."""
    target = LABELS[label][1]
    if target["kind"] == "other":
        return {"kind": "other"}
    out = dict(raw)
    out.update(target)
    if target["kind"] == "botting_expense":
        out.pop("shipping_status", None)
    return out


def search(config, query, limit=50):
    """Header rows from every inbox for a Gmail search, newest first."""
    rows, errors = [], []
    for address, password in config.accounts:
        try:
            rows += search_headers(config, address, password, query, limit)
        except Exception as e:
            logger.warning("inbox search failed for %s: %s", address, e)
            errors.append(f"{address}: {e}")
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows[:limit], errors


def open_email(config, account, message_id):
    password = dict(config.accounts).get(account)
    return fetch_by_message_id(config, account, password, message_id) if password else None


def label_emails(config, store, llm, picks, label):
    """picks: [{"account", "message_id", "sender", "subject", "date"}]. Runs in the background."""
    examples = few_shot(store)
    for row in picks:
        store.set_inbox_label(row, label, "working")
        try:
            email = open_email(config, row["account"], row["message_id"])
            if not email:
                store.set_inbox_label(row, label, "not found in Gmail")
                continue
            prompt = email_prompt(email, config.max_body_chars)
            raw, _ = extract_raw(llm, email, config.max_body_chars, examples)
            corrected = corrected_answer(raw, label)
            store.add_inbox_example(email, prompt, raw, corrected, label)
            result = normalize(corrected, email)
            store.save(email, result)
            if result["kind"] != "botting_expense":  # e.g. a receipt the AI wrongly filed as an expense
                store.db.execute("DELETE FROM expenses WHERE message_id = ?", (email.message_id,))
                store.db.commit()
            was = raw.get("shipping_status") if raw.get("kind") == "order" else raw.get("kind")
            store.set_inbox_label(row, label, f"saved (the AI had said {was or 'nothing'})")
            logger.info("inbox label %s on %s: model said %s", label, email.subject[:80], was)
        except Exception as e:
            logger.exception("inbox label failed for %s", row.get("subject"))
            store.set_inbox_label(row, label, f"failed: {e}")
