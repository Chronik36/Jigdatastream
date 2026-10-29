"""Logs & ideas: on the developer's own copy (the one with the signing key), the logs, provider suggestions,
training packs, shared steps and access requests people send from their BotTrack, read from his connected
inboxes. It works from the phone too, so new logs can be checked and read anywhere.

It only shows them. Nothing anyone sends is acted on from here: logs and suggestions from other people are read
and talked over first."""

import logging
import re
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from . import accountkeys, db, inbox
from .config import Config

logger = logging.getLogger("jigstream.devmail")
bp = Blueprint("devmail", __name__)

KINDS = [  # (key, label, subject start)
    ("logs", "Logs", "BotTrack logs"),
    ("suggestion", "Provider ideas", "BotTrack provider suggestion"),
    ("lessons", "Shared learning", "BotTrack shared learning"),
    ("pack", "Training packs (older builds)", "BotTrack training pack"),
    ("steps", "Shared steps", "BotTrack shared steps"),
    ("access", "Access requests", "BotTrack access request"),
]
QUERY = "subject:(" + " OR ".join(f'"{s}"' for _, _, s in KINDS) + ")"
SCHEMA = "CREATE TABLE IF NOT EXISTS devmail_seen (message_id TEXT PRIMARY KEY, seen_at TEXT);"


def allowed():
    return bool(accountkeys.signing_key())


def _con():
    return db.setup_once(db.connect(current_app.config.get("DB_PATH")), "devmail",
                         lambda c: c.executescript(SCHEMA))


def kind_of(subject):
    s = (subject or "").lower()
    return next((k for k, _, start in KINDS if s.startswith(start.lower())), None)


def mine(sender):
    """Sent from one of this computer's own inboxes (jacob's own app), rather than by someone else."""
    m = re.search(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", sender or "")
    return bool(m) and m.group(0).lower() in {a.lower() for a, _ in Config.from_env().accounts}


@bp.app_context_processor
def nav():
    return {"dev_mail": allowed()}


@bp.get("/developer/mail")
def page():
    if not allowed():
        abort(404)
    config = Config.from_env()
    show = request.args.get("kind", "")
    rows, errors, _ = inbox.search(config, QUERY, limit=60) if config.accounts else ([], [], None)
    c = _con()
    seen = {r[0] for r in c.execute("SELECT message_id FROM devmail_seen")}
    out, counts = [], {}
    for r in rows:
        k = kind_of(r["subject"])
        if not k:
            continue
        counts[k] = counts.get(k, 0) + 1
        if show and k != show:
            continue
        out.append(dict(r, kind=k, new=r["message_id"] not in seen, mine=mine(r["sender"])))
    return render_template("devmail.html", rows=out, errors=errors, kinds=KINDS, counts=counts, show=show,
                           new=sum(1 for r in out if r["new"]), configured=bool(config.accounts))


@bp.get("/developer/mail/read")
def read():
    if not allowed():
        abort(404)
    account, mid = request.args.get("account", ""), request.args.get("id", "")
    email = inbox.open_email(Config.from_env(), account, mid)
    if not email:
        return jsonify({"error": "That email couldn't be found in Gmail."}), 404
    c = _con()
    c.execute("INSERT OR IGNORE INTO devmail_seen VALUES (?, ?)", (mid, datetime.now(timezone.utc).isoformat()))
    c.commit()
    return jsonify({"subject": email.subject, "sender": email.sender, "date": email.date, "body": email.body[:80000]})
