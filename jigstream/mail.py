"""Fetch candidate order and receipt emails from Gmail over IMAP."""

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from email import policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser


logger = logging.getLogger("jigstream.mail")


@dataclass
class Email:
    message_id: str
    sender: str
    subject: str
    date: str  # ISO 8601, or "" when the header is missing
    body: str
    account: str = ""  # the inbox it was read from
    to: str = ""  # the address the email was sent to (a profile's email that forwards here)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "td", "th"}
    # Cents drawn small next to the dollars ($45<sup>00</sup>, Amazon's a-price-fraction): without the dot they
    # read as $4500.
    CENTS = re.compile(r"fraction|cents?\b|decimal|price-small|price__cents", re.I)

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0
        self._cents = []  # one entry per open tag: is it a cents tag?

    def handle_starttag(self, tag, attrs):
        if tag not in ("br", "img", "hr", "meta", "input", "wbr"):
            self._cents.append(tag == "sup" or bool(self.CENTS.search(dict(attrs).get("class") or "")))
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
        if tag not in ("br", "img", "hr", "meta", "input", "wbr") and self._cents:
            self._cents.pop()

    def handle_data(self, data):
        if self._skip:
            return
        if self._cents and self._cents[-1] and re.fullmatch(r"\s*\d{2}\s*", data):
            before = "".join(self.parts[-3:]).rstrip()
            if re.search(r"[$\u00a3\u20ac]\s?\d[\d,]*$", before):
                self.parts[-1] = self.parts[-1].rstrip()
                data = "." + data.strip()
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


ADDRESS = re.compile(r"[\w.+'=-]+@[\w-]+(?:\.[\w-]+)+")
FORWARD_SUBJECT = re.compile(r"^\s*((fwd?|fw|tr|wg|rv)\s*:\s*|\[fwd?:?\s*)+", re.I)
FORWARD_MARK = re.compile(r"-{2,}\s*(forwarded message|original message|message transf\w+)\s*-{2,}|"
                          r"^\s*begin forwarded message:", re.I | re.M)
FORWARD_HEADER = re.compile(r"^\s*\*?(from|sent|date|to|subject|cc)\s*:\s*\*?\s*(.*)$", re.I)


def _clean_address_text(text):
    # Outlook writes "Target <mailto:orders@target.com>" or "orders@target.com [mailto:orders@target.com]".
    text = re.sub(r"\s*\[mailto:[^\]]*\]", "", text, flags=re.I)
    return re.sub(r"mailto:", "", text, flags=re.I).strip()


