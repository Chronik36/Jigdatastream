"""Turn an email into a structured order or botting expense using the local model."""

import re
from datetime import datetime

STATUSES = [
    "ordered", "processing", "shipped", "in_transit", "out_for_delivery",
    "delivered", "cancelled", "returned", "refunded", "unknown",
]
EXPENSE_CATEGORIES = ["proxy", "bot", "cook_group", "captcha", "server", "aco_service", "account", "other"]
BILLING_PERIODS = ["one_time", "weekly", "monthly", "yearly"]

# Words that point at botting tools in general. There is deliberately no vendor list:
# new proxy and bot sellers appear all the time, so the model judges from content.
BOTTING_WORDS = re.compile(
    r"\b(prox(y|ies)|residential|isp|datacenter|\d+\s?gb\b|aio|sneaker bot|retail bot|bot key|"
    r"license key|renewal|cook ?group|discord|whop|captcha|vps|server|aco|checkout service)\b",
    re.I,
)
MONEY = re.compile(r"(?:[$\u00a3\u20ac]\s?\d[\d,]*(?:\.\d{2})?|\b\d[\d,]*\.\d{2}\s?(?:USD|EUR|GBP|CAD)\b)")
PURCHASE_WORDS = re.compile(
    r"\b(order|receipt|invoice|payment|paid|charged?|subscription|renew(al|ed)?|purchase[ds]?|"
    r"total|billing|shipped|shipping|delivered|delivery|tracking|cancel+ed|refund(ed)?|return(ed)?)\b",
    re.I,
)

SYSTEM_PROMPT = f"""You read one email and extract purchase data as JSON. Reply with JSON only.

Decide "kind":
- "order": a purchase of physical goods from a store (order confirmation, shipping notice, delivery notice, cancellation, refund, return).
- "botting_expense": a receipt for tools used for retail botting or reselling automation. Judge from what was bought, not who sent it, because new sellers appear all the time. Examples: proxies (residential, ISP, datacenter, per-GB data plans), bot software licenses, keys or renewals, cook group or Discord memberships (often sold through whop.com), captcha solving credits, servers/VPS, ACO (auto-checkout) services, aged or generated accounts.
- "other": anything else: marketing, newsletters, account notices, receipts unrelated to retail or botting.

For botting expenses, "vendor" is the business that sold the product, not the payment processor. If the receipt comes from Stripe, PayPal, Paddle, Sellix, Shopify, Whop, Gumroad or similar, use the product's or community's own name shown in the receipt (for whop.com, the community or product name). Only use the processor's name if no other seller is named.

JSON keys (use null when unknown, never invent values):
{{
  "kind": "order" | "botting_expense" | "other",
  "retailer": store or brand name for orders,
  "order_number": string,
  "order_date": "YYYY-MM-DD",
  "items": [{{"name": string, "quantity": number, "price": number}}],
  "total": number,
  "currency": "USD" or other ISO code,
  "shipping_address": full address as one string,
  "tracking_number": string,
  "carrier": "UPS" | "USPS" | "FedEx" | "DHL" | "Amazon" | "OnTrac" | other,
  "shipping_status": one of {STATUSES},
  "estimated_delivery": "YYYY-MM-DD",
  "vendor": seller name for botting expenses,
  "expense_category": one of {EXPENSE_CATEGORIES},
  "billing_period": one of {BILLING_PERIODS},
  "description": short description of what was bought (botting expenses)
}}"""

TRACKING_PATTERNS = [
    ("UPS", re.compile(r"\b1Z[0-9A-Z]{16}\b")),
    ("Amazon", re.compile(r"\bTBA\d{12}\b")),
    ("USPS", re.compile(r"\b9[2-5]\d{20}\b")),
    ("USPS", re.compile(r"\b[A-Z]{2}\d{9}US\b")),
]


def find_tracking(text):
    """Return (tracking_number, carrier) for the first well-known format in text."""
    compact = re.sub(r"(?<=\d) (?=\d)", "", text)
    for carrier, pattern in TRACKING_PATTERNS:
        m = pattern.search(compact)
        if m:
            return m.group(0), carrier
    m = re.search(r"fedex[^\n]{0,80}?\b(\d{12}|\d{15})\b", compact, re.I)
    if m:
        return m.group(1), "FedEx"
    return None, None


