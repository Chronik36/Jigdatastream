"""Pull new emails, extract them with the local model, and store the results."""

import sys

from .extract import extract
from .llm import LLMClient, LLMError
from .mail import fetch_emails


def run_sync(config, store, emails=None, llm=None, log=print):
    llm = llm or LLMClient(config.llm_base_url, config.llm_model, config.llm_api_key, config.llm_timeout)
    seen = store.seen_ids()
    if emails is None:
        emails = fetch_emails(config, skip_ids=seen)
    counts = {"order": 0, "botting_expense": 0, "other": 0, "error": 0}
    for email in emails:
        if email.message_id in seen:
            continue
        try:
            result = extract(llm, email, config.max_body_chars)
        except LLMError as e:
            if "Could not reach" in str(e):
                raise
            store.record_email(email, "error", str(e)[:500])
            counts["error"] += 1
            log(f"  ! {email.subject[:70]}: {e}", file=sys.stderr)
            continue
        store.save(email, result)
        counts[result["kind"]] += 1
        if result["kind"] != "other":
            label = result.get("retailer") or result.get("vendor")
            log(f"  {result['kind']:<16} {label or '?'}: {email.subject[:60]}")
    return counts
