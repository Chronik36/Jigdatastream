"""Pull new emails from each inbox in turn, extract them with the local model, and store the results."""

import sys
from datetime import datetime, timezone

from .extract import extract, looks_like_purchase
from .llm import LLMClient, LLMError
from .mail import fetch_emails


def search_days(config, state):
    """How far back to search one inbox: None means the whole mailbox."""
    if config.full_history:
        return None
    if config.days_back:
        return config.days_back
    if state and state.get("last_synced_at"):
        last = datetime.fromisoformat(state["last_synced_at"])
        # Gmail's newer_than works in whole days; overlap by two so nothing slips through.
        return max(1, (datetime.now(timezone.utc) - last).days + 2)
    return config.first_sync_days


def process(email, store, llm, config, counts, log):
    if not looks_like_purchase(email):
        store.record_email(email, "skipped")
        counts["skipped"] += 1
        return
    try:
        result = extract(llm, email, config.max_body_chars)
    except LLMError as e:
        if "Could not reach" in str(e):
            raise
        store.record_email(email, "error", str(e)[:500])
        counts["error"] += 1
        log(f"  ! {email.subject[:70]}: {e}", file=sys.stderr)
        return
    store.save(email, result)
    counts[result["kind"]] += 1
    if result["kind"] != "other":
        label = result.get("retailer") or result.get("vendor")
        log(f"  {result['kind']:<16} {label or '?'}: {email.subject[:60]}")


def run_sync(config, store, emails=None, llm=None, log=print, progress=None):
    """Sync every configured inbox one after another. `emails` replaces Gmail in tests.
    progress(text) is called as work advances, for the dashboard."""
    llm = llm or LLMClient(config.llm_base_url, config.llm_model, config.llm_api_key, config.llm_timeout)
    progress = progress or (lambda text: None)
    counts = {"order": 0, "botting_expense": 0, "other": 0, "skipped": 0, "error": 0, "failed_inboxes": []}
    seen = store.seen_ids()

    if emails is not None:
        for email in emails:
            if email.message_id not in seen:
                process(email, store, llm, config, counts, log)
        return counts

    if not config.accounts:
        raise RuntimeError("No inboxes configured. Set GMAIL_ACCOUNTS or GMAIL_ADDRESS in .env (see README).")

    for n, (address, password) in enumerate(config.accounts, 1):
        started = datetime.now(timezone.utc).isoformat()
        days = search_days(config, store.account_state(address))
        span = "whole mailbox" if days is None else f"last {days} days"
        log(f"[{n}/{len(config.accounts)}] {address}: {span}")
        total = {"n": 0}

        def on_total(count, address=address):
            total["n"] = count
            log(f"  {count} new emails to check")

        try:
            for i, email in enumerate(fetch_emails(config, address, password, days, seen, on_total), 1):
                progress(f"Inbox {n} of {len(config.accounts)} ({address}): email {i} of {total['n']}")
                if email.message_id in seen:
                    continue
                process(email, store, llm, config, counts, log)
                seen.add(email.message_id)
        except LLMError:
            raise
        except Exception as e:  # a bad password or network drop on one inbox shouldn't stop the others
            counts["failed_inboxes"].append(address)
            log(f"  ! {address}: {e}", file=sys.stderr)
            continue
        # A capped run (MAX_EMAILS) hasn't finished the range, so don't record it as synced.
        if not (config.max_emails and total["n"] >= config.max_emails):
            store.mark_synced(address, started, full=days is None)
    return counts


def summary(counts):
    text = (f"{counts['order']} order emails, {counts['botting_expense']} botting receipts, "
            f"{counts['other']} not relevant, {counts['skipped']} skipped without the model, "
            f"{counts['error']} errors")
    if counts["failed_inboxes"]:
        text += f". Could not sync: {', '.join(counts['failed_inboxes'])}"
    return text + "."
