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
    "Nike/SNKRS": ["nike.com"], "Footlocker": ["footlocker.com", "champssports.com", "eastbay.com"],
    "Finish Line": ["finishline.com"], "JD Sports": ["jdsports.com"], "Shopify": ["shopify.com"],
    "Best Buy": ["bestbuy.com"], "Target": ["target.com"], "Walmart": ["walmart.com"],
    "Adidas": ["adidas.com"], "Amazon": ["amazon.com"], "GameStop": ["gamestop.com"],
}
KIND_RULES = [
    ("cancelled", r"cancel"),
    ("shipped", r"shipped|on its way|tracking|out for delivery"),
    ("confirmed", r"order (confirm|receiv)|thanks for (your )?order|thank you for your order|order placed|we got your order"),
]
ORDER_RE = re.compile(r"(?:order\s*(?:#|number|no\.?|id)?\s*[:\-]?\s*)([A-Z0-9][A-Z0-9\-]{5,})", re.I)
TOTAL_RE = re.compile(r"(?:order )?total[^$\d]{0,20}\$\s?([\d,]+\.\d{2})", re.I)


def retailer_for(sender):
    s = sender.lower()
    for name, doms in RETAILERS.items():
        if any(d in s for d in doms):
            return name
    return None


def classify_kind(subject, body=""):
    text = f"{subject} {body[:500]}".lower()
    for kind, pat in KIND_RULES:
        if re.search(pat, text):
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
    t = TOTAL_RE.search(body)
    try:
        ts = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        ts = ""
    return {"message_id": msg.get("Message-ID", ""), "retailer": retailer, "kind": kind,
            "order_number": m.group(1).upper() if m else "", "product": "",
            "total": t.group(1) if t else "", "ts": ts, "subject": subject, "sender": sender}


def store(con, rec):
    cur = con.execute(
        """INSERT OR IGNORE INTO orders (message_id,retailer,kind,order_number,product,total,ts,subject,sender)
        VALUES (:message_id,:retailer,:kind,:order_number,:product,:total,:ts,:subject,:sender)""", rec)
    return cur.rowcount


def sync_gmail(con, since="01-Jan-2024", limit=500):
    user, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        raise RuntimeError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (a Google App Password).")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(user, pw)
    imap.select('"[Gmail]/All Mail"', readonly=True)
    _, data = imap.search(None, f'(SINCE "{since}")')
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
