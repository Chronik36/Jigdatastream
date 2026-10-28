"""Pull new emails from each inbox in turn, extract them with the local model, and store the results."""

import logging
import re
import sys
import time
from datetime import datetime, timezone

from . import sales
from .extract import MONEY, extract, looks_like_purchase, platforms_named
from .llm import LLMError, from_config as llm_for
from .mail import fetch_emails, newest_emails
from . import packs
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


QUIET_AFTER = 3  # a sender whose last emails were all "not relevant" this many times stops going to the model
# Subjects that always go to the model, whoever sends them.
TRANSACTIONAL = re.compile(r"\b(order|receipt|invoice|payment|paid|purchase|confirm|confirmation|ship|shipped|shipping|"
                           r"deliver|delivered|tracking|cancel|cancell?ed|refund|return|renew|renewal|subscription|"
                           r"charge|charged|billing|bill|sold|payout|sale)\b", re.I)
ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def sender_address(sender):
    m = ADDRESS.search(sender or "")
    return m.group(0).lower() if m else (sender or "").strip().lower()


def quiet_senders(db):
    """Senders the model has only ever called "not relevant" (newsletters, sale ads), QUIET_AFTER times or more.
    Their next ads skip the model; anything with order, receipt, shipping, cancel or payment wording still goes."""
    useful, other = set(), {}
    for r in db.execute("SELECT sender, kind FROM emails WHERE kind IN ('other', 'order', 'botting_expense', 'sale')"):
        a = sender_address(r["sender"])
        if r["kind"] == "other":
            other[a] = other.get(a, 0) + 1
        else:
            useful.add(a)
    return {a for a, n in other.items() if n >= QUIET_AFTER and a not in useful}


def process(email, store, llm, config, counts, log, examples=(), platforms=(), quiet=()):
    seller = bool(platforms_named(email, platforms))  # a selling app's payout or trade-in reads differently
    if seller and MONEY.search(f"{email.subject}\n{email.body[:20000]}"):
        quiet = ()
    elif not looks_like_purchase(email):
        store.record_email(email, "skipped")
        counts["skipped"] += 1
        logger.debug("skipped (no price or tracking): %s | %s", email.sender, email.subject[:80])
        return
    if quiet and sender_address(email.sender) in quiet and not TRANSACTIONAL.search(email.subject or ""):
        store.record_email(email, "skipped")
        counts["skipped"] += 1
        logger.debug("skipped (sender only sends ads): %s | %s", email.sender, email.subject[:80])
        return
    started = time.monotonic()
    try:
        shown = examples(email) if callable(examples) else examples  # the examples closest to this email
        result = extract(llm, email, config.max_body_chars, shown, platforms)
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


def run_sync(config, store, emails=None, llm=None, log=print, progress=None, stop=None):
    """Sync every configured inbox one after another. `emails` replaces Gmail in tests.
    progress(text) is called as work advances, for the dashboard. stop() returning True ends the sync
    after the email being read; every email read so far is kept, so the next sync carries on from there."""
    llm = llm or llm_for(config)
    progress = progress or (lambda text: None)
    stop = stop or (lambda: False)
    cli_log = log

    def log(message, file=None):
        # Everything shown on the command line also goes to logs/app.log.
        (logger.warning if file is sys.stderr else logger.info)(message.strip())
        cli_log(message, file=file) if file else cli_log(message)

    logger.info("sync started: %d inbox(es), model %s, %s", len(config.accounts), config.llm_model,
                "whole mailbox" if config.full_history else f"days={config.days_back or 'auto'}")
    counts = {"order": 0, "botting_expense": 0, "other": 0, "skipped": 0, "error": 0, "failed_inboxes": []}
    platforms = sales.platforms(store.db)
    moved = sales.move_misread(store.db, platforms)  # "you sold it" emails saved as orders before
    if moved:
        log(f"  {len(moved)} order(s) were really sales; moved to Sales")
    config.search_query = sales.with_terms(config.search_query, sales.custom_platforms(store.db))
    seen = store.seen_ids()
    examples = packs.picker(store, few_shot(store)) if config.use_examples else []
    quiet = quiet_senders(store.db)
    if quiet:
        logger.info("%d sender(s) only send ads; their ads skip the model", len(quiet))
    if examples:
        logger.info("%d training example(s) to pick from (yours and shared)", len(examples))

    if emails is not None:
        for email in emails:
            if email.message_id not in seen:
                process(email, store, llm, config, counts, log, examples, platforms, quiet)
        counts.pop("streak", None)
        return counts

    if not config.accounts:
        raise RuntimeError("No inboxes configured. Set GMAIL_ACCOUNTS or GMAIL_ADDRESS in .env (see README).")

    for n, (address, password) in enumerate(config.accounts, 1):
        if stop():
            break
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
                if stop():
                    break
                progress(f"Inbox {n} of {len(config.accounts)} ({address}): email {i} of {total['n']}")
                if email.message_id in seen:
                    continue
                process(email, store, llm, config, counts, log, examples, platforms, quiet)
                seen.add(email.message_id)
        except LLMError:
            raise
        except Exception as e:  # a bad password or network drop on one inbox shouldn't stop the others
            counts["failed_inboxes"].append(address)
            log(f"  ! {address}: {e}", file=sys.stderr)
            continue
        if stop():  # stopped part way, so this inbox's range isn't done
            counts["stopped"] = True
            break
        # A capped run (MAX_EMAILS) hasn't finished the range, so don't record it as synced.
        if not (config.max_emails and total["n"] >= config.max_emails):
            store.mark_synced(address, started, full=days is None)
    if moved and not stop():  # the AI re-reads them as sales (payout, fees) and learns from it
        from .inbox import label_emails
        for sale, mails in moved:
            label_emails(config, store, llm, mails, "sale", sale)
    counts.pop("streak", None)
    logger.info("sync %s: %s", "stopped" if counts.get("stopped") else "finished", summary(counts))
    return counts


