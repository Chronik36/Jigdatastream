"""Command line: python -m jigstream [sync|check]. Run the dashboard with python run.py."""

import argparse
import sys

from . import db, tracking
from .config import Config
from .llm import LLMClient, LLMError
from .orderstore import Store
from .sync import run_sync, summary


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jigstream", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sync", help="pull new emails and extract orders and botting receipts")
    s.add_argument("--days", type=int, help="search this many days back (default: 365 on an inbox's "
                   "first sync, then only mail since the last sync)")
    s.add_argument("--all", action="store_true", help="search each whole mailbox (resumable backfill)")
    s.add_argument("--inbox", help="only sync this address")
    s.add_argument("--limit", type=int, help="max new emails per inbox this run")
    sub.add_parser("check", help="test the connection to the local model")
    t = sub.add_parser("track", help="refresh live carrier tracking now (needs SHIP24_API_KEY)")
    t.add_argument("--force", action="store_true", help="refresh even shipments checked in the last few hours")
    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.command == "track":
        provider = tracking.provider_from_env()
        if not provider:
            sys.exit("Set SHIP24_API_KEY in .env first (see README).")
        n = tracking.refresh(db.connect(), provider, min_hours=0 if args.force else 4)
        print(f"Live tracking updated for {n} shipments.")
        return
    try:
        if args.command == "check":
            llm = LLMClient(config.llm_base_url, config.llm_model, config.llm_api_key, config.llm_timeout)
            reply = llm.chat_json("Reply with JSON only.", 'Return {"ok": true}')
            print(f"Model {config.llm_model} at {config.llm_base_url} replied: {reply}")
            return
        config.days_back = args.days
        config.full_history = args.all
        if args.limit:
            config.max_emails = args.limit
        if args.inbox:
            config.accounts = [a for a in config.accounts if a[0].lower() == args.inbox.lower()]
            if not config.accounts:
                sys.exit(f"{args.inbox} is not in GMAIL_ACCOUNTS")
        counts = run_sync(config, Store(db.connect()))
    except (LLMError, RuntimeError) as e:
        sys.exit(str(e))
    print(f"Done: {summary(counts)}")


if __name__ == "__main__":
    main()
