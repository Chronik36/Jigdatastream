"""Command line: python -m jigstream [sync|check]. Run the dashboard with python run.py."""

import argparse
import sys

from . import db
from .config import Config
from .llm import LLMClient, LLMError
from .orderstore import Store
from .sync import run_sync


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jigstream", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sync", help="pull new emails and extract orders and botting receipts")
    s.add_argument("--days", type=int, help="how far back to search (default 90)")
    s.add_argument("--limit", type=int, help="max emails to look at this run")
    sub.add_parser("check", help="test the connection to the local model")
    args = parser.parse_args(argv)

    config = Config.from_env()
    try:
        if args.command == "check":
            llm = LLMClient(config.llm_base_url, config.llm_model, config.llm_api_key, config.llm_timeout)
            reply = llm.chat_json("Reply with JSON only.", 'Return {"ok": true}')
            print(f"Model {config.llm_model} at {config.llm_base_url} replied: {reply}")
            return
        if args.days:
            config.days_back = args.days
        if args.limit:
            config.max_emails = args.limit
        counts = run_sync(config, Store(db.connect()))
    except (LLMError, RuntimeError) as e:
        sys.exit(str(e))
    print(f"Done: {counts['order']} order emails, {counts['botting_expense']} botting receipts, "
          f"{counts['other']} other, {counts['error']} errors.")


if __name__ == "__main__":
    main()
