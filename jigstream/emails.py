"""Read-only Gmail IMAP scan for retailer order emails.

Setup: enable 2FA on the Google account, create an App Password, then set
GMAIL_ADDRESS and GMAIL_APP_PASSWORD in the environment. Nothing is sent or
deleted; the mailbox is opened read-only.
"""
import email
import imaplib
import os
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

RETAILERS = {
    "Target": ["target.com"], "Amazon": ["amazon.com"], "Walmart": ["walmart.com"],
    "Best Buy": ["bestbuy.com"], "P-Bandai": ["p-bandai.com", "bandai.com", "bandai-hobby"],
    "Crunchyroll": ["crunchyroll.com"], "Topps": ["topps.com"],
    "Pokemon Center": ["pokemoncenter.com"],
    # Shopify stores send from shopifyemail.com with the store name as sender name
    "Shopify": ["shopifyemail.com", "shopify.com"],
}
# Site keywords used to match a webhook's "site" field to a retailer.
SITE_KEYS = {
    "Target": ["target"], "Amazon": ["amazon"], "Walmart": ["walmart"], "Best Buy": ["bestbuy", "best buy"],
    "P-Bandai": ["bandai"], "Crunchyroll": ["crunchyroll"], "Topps": ["topps"],
    "Pokemon Center": ["pokemoncenter", "pokemon center"],
}
# Subject lines decide first. Body text is only used for unmistakable phrases, so a footer like
# "to cancel your order visit..." in a shipping email is not read as a cancellation.
SUBJECT_RULES = [
    ("cancelled", r"cancel"),
    ("shipped", r"shipped|shipment|on its way|on the way|tracking|out for delivery|delivered"),
    ("confirmed", r"order (confirm|receiv|placed)|thanks for (your )?(order|shopping)|thank you for your order|we got your order|confirmed"),
]
BODY_RULES = [
    ("cancelled", r"(has|have|had|was|been|were) (been )?(successfully )?cancel(l)?ed|we('ve| have)? cancel(l)?ed|unable to (fulfill|process) your order|order (could not|couldn't) be (fulfilled|processed)"),
    ("shipped", r"(has|have) shipped|is on (its|the) way|tracking number"),
    ("confirmed", r"we('ve| have) received your order|your order (is|has been) confirmed"),
]
ORDER_RE = re.compile(
    r"(\d{3}-\d{7}-\d{7})"                                   # Amazon
    r"|order\s*(?:#|number|no\.?|id)?\s*[:\-]?\s*#?\s*([A-Z0-9][A-Z0-9\-]{3,})", re.I)
TOTAL_RE = re.compile(r"(?:order )?total[^$\d]{0,20}\$\s?([\d,]+\.\d{2})", re.I)


def retailer_for(sender):
    s = sender.lower()
    for name, doms in RETAILERS.items():
        if any(d in s for d in doms):
            return name
    return None


def store_name(sender):
    return re.sub(r"<.*?>|\"", "", sender).strip()


def classify_kind(subject, body=""):
    for kind, pat in SUBJECT_RULES:
        if re.search(pat, subject, re.I):
            return kind
    head = body[:1500]
    for kind, pat in BODY_RULES:
        if re.search(pat, head, re.I):
            return kind
    return None


def parse_message(msg):
    sender = str(make_header(decode_header(msg.get("From", ""))))
    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    retailer = retailer_for(sender)
    if not retailer:
        return None
    body = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload:
                txt = payload.decode(part.get_content_charset() or "utf-8", "replace")
                body += re.sub(r"<[^>]+>", " ", txt)
    kind = classify_kind(subject, body)
    if not kind:
        return None
    m = ORDER_RE.search(subject) or ORDER_RE.search(body)
    order = (m.group(1) or m.group(2)).upper() if m else ""
    t = TOTAL_RE.search(body)
    body = re.sub(r"\s+", " ", body).strip()[:3000]
    try:
        ts = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        ts = ""
    return {"message_id": msg.get("Message-ID", ""), "retailer": retailer, "kind": kind,
            "order_number": order, "product": "",
            "total": t.group(1) if t else "", "ts": ts, "subject": subject, "sender": sender,
            "store": store_name(sender), "body": body}


def store(con, rec):
    cur = con.execute(
        """INSERT OR IGNORE INTO orders (message_id,retailer,kind,order_number,product,total,ts,subject,sender,store,body)
        VALUES (:message_id,:retailer,:kind,:order_number,:product,:total,:ts,:subject,:sender,:store,:body)""", rec)
    return cur.rowcount


def sync_gmail(con, since="2024/01/01", limit=2000):
    user, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        raise RuntimeError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (a Google App Password).")
    domains = sorted({d for doms in RETAILERS.values() for d in doms})
    query = f"from:({' OR '.join(domains)}) after:{since}"
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(user, pw)
    imap.select('"[Gmail]/All Mail"', readonly=True)
    _, data = imap.search(None, "X-GM-RAW", f'"{query}"')
    ids = data[0].split()[-limit:]
    added = 0
    for i in ids:
        _, msgdata = imap.fetch(i, "(BODY.PEEK[])")
        rec = parse_message(email.message_from_bytes(msgdata[0][1]))
        if rec:
            added += store(con, rec)
    con.commit()
    imap.logout()
    return added
