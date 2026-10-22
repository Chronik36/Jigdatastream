"""Training mode: status corrections from the Orders page become examples for the model.

When you set an order's status by hand, every inbox (All Mail and Spam) is searched for
that order's emails by order and tracking number. Each one is read again by the model,
and kept with the model's answer. Emails that are about the status you chose (a
cancellation email for "cancelled", and so on) also get the corrected answer. The most
useful corrections are shown to the model as examples before each new email, and all of
them can be exported as JSONL to fine-tune a model later.

Unlike normal syncs, this keeps the text of those emails in the local database, because
an example needs the email it came from.
"""

import copy
import json
import logging
import re

from .extract import SYSTEM_PROMPT, email_prompt, extract_raw
from .mail import fetch_emails

logger = logging.getLogger("jigstream.training")

# Wording that marks an email as being about each status.
STATUS_WORDS = {
    "ordered": r"order (is )?(confirm|received|placed)|thanks? (you )?for (your )?(order|purchase)|order summary",
    "processing": r"processing|preparing your order",
    "shipped": r"shipped|on (its|the) way|has been sent|dispatched|tracking (number|info)",
    "in_transit": r"in transit|on (its|the) way|shipped|has been sent|dispatched|tracking (number|info)",
    "out_for_delivery": r"out for delivery",
    "delivered": r"\bdelivered\b|has arrived|was delivered",
    "cancelled": r"cancel+ed|cancel+ation|has been cancel",
    "returned": r"return (received|processed)|returned",
    "refunded": r"refund",
}
EXAMPLE_CHARS = 2500  # keep prompt examples short; local models have limited room


def about_status(email, status):
    pattern = STATUS_WORDS.get(status)
    return bool(pattern and re.search(pattern, f"{email.subject}\n{email.body[:3000]}", re.I))


def order_query(order):
    terms = [t for t in (order.get("order_number"), order.get("tracking_number")) if t]
    return " OR ".join(f'"{t}"' for t in terms)


def few_shot(store, n=2):
    """Up to n corrected examples for the prompt: ones the model got wrong first, one per status."""
    picked, statuses = [], set()
    for ex in sorted(store.examples(), key=lambda e: (e["model_was_right"] or 0, -e["id"])):
        if ex["status"] in statuses:
            continue
        statuses.add(ex["status"])
        picked.append((ex["prompt"][:EXAMPLE_CHARS], ex["corrected"]))
        if len(picked) == n:
            break
    return picked


def rescan(config, store, llm, label_id, order, max_per_inbox=25):
    """Find and re-read the emails for one corrected order. Returns a one-line result."""
    status = store.db.execute("SELECT status FROM labels WHERE id = ?", (label_id,)).fetchone()["status"]
    query = order_query(order)
    if not query:
        text = "No order or tracking number to search for, so only the status was saved."
        store.set_rescan_state(label_id, text)
        return text
    cfg = copy.copy(config)
    cfg.search_query = f"({query}) -in:trash"
    cfg.max_emails = max_per_inbox
    examples = few_shot(store)
    found = matched = 0
    for address, password in config.accounts:
        try:
            for email in fetch_emails(cfg, address, password, days=None):
                prompt = email_prompt(email, config.max_body_chars)
                try:
                    raw, result = extract_raw(llm, email, config.max_body_chars, examples)
                except Exception as e:  # one unreadable email shouldn't stop the rest
                    logger.warning("rescan: model failed on %s: %s", email.subject[:80], e)
                    continue
                store.save(email, result)
                is_match = about_status(email, status)
                store.add_example(label_id, order["id"], email, prompt, raw, status, is_match)
                found += 1
                matched += is_match
                logger.info("rescan label %s: %s | model said %s, about %s: %s", label_id, email.subject[:80],
                            raw.get("shipping_status"), status, is_match)
        except Exception as e:
            logger.warning("rescan: %s failed: %s", address, e)
    if not found:
        text = f"No emails found for {query}."
    elif matched:
        text = f"Found {found} email(s); {matched} about \"{status}\" saved as training examples."
    else:
        text = f"Found {found} email(s), but none look like a \"{status}\" email; kept them for reference."
    store.set_rescan_state(label_id, text)
    logger.info("rescan label %s done: %s", label_id, text)
    return text


def export_jsonl(store):
    """Corrected examples in the chat fine-tuning format most tools accept."""
    lines = []
    for ex in store.examples():
        lines.append(json.dumps({"messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": ex["prompt"]},
            {"role": "assistant", "content": ex["corrected"]},
        ]}))
    return "\n".join(lines) + ("\n" if lines else "")
