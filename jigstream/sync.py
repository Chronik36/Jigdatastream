"""Pull new emails from each inbox in turn, extract them with the local model, and store the results."""

import logging
import sys
import time
from datetime import datetime, timezone

from . import sales
from .extract import extract, looks_like_purchase
from .llm import LLMError, from_config as llm_for
from .mail import fetch_emails
from .training import few_shot

logger = logging.getLogger("jigstream.sync")


RECHECK_DAYS = 60
ERROR_STREAK = 5


def search_days(config, state):
    """How far back to search one inbox: None means the whole mailbox."""
    if config.full_history:
        return None
    if config.days_back:
        return config.days_back
    if state and state.get("last_synced_at"):
        last = datetime.fromisoformat(state["last_synced_at"])
        # Gmail's newer_than works in whole days; overlap by two so nothing slips through. Every sync also
        # looks back RECHECK_DAYS (60), so mail that was missed or labelled late still gets read and a late
        # cancel (Pokemon Center can cancel a month on) finds its order. Mail already read is skipped cheaply.
        return max(RECHECK_DAYS, (datetime.now(timezone.utc) - last).days + 2)
    return config.first_sync_days


def process(email, store, llm, config, counts, log, examples=(), platforms=()):
    if not looks_like_purchase(email):
        store.record_email(email, "skipped")
        counts["skipped"] += 1
        logger.debug("skipped (no price or tracking): %s | %s", email.sender, email.subject[:80])
        return
    started = time.monotonic()
    try:
        result = extract(llm, email, config.max_body_chars, examples, platforms)
    except LLMError as e:
        if "Could not reach" in str(e):
            raise
        store.record_email(email, "error", str(e)[:500])
        counts["error"] += 1
        counts["streak"] = counts.get("streak", 0) + 1
        log(f"  ! {email.subject[:70]}: {e}", file=sys.stderr)
        if counts["streak"] >= ERROR_STREAK:  # the engine is failing on everything; stop instead of grinding on
            raise LLMError(f"The local model failed on {ERROR_STREAK} emails in a row; stopping this sync. Last: {e}")
        return
    counts.pop("streak", None)
    store.save(email, result)
    counts[result["kind"]] = counts.get(result["kind"], 0) + 1
    logger.info("%s in %.1fs: %s | %s -> %s", result["kind"], time.monotonic() - started, email.sender,
                email.subject[:80], {k: v for k, v in result.items() if k not in ("kind", "items")})
    if result["kind"] != "other":
        label = result.get("retailer") or result.get("vendor") or result.get("platform")
        log(f"  {result['kind']:<16} {label or '?'}: {email.subject[:60]}")


def run_sync(config, store, emails=None, llm=None, log=print, progress=None):
    """Sync every configured inbox one after another. `emails` replaces Gmail in tests.
    progress(text) is called as work advances, for the dashboard."""
    llm = llm or llm_for(config)
    progress = progress or (lambda text: None)
    cli_log = log

    def log(message, file=None):
        # Everything shown on the command line also goes to logs/app.log.
        (logger.warning if file is sys.stderr else logger.info)(message.strip())
        cli_log(message, file=file) if file else cli_log(message)

    logger.info("sync started: %d inbox(es), model %s, %s", len(config.accounts), config.llm_model,
                "whole mailbox" if config.full_history else f"days={config.days_back or 'auto'}")
    counts = {"order": 0, "botting_expense": 0, "other": 0, "skipped": 0, "error": 0, "failed_inboxes": []}
    platforms = sales.platforms(store.db)
    config.search_query = sales.with_terms(config.search_query, sales.custom_platforms(store.db))
    seen = store.seen_ids()
    examples = few_shot(store) if config.use_examples else []
    if examples:
        logger.info("using %d training example(s) in prompts", len(examples))

    if emails is not None:
        for email in emails:
            if email.message_id not in seen:
                process(email, store, llm, config, counts, log, examples, platforms)
        counts.pop("streak", None)
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
                process(email, store, llm, config, counts, log, examples, platforms)
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
    counts.pop("streak", None)
    logger.info("sync finished: %s", summary(counts))
    return counts


def summary(counts):
    """One line for the dashboard and the log."""
    text = (f"{counts['order']} order emails, {counts['botting_expense']} botting receipts, "
            + (f"{counts['sale']} sales, " if counts.get("sale") else "") +
            f"{counts['other']} not relevant, {counts['skipped']} skipped without the model, "
            f"{counts['error']} errors")
    if counts["failed_inboxes"]:
        text += f". Could not sync: {', '.join(counts['failed_inboxes'])}"
    return text + "."
