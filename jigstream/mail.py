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
    account: str = ""  # the inbox it was read from


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "td", "th"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            # Tracking numbers often only appear inside carrier links.
            if re.search(r"track|ups\.com|usps\.com|fedex\.com|dhl\.", href, re.I):
                self.parts.append(f" [{href}] ")
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


def _batches(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def fetch_emails(config, address, password, days=None, skip_ids=(), on_total=None):
    """Yield new Email objects from one inbox, newest first.

    days=None searches the whole mailbox. Messages whose Message-ID is in skip_ids
    are never downloaded, which is what makes an interrupted backfill resumable.
    """
    query = config.search_query + (f" newer_than:{days}d" if days else "")
    imap = imaplib.IMAP4_SSL(config.imap_host)
    try:
        try:
            imap.login(address, password)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"Gmail login failed for {address}: check the app password ({e})") from e
        status, _ = imap.select(config.mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open mailbox {config.mailbox} for {address}")
        status, data = imap.uid("SEARCH", "X-GM-RAW", _quote(query))
        if status != "OK":
            raise RuntimeError(f"Gmail search failed for {address}: {data}")
        uids = data[0].split()[::-1]

        # Read Message-IDs in batches so already-processed mail costs almost nothing.
        skip, new = set(skip_ids), []
        for batch in _batches(uids, 500):
            status, data = imap.uid("FETCH", b",".join(batch), "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if status != "OK":
                new.extend(batch)
                continue
            for i, part in enumerate(data):
                if not isinstance(part, tuple):
                    continue
                # The UID item can come before or after the header literal.
                after = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
                m = re.search(rb"UID (\d+)", part[0]) or re.search(rb"UID (\d+)", after)
                if not m:
                    continue
                if str(email.message_from_bytes(part[1])["Message-ID"] or "").strip() not in skip:
                    new.append(m.group(1))
        order = {uid: i for i, uid in enumerate(uids)}
        new.sort(key=lambda u: order.get(u, 0))
        if config.max_emails:
            new = new[: config.max_emails]
        if on_total:
            on_total(len(new))

        for uid in new:
            status, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = parse_message(data[0][1])
            if not msg.message_id:
                msg.message_id = f"uid-{address}-{uid.decode()}"
            msg.account = address
            yield msg
    finally:
        try:
            imap.logout()
        except Exception:
            pass
