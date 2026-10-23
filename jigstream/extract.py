"""Turn an email into a structured order or botting expense using the local model."""

import re
from datetime import datetime

STATUSES = [
    "ordered", "processing", "shipped", "in_transit", "out_for_delivery",
    "delivered", "exception", "cancelled", "returned", "refunded", "unknown",
]
# Normal progress order. A status never moves back down this list; cancelled,
# returned, refunded and exception can replace any status.
PROGRESS = ["ordered", "processing", "shipped", "in_transit", "out_for_delivery", "delivered"]


def status_advances(old, new):
    if new in (None, "unknown") or new == old:
        return False
    if old in (None, "unknown") or new not in PROGRESS or old not in PROGRESS:
        return True
    return PROGRESS.index(new) > PROGRESS.index(old)
EXPENSE_CATEGORIES = ["proxy", "bot", "cook_group", "captcha", "server", "aco_service", "account", "other"]
BILLING_PERIODS = ["one_time", "weekly", "monthly", "yearly"]
# Where people resell. Only a hint for the model: a sale from any other marketplace counts too, and you
# can add your own on the Profit goal tab or teach it with the Inbox's "Sale / payout" label.
SELL_PLATFORMS = ["StockX", "GOAT", "eBay", "Tradepost", "Mercari", "Vinted", "Alias"]

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
    r"total|billing|shipped|shipping|delivered|delivery|tracking|cancel+ed|refund(ed)?|return(ed)?|"
    r"sold|payouts?|paid out|earnings|cash ?out)\b",
    re.I,
)

