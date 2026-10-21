"""Fetch candidate order and receipt emails from Gmail over IMAP."""

import email
import imaplib
import re
from dataclasses import dataclass
from email import policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser


@dataclass
class Email:
    message_id: str
    sender: str
    subject: str
    date: str  # ISO 8601, or "" when the header is missing
    body: str


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "td", "th"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n" if tag != "td" else " | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html):
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def message_text(msg):
    """Best readable text for a message: the HTML part (receipts put the
    details there), falling back to plain text."""
    html = plain = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            content = part.get_payload(decode=True).decode("utf-8", "replace")
        if ctype == "text/html" and html is None:
            html = content
        elif ctype == "text/plain" and plain is None:
            plain = content
    if html:
        return html_to_text(html)
    return (plain or "").strip()


def parse_message(raw):
    msg = email.message_from_bytes(raw, policy=policy.default)
    date = ""
    if msg["Date"]:
        try:
            date = parsedate_to_datetime(str(msg["Date"])).isoformat()
        except (TypeError, ValueError):
            pass
    return Email(
        message_id=str(msg["Message-ID"] or "").strip(),
        sender=str(msg["From"] or ""),
        subject=str(msg["Subject"] or ""),
        date=date,
        body=message_text(msg),
    )


def _quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fetch_emails(config, skip_ids=()):
    """Yield Email objects matching the Gmail search, newest first."""
    if not config.gmail_address or not config.gmail_app_password:
        raise RuntimeError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (see README).")
    query = f"{config.search_query} newer_than:{config.days_back}d"
    imap = imaplib.IMAP4_SSL(config.imap_host)
    try:
        imap.login(config.gmail_address, config.gmail_app_password)
        status, _ = imap.select(config.mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open mailbox {config.mailbox}")
        status, data = imap.uid("SEARCH", "X-GM-RAW", _quote(query))
        if status != "OK":
            raise RuntimeError(f"Gmail search failed: {data}")
        uids = data[0].split()[::-1][: config.max_emails]
        skip = set(skip_ids)
        for uid in uids:
            # Peek at headers first so already-processed mail costs one small fetch.
            status, data = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if status == "OK" and data and data[0]:
                header = email.message_from_bytes(data[0][1])
                if str(header["Message-ID"] or "").strip() in skip:
                    continue
            status, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not data or not data[0]:
                continue
            msg = parse_message(data[0][1])
            if not msg.message_id:
                msg.message_id = f"uid-{uid.decode()}"
            yield msg
    finally:
        try:
            imap.logout()
        except Exception:
            pass