def quick_check(config, store, llm=None, newest=0, stop=None, fetch=None):
    """Read the last day of email in every inbox (only the newest `newest` when set), so new orders, shipping and
    cancels show up within minutes instead of waiting for the next full sync. Emails already read are skipped by Message-ID before anything
    is downloaded. fetch replaces Gmail in tests. Returns the counts, like run_sync."""
    stop = stop or (lambda: False)
    fetch = fetch or newest_emails
    counts = {"order": 0, "botting_expense": 0, "other": 0, "skipped": 0, "error": 0, "failed_inboxes": []}
    picked = {}
    config.search_query = sales.with_terms(config.search_query, sales.custom_platforms(store.db))

    def known(ids):
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        return {r[0] for r in store.db.execute(
            "SELECT message_id FROM emails WHERE error IS NULL AND message_id IN (%s)" % marks, ids)}

    def log(message, file=None):
        (logger.warning if file is sys.stderr else logger.debug)(message.strip())

    for address, password in config.accounts:
        if stop():
            break
        try:
            for email in fetch(config, address, password, newest, known):
                if stop():
                    break
                if not picked:  # only worth building once there's something new to read
                    llm = llm or llm_for(config)
                    picked.update(platforms=sales.platforms(store.db), quiet=quiet_senders(store.db),
                                  examples=packs.picker(store, few_shot(store)) if config.use_examples else [])
                process(email, store, llm, config, counts, log, picked["examples"], picked["platforms"],
                        picked["quiet"])
        except LLMError:
            raise
        except Exception as e:
            counts["failed_inboxes"].append(address)
            logger.warning("quick check of %s failed: %s", address, e)
    counts.pop("streak", None)
    if picked:
        logger.info("quick check: %s", summary(counts))
    return counts


def sale_query(platforms):
    """A Gmail search for every email naming a selling platform."""
    words = [f'"{p}"' if " " in p else p for p in platforms]
    if "Tradepost" in platforms:
        words.append('"trade post"')
    return f"({' OR '.join(dict.fromkeys(words))} OR buylist OR \"trade-in\") -in:trash"


def find_sales(config, store, llm=None, days=365, stop=None, progress=None, fetch=None):
    """Go back over a year of mail from selling platforms (Tradepost, StockX, eBay...) and read again every one
    not already saved as a sale, order or expense, so payouts the sync skipped or called "not relevant" are found.
    Returns the counts, like run_sync."""
    import dataclasses
    stop, progress, fetch = stop or (lambda: False), progress or (lambda text: None), fetch or fetch_emails
    platforms = sales.platforms(store.db)
    search = dataclasses.replace(config, search_query=sale_query(platforms))
    counts = {"order": 0, "botting_expense": 0, "sale": 0, "other": 0, "skipped": 0, "error": 0, "failed_inboxes": []}
    done = {r[0] for r in store.db.execute(
        "SELECT message_id FROM emails WHERE kind IN ('order', 'botting_expense', 'sale') AND error IS NULL")}
    done |= {r[0] for r in store.db.execute("SELECT message_id FROM sales")}
    llm = llm or llm_for(config)
    examples = packs.picker(store, few_shot(store)) if config.use_examples else []

    def log(message, file=None):
        (logger.warning if file is sys.stderr else logger.info)(message.strip())

    for n, (address, password) in enumerate(config.accounts, 1):
        if stop():
            break
        try:
            for i, email in enumerate(fetch(search, address, password, days, done), 1):
                if stop():
                    break
                progress(f"Looking for sales in inbox {n} of {len(config.accounts)}: email {i}")
                if email.message_id in done:
                    continue
                process(email, store, llm, config, counts, log, examples, platforms)
                done.add(email.message_id)
        except LLMError:
            raise
        except Exception as e:
            counts["failed_inboxes"].append(address)
            logger.warning("finding sales in %s failed: %s", address, e)
    counts.pop("streak", None)
    logger.info("find sales: %s", summary(counts))
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
