"""Computers signed into the same Gmail find each other and link by themselves, with no code to type.

Each computer that has it turned on keeps one small note about itself in a Gmail label of its own, BotTrack-Link
(never in the inbox): its id, its name, its link address and the group key. The first computer makes the group
key; the others take it from the oldest note. Two computers in the same group work out the same key for their pair
from it (peersync.sync_peers.secret), so they can swap right away. Old notes go to Gmail's Trash.

Only someone who can open the Gmail can join, the same as anything else in it. Turning it off removes this
computer's note.
"""
import email
import hashlib
import hmac
import imaplib
import json
import logging
import secrets
import time
from email.message import EmailMessage

from . import mail, peersync

logger = logging.getLogger("jigstream.gmaillink")

LABEL = "BotTrack-Link"
SUBJECT = "BotTrack link "
EVERY = 600  # seconds between looks at the label
_last = [0.0]


def on(con):
    return peersync.meta(con, "gmail_link") == "1"


class GmailBox:
    """The BotTrack-Link label of one Gmail inbox, over IMAP."""

    def __init__(self, config, address, password):
        self.config, self.address, self.password = config, address, password

    def _open(self):
        imap = imaplib.IMAP4_SSL(self.config.imap_host, timeout=30)
        mail.login(imap, self.address, self.password)
        imap.create(f'"{LABEL}"')  # an error when it's there already; fine
        imap.select(f'"{LABEL}"')
        return imap

    def notes(self):
        imap = self._open()
        try:
            _, data = imap.uid("SEARCH", None, "ALL")
            out = []
            for uid in (data[0] or b"").split()[-50:]:
                _, msg = imap.uid("FETCH", uid, "(RFC822)")
                raw = next((m[1] for m in msg if isinstance(m, tuple)), b"")
                try:
                    body = email.message_from_bytes(raw).get_payload(decode=True) or b""
                    out.append(json.loads(body))
                except (ValueError, TypeError):
                    continue
            return out
        finally:
            imap.logout()

    def post(self, site, note):
        """Replace this computer's note (None just removes it)."""
        imap = self._open()
        try:
            _, data = imap.uid("SEARCH", None, f'SUBJECT "{SUBJECT}{site}"')
            for uid in (data[0] or b"").split():
                imap.uid("COPY", uid, '"[Gmail]/Trash"')
                imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            imap.expunge()
            if note is not None:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = SUBJECT + site, self.address, self.address
                msg.set_content(json.dumps(note))
                imap.append(f'"{LABEL}"', r"(\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        finally:
            imap.logout()


def boxes(config):
    return [GmailBox(config, a, p) for a, p in config.accounts]


def pair_secret(group, a, b):
    return hmac.new(group.encode(), "|".join(sorted([a, b])).encode(), hashlib.sha256).hexdigest()


def meet(con, config, box_list=None):
    """Post this computer's note and link with every other computer in the group. Returns the names linked now."""
    if not on(con):
        return []
    box_list = boxes(config) if box_list is None else box_list
    if not box_list:
        return []
    me = peersync.site(con)
    notes = {}
    for box in box_list:
        try:
            for n in box.notes():
                if isinstance(n, dict) and n.get("site") and n.get("key"):
                    if n["site"] not in notes or n.get("at", "") > notes[n["site"]].get("at", ""):
                        notes[n["site"]] = n
        except Exception as e:
            logger.warning("couldn't read the %s label in %s: %s", LABEL, box.address, e)
    since = peersync.meta(con, "gmail_since") or peersync.now()
    group = peersync.meta(con, "gmail_group") or secrets.token_urlsafe(24)
    # Two computers that started a group at the same time end up in the older one.
    first = min([n for s, n in notes.items() if s != me] + [{"since": since, "site": me, "key": group}],
                key=lambda n: (n.get("since", ""), n["site"]))
    group = first["key"]
    peersync.set_meta(con, "gmail_since", since)
    peersync.set_meta(con, "gmail_group", group)
    mine = {"site": me, "name": peersync.computer_name(), "url": peersync.my_url(), "topic": peersync.sync_topic(),
            "key": group, "since": peersync.meta(con, "gmail_since"), "at": peersync.now()}
    old = notes.get(me) or {}
    if any(old.get(k) != mine[k] for k in ("name", "url", "topic", "key", "since")):
        for box in box_list:
            try:
                box.post(me, mine)
            except Exception as e:
                logger.warning("couldn't leave a note in %s: %s", box.address, e)
    linked = []
    for site, n in notes.items():
        if site == me or n.get("key") != group:
            continue
        secret = pair_secret(group, me, site)
        row = con.execute("SELECT id, secret FROM sync_peers WHERE site=?", (site,)).fetchone()
        if row is None:
            con.execute("INSERT INTO sync_peers (site, name, url, topic, secret, paired_at) VALUES (?,?,?,?,?,?)",
                        (site, (n.get("name") or "Other computer")[:80], n.get("url") or "", n.get("topic"), secret, peersync.now()))
            linked.append(n.get("name") or "Other computer")
            logger.info("linked with %s through Gmail", n.get("name"))
        else:
            con.execute("UPDATE sync_peers SET secret=?, name=?, url=CASE WHEN ?!='' THEN ? ELSE url END, "
                        "topic=COALESCE(?, topic) WHERE id=?",
                        (secret, (n.get("name") or "Other computer")[:80], n.get("url") or "", n.get("url") or "",
                         n.get("topic"), row["id"]))
    if linked:
        peersync.install(con)
    return linked


def turn(con, config, enable, box_list=None):
    """Turn linking through Gmail on or off for this computer."""
    peersync.set_meta(con, "gmail_link", "1" if enable else "0")
    if enable:
        _last[0] = time.time()
        return meet(con, config, box_list)
    for box in boxes(config) if box_list is None else box_list:
        try:
            box.post(peersync.site(con), None)
        except Exception as e:
            logger.warning("couldn't remove the note from %s: %s", box.address, e)
    return []


def due():
    if time.time() - _last[0] >= EVERY:
        _last[0] = time.time()
        return True
    return False