def _forward_date(text):
    text = re.sub(r"\s+at\s+", " ", (text or "").strip())
    try:
        return parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    from datetime import datetime
    for fmt in ("%A, %B %d, %Y %I:%M %p", "%a, %b %d, %Y %I:%M %p", "%B %d, %Y %I:%M:%S %p %Z",
                "%B %d, %Y %I:%M %p", "%a, %b %d, %Y, %I:%M %p", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(text[:40].strip(), fmt).isoformat()
        except ValueError:
            pass
    return ""


def forwarded_headers(body):
    """The original From / To / Subject / Date of a forwarded email, read from the block that Gmail,
    Outlook and Apple Mail put at the top of a forward. {} when the body has no such block."""
    lines = (body or "").splitlines()[:80]
    mark = FORWARD_MARK.search("\n".join(lines))
    start = "\n".join(lines)[:mark.end()].count("\n") + 1 if mark else 0
    found, key = {}, None
    for line in lines[start:start + 30]:
        clean = line.strip(" |").lstrip("> ").strip()
        m = FORWARD_HEADER.match(clean)
        if m:
            key = m.group(1).lower()
            key = "date" if key == "sent" else key
            if key not in found:
                found[key] = _clean_address_text(m.group(2))
            continue
        if found and not clean:
            continue
        if found:
            if key in ("to", "cc") and ADDRESS.search(clean) and ":" not in clean[:12]:
                found[key] += " " + _clean_address_text(clean)  # a To list wrapped onto the next line
                continue
            break
    # Without a "Forwarded message" line, only trust a block that looks like one: From plus Subject or Sent.
    if not mark and not ("from" in found and ("subject" in found or "date" in found)):
        return {}
    return found if "from" in found else {}


def original_recipients(msg, forwarded=None):
    """Who the store really sent this to, best first: the address in a forwarded block, then
    X-Original-To and Gmail's X-Forwarded-For (set when another inbox forwards here), then To.
    iCloud Hide My Email keeps the hidden address in To, so it comes through as is."""
    out = []
    for text in ((forwarded or {}).get("to"), msg["X-Original-To"],
                 (str(msg["X-Forwarded-For"] or "").split() or [""])[0], msg["To"], msg["Delivered-To"]):
        for a in ADDRESS.findall(str(text or "")):
            if a.lower() not in out:
                out.append(a.lower())
    return ", ".join(out)


def parse_message(raw):
    msg = email.message_from_bytes(raw, policy=policy.default)
    date = ""
    if msg["Date"]:
        try:
            date = parsedate_to_datetime(str(msg["Date"])).isoformat()
        except (TypeError, ValueError):
            pass
    body = message_text(msg)
    sender, subject = str(msg["From"] or ""), str(msg["Subject"] or "")
    forwarded = forwarded_headers(body) if (FORWARD_SUBJECT.match(subject) or FORWARD_MARK.search(body[:4000])) else {}
    if forwarded:
        # A forward from Outlook (or by hand): the store's own sender and subject are inside.
        sender = forwarded.get("from") or sender
        subject = forwarded.get("subject") or subject
        date = date if not forwarded.get("date") else (_forward_date(forwarded["date"]) or date)
    return Email(
        message_id=str(msg["Message-ID"] or "").strip(),
        sender=sender,
        subject=FORWARD_SUBJECT.sub("", subject).strip() or subject,
        date=date,
        body=body,
        to=original_recipients(msg, forwarded),
    )


def _quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _batches(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def check_login(config, address, password):
    """Try an IMAP login; return None if it works, else a message to show."""
    try:
        imap = imaplib.IMAP4_SSL(config.imap_host, timeout=20)
    except OSError as e:
        return f"Could not reach Gmail: {e}"
    try:
        imap.login(address, password)
        return None
    except imaplib.IMAP4.error as e:
        logger.warning("login check failed for %s: %s", address, e)
        return ("Gmail rejected that address and app password. Use a 16-letter app password "
                "(not your normal password), and make sure 2-Step Verification is on.")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def spam_folder(imap):
    """The Spam folder's name, found by its \\Junk flag since Gmail localizes it."""
    status, data = imap.list()
    if status == "OK":
        for line in data or []:
            if isinstance(line, bytes) and b"\\Junk" in line:
                m = re.search(rb'"[^"]*" ("?)(.+?)\1$', line) or re.search(rb" (\S+)$", line)
                if m:
                    name = m.group(m.lastindex).decode()
                    return name if name.startswith('"') else f'"{name}"'
    return '"[Gmail]/Spam"'


def _new_uids(imap, query, skip, address):
    """UIDs matching query in the selected folder whose Message-ID isn't in skip, newest first."""
    status, data = imap.uid("SEARCH", "X-GM-RAW", _quote(query))
    if status != "OK":
        raise RuntimeError(f"Gmail search failed for {address}: {data}")
    uids = data[0].split()[::-1]
    new = []
    # Read Message-IDs in batches so already-processed mail costs almost nothing.
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
            message_id = str(email.message_from_bytes(part[1])["Message-ID"] or "").strip()
            if message_id not in skip:
                new.append(m.group(1))
                if message_id:
                    skip.add(message_id)  # the same mail in two folders is read once
    order = {uid: i for i, uid in enumerate(uids)}
    new.sort(key=lambda u: order.get(u, 0))
    return new


def fetch_emails(config, address, password, days=None, skip_ids=(), on_total=None):
    """Yield new Email objects from one inbox: All Mail, then Spam (forwarded
    receipts often land there). Newest first within each folder.

    days=None searches the whole mailbox. Messages whose Message-ID is in skip_ids
    are never downloaded, which is what makes an interrupted backfill resumable.
    """
    since = f" newer_than:{days}d" if days else ""
    imap = imaplib.IMAP4_SSL(config.imap_host)
    try:
        try:
            imap.login(address, password)
        except imaplib.IMAP4.error as e:
            logger.error("Gmail login failed for %s: %s", address, e)
            raise RuntimeError(f"Gmail login failed for {address}: check the app password ({e})") from e
        skip = set(skip_ids)
        folders = [(config.mailbox, config.search_query + since)]
        spam_query = config.search_query.replace("-in:spam", "").strip() + " in:spam" + since
        folders.append((spam_folder(imap), spam_query))

        plan = []
        for folder, query in folders:
            status, _ = imap.select(folder, readonly=True)
            if status != "OK":
                if folder == config.mailbox:
                    raise RuntimeError(f"Could not open mailbox {folder} for {address}")
                logger.info("%s: folder %s not available, skipping it", address, folder)
                continue  # no Spam folder visible over IMAP: carry on without it
            plan.append((folder, _new_uids(imap, query, skip, address)))
            logger.info("%s %s: %d new matching emails (query: %s)", address, folder, len(plan[-1][1]), query)
        if config.max_emails:
            left, capped = config.max_emails, []
            for folder, uids in plan:
                capped.append((folder, uids[:left]))
                left = max(0, left - len(uids))
            plan = capped
        if on_total:
            on_total(sum(len(uids) for _, uids in plan))

        for folder, uids in plan:
            if not uids:
                continue
            imap.select(folder, readonly=True)
            for uid in uids:
                status, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not data or not isinstance(data[0], tuple):
                    continue
                msg = parse_message(data[0][1])
                if not msg.message_id:
                    msg.message_id = f"uid-{address}-{folder}-{uid.decode()}"
                msg.account = address
                yield msg
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _header_rows(imap, uids, address, spam):
    rows = []
    for batch in _batches(uids, 100):
        status, data = imap.uid("FETCH", b",".join(batch),
                                "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
        if status != "OK":
            continue
        for i, part in enumerate(data):
            if not isinstance(part, tuple):
                continue
            msg = parse_message(part[1])
            after = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
            uid = re.search(rb"UID (\d+)", part[0]) or re.search(rb"UID (\d+)", after)
            rows.append({"message_id": msg.message_id, "sender": msg.sender, "subject": msg.subject,
                         "date": msg.date, "account": address, "spam": spam, "uid": int(uid.group(1)) if uid else 0})
    return rows


def search_headers(config, address, password, query="", limit=50, after=None):
    """Newest emails matching a Gmail search (headers only, nothing downloaded), All Mail then Spam.
    after = {"f": folder (0 All Mail, 1 Spam), "u": uid} continues below the last email already shown.
    Each row carries its folder ("f") and "uid" so the caller can say where the next page starts."""
    imap = imaplib.IMAP4_SSL(config.imap_host, timeout=30)
    after = after or {"f": 0, "u": None}
    try:
        imap.login(address, password)
        rows = []
        for f, (folder, spam) in enumerate(((config.mailbox, False), (spam_folder(imap), True))):
            if f < after["f"] or len(rows) >= limit or imap.select(folder, readonly=True)[0] != "OK":
                continue
            q = (query or "in:anywhere").strip()
            status, data = imap.uid("SEARCH", "X-GM-RAW", _quote(q))
            if status != "OK":
                continue
            uids = sorted((int(u) for u in data[0].split()), reverse=True)
            if f == after["f"] and after.get("u"):
                uids = [u for u in uids if u < after["u"]]
            found = _header_rows(imap, [str(u).encode() for u in uids[: limit - len(rows)]], address, spam)
            rows += sorted((dict(r, f=f) for r in found), key=lambda r: -r["uid"])
        return rows
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def fetch_by_message_id(config, address, password, message_id):
    """One email by its Message-ID, read-only, from All Mail or Spam. None if it isn't there."""
    imap = imaplib.IMAP4_SSL(config.imap_host, timeout=30)
    try:
        imap.login(address, password)
        for folder in (config.mailbox, spam_folder(imap)):
            if imap.select(folder, readonly=True)[0] != "OK":
                continue
            status, data = imap.uid("SEARCH", "X-GM-RAW", _quote(f"rfc822msgid:{message_id.strip('<>')}"))
            uids = data[0].split() if status == "OK" and data and data[0] else []
            if not uids:
                continue
            status, data = imap.uid("FETCH", uids[0], "(BODY.PEEK[])")
            if status == "OK" and data and isinstance(data[0], tuple):
                msg = parse_message(data[0][1])
                msg.account = address
                msg.message_id = msg.message_id or message_id
                return msg
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def fetch_many(config, address, password, message_ids):
    """Several emails by Message-ID from one inbox over one sign-in: yields (message_id, Email or None)."""
    imap = imaplib.IMAP4_SSL(config.imap_host, timeout=30)
    try:
        imap.login(address, password)
        folders = [config.mailbox, spam_folder(imap)]
        for message_id in message_ids:
            found = None
            for folder in folders:
                if not folder or imap.select(folder, readonly=True)[0] != "OK":
                    continue
                status, data = imap.uid("SEARCH", "X-GM-RAW", _quote(f"rfc822msgid:{message_id.strip('<>')}"))
                uids = data[0].split() if status == "OK" and data and data[0] else []
                if not uids:
                    continue
                status, data = imap.uid("FETCH", uids[0], "(BODY.PEEK[])")
                if status == "OK" and data and isinstance(data[0], tuple):
                    found = parse_message(data[0][1])
                    found.account = address
                    found.message_id = found.message_id or message_id
                    break
            yield message_id, found
    finally:
        try:
            imap.logout()
        except Exception:
            pass