def carrier_for(tracking):
    if not tracking:
        return None
    for carrier, pattern in TRACKING_PATTERNS:
        if pattern.fullmatch(tracking):
            return carrier
    return None


def looks_like_purchase(email):
    """Cheap pre-pass: skip mail with no purchase wording next to a price or tracking number."""
    text = f"{email.subject}\n{email.body[:20000]}"
    if not PURCHASE_WORDS.search(text):
        return False
    return bool(MONEY.search(text) or find_tracking(text)[0])


def botting_signals(email):
    words = {m.group(0).lower() for m in BOTTING_WORDS.finditer(f"{email.subject}\n{email.body[:6000]}")}
    return sorted(words)[:8]


def _money(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d[\d,]*\.?\d*", str(value))
    return float(m.group(0).replace(",", "")) if m else None


def _date(value):
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%a, %b %d, %Y"):
        try:
            return datetime.strptime(s[:30], fmt).date().isoformat()
        except ValueError:
            pass
    m = re.match(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else None


def _str(value):
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in ("", "null", "none", "n/a", "unknown") else s


def normalize(data, email):
    """Clean the model's output and fill gaps from the email itself."""
    kind = data.get("kind")
    if kind not in ("order", "botting_expense", "other"):
        kind = "other"

    email_day = email.date[:10] if email.date else None
    total = _money(data.get("total"))
    currency = (_str(data.get("currency")) or "USD").upper()[:3]

    if kind == "botting_expense":
        category = data.get("expense_category")
        if category not in EXPENSE_CATEGORIES:
            category = "other"
        period = data.get("billing_period")
        return {
            "kind": kind,
            "vendor": _str(data.get("vendor")) or _str(data.get("retailer")) or _sender_name(email.sender),
            "category": category,
            "description": _str(data.get("description")) or email.subject,
            "amount": total,
            "currency": currency,
            "date": _date(data.get("order_date")) or email_day,
            "order_number": _str(data.get("order_number")),
            "billing_period": period if period in BILLING_PERIODS else None,
        }

    if kind == "other":
        return {"kind": "other"}

    items = []
    for item in data.get("items") or []:
        if isinstance(item, dict) and _str(item.get("name")):
            items.append({
                "name": _str(item.get("name")),
                "quantity": item.get("quantity") if isinstance(item.get("quantity"), (int, float)) else 1,
                "price": _money(item.get("price")),
            })

    tracking = _str(data.get("tracking_number"))
    carrier = _str(data.get("carrier"))
    if tracking:
        tracking = tracking.replace(" ", "").upper()
    else:
        tracking, carrier = find_tracking(email.body)
    carrier = carrier or carrier_for(tracking)

    status = data.get("shipping_status")
    if status not in STATUSES:
        status = "shipped" if tracking else "unknown"

    return {
        "kind": "order",
        "retailer": _str(data.get("retailer")) or _sender_name(email.sender),
        "order_number": _str(data.get("order_number")),
        "order_date": _date(data.get("order_date")) or email_day,
        "items": items,
        "total": total,
        "currency": currency,
        "shipping_address": _str(data.get("shipping_address")),
        "tracking_number": tracking,
        "carrier": carrier,
        "shipping_status": status,
        "estimated_delivery": _date(data.get("estimated_delivery")),
    }


def _sender_name(sender):
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', sender or "")
    if m:
        return m.group(1).strip()
    m = re.search(r"@([\w.-]+)", sender or "")
    return m.group(1) if m else (sender or None)


def extract(llm, email, max_body_chars=12000):
    user = (
        f"From: {email.sender}\nSubject: {email.subject}\nDate: {email.date}\n\n"
        f"{email.body[:max_body_chars]}"
    )
    signals = botting_signals(email)
    if signals:
        user += f"\n\n(Botting-related words found: {', '.join(signals)}. Decide from the content.)"
    return normalize(llm.chat_json(SYSTEM_PROMPT, user), email)