SYSTEM_PROMPT = f"""You read one email and extract purchase data as JSON. Reply with JSON only.

Decide "kind":
- "order": a purchase of physical goods from a store (order confirmation, shipping notice, delivery notice, cancellation, refund, return).
- "botting_expense": a receipt for tools used for retail botting or reselling automation. Judge from what was bought, not who sent it, because new sellers appear all the time. Examples: proxies (residential, ISP, datacenter, per-GB data plans), bot software licenses, keys or renewals, cook group or Discord memberships (often sold through whop.com), captcha solving credits, servers/VPS, ACO (auto-checkout) services, aged or generated accounts.
- "sale": you SOLD something on a resale marketplace or selling app ({", ".join(SELL_PLATFORMS)}, or any other) and are being paid: "you sold", "you made the sale", "your item sold", "payout sent", "you've been paid", "cash out". Buying on eBay or similar is an "order", not a sale.
- "other": anything else: marketing, newsletters, account notices, receipts unrelated to retail or botting. Payment reminders, unpaid or upcoming invoices and "your plan will renew" notices are "other": only money actually charged counts.

For botting expenses, "vendor" is the business that sold the product, not the payment processor. If the receipt comes from Stripe, PayPal, Paddle, Sellix, Shopify, Whop, Gumroad or similar, use the product's or community's own name shown in the receipt (for whop.com, the community or product name). Only use the processor's name if no other seller is named.

JSON keys (use null when unknown, never invent values):
{{
  "kind": "order" | "botting_expense" | "sale" | "other",
  "retailer": store or brand name for orders,
  "order_number": string,
  "order_date": "YYYY-MM-DD",
  "items": [{{"name": string, "quantity": number, "price": number}}],
  "total": number (the amount actually paid, e.g. "Total", "Amount paid", "Amount charged"; for expenses too),
  "currency": "USD" or other ISO code,
  "shipping_address": full address as one string,
  "shipping_name": the name the order ships to (recipient), as written,
  "tracking_number": string,
  "carrier": "UPS" | "USPS" | "FedEx" | "DHL" | "Amazon" | "OnTrac" | other,
  "shipping_status": one of {STATUSES},
  "estimated_delivery": "YYYY-MM-DD",
  "vendor": seller name for botting expenses,
  "expense_category": one of {EXPENSE_CATEGORIES},
  "billing_period": one of {BILLING_PERIODS},
  "description": short description of what was bought (botting expenses),
  "platform": where you sold it, for sales (e.g. "StockX"),
  "sale_price": number, the price the item sold for (sales),
  "fees": number, fees the platform took (sales),
  "payout": number, what you receive after fees (sales),
  "size": string, the item's size if shown (sales)
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


CARRIER_LINK = re.compile(r"(ups|usps|fedex|dhl|ontrac|lasership|narvar|aftership|parcelsapp)\.(com|net|io)", re.I)


def find_tracking_url(text, tracking):
    """A carrier tracking link from the email (mail.py keeps them as [url])."""
    urls = re.findall(r"\[(https?://[^\]\s]+)\]", text or "")
    if tracking:
        for url in urls:
            if tracking in url.replace("%20", "").upper():
                return url
    for url in urls:
        if CARRIER_LINK.search(url.split("/")[2] if url.count("/") >= 2 else url):
            return url
    return None


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


TOTAL_LINE = re.compile(r"(amount (paid|charged|due)|total( paid| charged| due| amount)?|grand total|order total|"
                        r"you paid|payment amount|charged|paid)\b", re.I)
MONEY_VALUE = re.compile(r"(?:(?:US|CA|A)?[$\u00a3\u20ac]\s?(\d[\d,]*(?:\.\d{1,2})?)|"
                         r"\b(\d[\d,]*(?:[.,]\d{2})?)\s?(?:USD|EUR|GBP|CAD|[$\u00a3\u20ac])(?![\w\d])|"
                         r"\b(?:USD|EUR|GBP|CAD)\s?(\d[\d,]*(?:\.\d{1,2})?))")

NOT_A_CHARGE = re.compile(r"\b(reminder|past due|overdue|unpaid|due (soon|on|date)|upcoming (payment|invoice|renewal)|"
                          r"will (renew|be charged)|payment failed|failed payment)\b", re.I)
PAID_WORDS = re.compile(r"\b(receipt|paid|payment (received|successful|confirmed)|thank you for your (payment|purchase|order))\b", re.I)


def _amount_of(m):
    raw = next(g for g in m.groups() if g)
    if re.fullmatch(r"\d{1,3}(\.\d{3})*,\d{2}|\d+,\d{2}", raw):  # 110,00 or 1.234,50 (European)
        raw = raw.replace(".", "").replace(",", ".")
    return float(raw.replace(",", ""))


def find_total(text):
    """The amount paid, read straight from the email when the model didn't give one: the money on or
    just after a Total / Amount paid line (the last such line wins, as subtotals come first)."""
    # Receipt HTML is nested tables; their empty cells leave "| |" lines that would hide the amount
    # sitting a few rows under its label.
    lines = [l.strip(" |") for l in (text or "").splitlines()]
    lines = [l for l in lines if re.search(r"\w", l)]
    found = None
    for i, line in enumerate(lines):
        if not TOTAL_LINE.search(line) or re.search(r"\bsub ?-?total\b", line, re.I):
            continue
        for candidate in (line[TOTAL_LINE.search(line).start():], *lines[i + 1:i + 4]):
            m = MONEY_VALUE.search(candidate)
            if m:
                found = _amount_of(m)
                break
    if found is None:
        amounts = [_amount_of(m) for m in MONEY_VALUE.finditer(text or "")]
        amounts = [a for a in amounts if a > 0]
        if amounts:
            top = max(set(amounts), key=lambda a: (amounts.count(a), a))
            # One price in the whole email, or the price repeated (item, total, paid) that is also the largest.
            if len(set(amounts)) == 1 or (amounts.count(top) >= 2 and top == max(amounts)):
                found = top
    return found


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
    if kind not in ("order", "botting_expense", "sale", "other"):
        kind = "other"

    email_day = email.date[:10] if email.date else None
    total = _money(data.get("total"))
    currency = (_str(data.get("currency")) or "USD").upper()[:3]

    if kind == "botting_expense" and NOT_A_CHARGE.search(email.subject or "") and not PAID_WORDS.search(email.subject or ""):
        kind = "other"  # a reminder or renewal notice, not money spent
    if kind in ("botting_expense", "order") and total is None:
        total = find_total(email.body)

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

    if kind == "sale":
        items = [i for i in data.get("items") or [] if isinstance(i, dict) and _str(i.get("name"))]
        price, fees, payout = _money(data.get("sale_price")), _money(data.get("fees")), _money(data.get("payout"))
        if payout is None and price is not None and fees is not None:
            payout = round(price - fees, 2)
        if price is None and payout is None:
            payout = total if total is not None else find_total(email.body)
        return {
            "kind": "sale",
            "platform": _str(data.get("platform")) or _str(data.get("retailer")) or _sender_name(email.sender),
            "item": _str(items[0]["name"]) if items else _str(data.get("description")),
            "size": _str(data.get("size")),
            "sale_price": price,
            "fees": fees,
            "payout": payout,
            "currency": currency,
            "date": _date(data.get("order_date")) or email_day,
            "order_number": _str(data.get("order_number")),
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
        "shipping_name": _str(data.get("shipping_name")),
        "recipient_email": _recipient(email),
        "tracking_number": tracking,
        "tracking_url": find_tracking_url(email.body, tracking) if tracking else None,
        "carrier": carrier,
        "shipping_status": status,
        "estimated_delivery": _date(data.get("estimated_delivery")),
    }


def _recipient(email):
    """The address the store sent the order email to: the profile's email, even when it forwards to this inbox."""
    m = re.search(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", getattr(email, "to", "") or "")
    return m.group(0).lower() if m else None


def _sender_name(sender):
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', sender or "")
    if m:
        return m.group(1).strip()
    m = re.search(r"@([\w.-]+)", sender or "")
    return m.group(1) if m else (sender or None)


def platforms_named(email, platforms=()):
    """Selling platforms named in the sender or subject, from the built-in list and your own."""
    head = f"{email.sender}\n{email.subject}"
    return [p for p in dict.fromkeys([*SELL_PLATFORMS, *platforms])
            if p and re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", head, re.I)]


def email_prompt(email, max_body_chars=12000, platforms=()):
    user = (
        f"From: {email.sender}\nSubject: {email.subject}\nDate: {email.date}\n\n"
        f"{email.body[:max_body_chars]}"
    )
    signals = botting_signals(email)
    if signals:
        user += f"\n\n(Botting-related words found: {', '.join(signals)}. Decide from the content.)"
    named = platforms_named(email, platforms)
    if named:
        user += (f"\n\n(This comes from a selling platform: {', '.join(named)}. If it says you sold something or "
                 "were paid out, kind is \"sale\"; if you bought something, it's an \"order\".)")
    return user


def extract_raw(llm, email, max_body_chars=12000, examples=(), platforms=()):
    """(the model's raw JSON, the cleaned result)."""
    user = email_prompt(email, max_body_chars, platforms)
    raw = llm.chat_json(SYSTEM_PROMPT, user, examples=examples) if examples else llm.chat_json(SYSTEM_PROMPT, user)
    return raw, normalize(raw, email)


def extract(llm, email, max_body_chars=12000, examples=(), platforms=()):
    return extract_raw(llm, email, max_body_chars, examples, platforms)[1]
