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
# shipping: postage, labels, boxes; supplies: sleeves, toploaders, tape; gas: driving to stores and shows
EXPENSE_CATEGORIES = ["proxy", "bot", "cook_group", "captcha", "server", "aco_service", "account", "shipping", "supplies",
                      "gas", "other"]
BILLING_PERIODS = ["one_time", "weekly", "monthly", "yearly"]
# Where people resell. Only a hint for the model: a sale from any other marketplace counts too, and you
# can add your own on the Profit goal tab or teach it with the Inbox's "Sale / payout" label.
SELL_PLATFORMS = ["StockX", "GOAT", "eBay", "Tradepost", "Mercari", "Vinted", "Alias"]
# Other ways a platform's name is written in senders and subjects ("Trade Post", tradepostapp.com).
SELL_ALIASES = {"Tradepost": r"trade[\s-]?post\w*"}

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
    r"sold|payouts?|paid out|earnings|cash ?out|trade[- ]?ins?|buylist|offer accepted|deposit(ed)?|"
    r"transfer(red)?|store credit)\b",
    re.I,
)

SYSTEM_PROMPT = f"""You read one email and extract purchase data as JSON. Reply with JSON only.

Decide "kind":
- "order": a purchase of physical goods from a store (order confirmation, shipping notice, delivery notice, cancellation, refund, return).
- "botting_expense": a receipt for tools used for retail botting or reselling automation. Judge from what was bought, not who sent it, because new sellers appear all the time. Examples: proxies (residential, ISP, datacenter, per-GB data plans), bot software licenses, keys or renewals, cook group or Discord memberships (often sold through whop.com), captcha solving credits, servers/VPS, ACO (auto-checkout) services, aged or generated accounts.
- "sale": you SOLD something on a resale marketplace or selling app ({", ".join(SELL_PLATFORMS)}, or any other) and are being paid: "you sold", "you made the sale", "your item sold", "payout sent", "you've been paid", "cash out". Selling or trading in to a buyback app or shop is a sale too (Tradepost, a card shop's buylist): "trade-in complete", "offer accepted", "we received your cards", "payment sent", "store credit added"; the amount you get is the payout. Buying on eBay or similar is an "order", not a sale.
- "other": anything else: marketing, newsletters, account notices, receipts unrelated to retail or botting. Payment reminders, unpaid or upcoming invoices and "your plan will renew" notices are "other": only money actually charged counts.

For orders, a cancellation email ("your order was cancelled", "we couldn't process your order") is kind "order" with shipping_status "cancelled", and its order_number is the order it cancels. Copy the order number exactly as written.

For botting expenses, "vendor" is the business that sold the product, not the payment processor. If the receipt comes from Stripe, PayPal, Paddle, Sellix, Shopify, Whop, Gumroad or similar, use the product's or community's own name shown in the receipt (for whop.com, the community or product name). Only use the processor's name if no other seller is named.

JSON keys. Leave out any key that is unknown or doesn't apply to the kind, and never invent values; for kind "other" reply {{"kind": "other"}} and nothing more:
{{
  "kind": "order" | "botting_expense" | "sale" | "other",
  "retailer": store or brand name for orders,
  "order_number": string,
  "order_date": "YYYY-MM-DD",
  "items": [{{"name": string, "quantity": number, "price": number}}] (only what was bought in this order; never products from "you may also like", "recommended for you", "recently viewed" or other suggestions),
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
    m = re.search(r"-?\d[\d,.]*", str(value))
    if not m:
        return None
    raw = m.group(0).rstrip(".,")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})*,\d{2}|-?\d+,\d{2}", raw):  # 45,00 or 1.234,50 (European)
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def amounts_in(text):
    """Every money amount written in the email."""
    return {round(_amount_of(m), 2) for m in MONEY_VALUE.finditer(text or "")}


def check_cents(price, text):
    """A price the email only shows with a decimal point in it: 4500 read from $45.00."""
    if price is None:
        return None
    shown = amounts_in(text)
    return round(price / 100, 2) if shown and round(price, 2) not in shown and round(price / 100, 2) in shown else price


def check_total(total, text):
    """The model's total held against the email it read. A total the email never shows is a misread: most often
    the cents run together with the dollars ($45<sup>00</sup> read as 4500), so the same number with the decimal
    put back wins when the email shows it; otherwise the email's own Total line. When the email shows no money at
    all (or the total), the model's answer stands."""
    if total is None:
        return None
    shown = amounts_in(text)
    if not shown or round(total, 2) in shown:
        return total
    if round(total / 100, 2) in shown:
        return round(total / 100, 2)
    found = find_total(text)
    return found if found is not None else total


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


# Cancel and refund emails, read from the email itself so the model's guess can't miss them. A store's
# "cancel anytime" or "how to cancel" footer, or one item of several cancelled, isn't a cancelled order.
CANCEL_SUBJECT = re.compile(r"\bcancel+(ed|ation)\b|\bcancel+ing your order\b|\bwe('ve| have) cancel+ed\b|"
                            r"\b(order|purchase) (could ?n.t|could not|was unable to|can.t|cannot) be (processed|fulfilled|completed)\b",
                            re.I)
CANCEL_BODY = re.compile(r"\b(your |this )?(order|purchase)( #?\s?[\w-]+)? (has been|was|is|got|have been) cancel+ed\b|"
                         r"\bwe('ve| have) cancel+ed your (order|purchase)\b|\border cancel+ation\b|"
                         r"\bcancel+ation (confirmation|notice)\b|\byour order is cancel+ed\b", re.I)
PARTIAL_CANCEL = re.compile(r"\b(some|one|an?) (of (your|the) )?items?\b[^.\n]{0,40}\bcancel|\bpartial(ly)? cancel|"
                            r"\bitems? (were|was|have been|has been) cancel+ed\b", re.I)
NOT_CANCEL = re.compile(r"\bcancel+(ed)? (any ?time|at any time)\b|\b(how|want|wish|like|need) to cancel\b|"
                        r"\bcancel your (subscription|membership)\b|\brequest(ed)? (a |to )?cancel", re.I)
REFUND_SUBJECT = re.compile(r"\brefund(ed| issued| processed| confirmation| complete| on the way)?\b", re.I)
ORDER_NUMBER = re.compile(r"\border\s*(?:number|no\.?|num|id)?\s*[:#]?\s*#?\s*([A-Z0-9][A-Z0-9-]{4,})\b", re.I)


def cancel_signal(email):
    """'cancelled', 'refunded' or None, from what the email itself says (subject first, then its opening)."""
    subject, opening = email.subject or "", (email.body or "")[:1500]
    if PARTIAL_CANCEL.search(subject) or (not CANCEL_SUBJECT.search(subject) and PARTIAL_CANCEL.search(opening)):
        return None
    if CANCEL_SUBJECT.search(subject) and not NOT_CANCEL.search(subject):
        return "cancelled"
    if CANCEL_BODY.search(opening) and not NOT_CANCEL.search(CANCEL_BODY.search(opening).group(0)):
        return "cancelled"
    if REFUND_SUBJECT.search(subject) and not re.search(r"\brefund (policy|request)\b", subject, re.I):
        return "refunded"
    return None


def find_order_number(email):
    for text in (email.subject or "", (email.body or "")[:3000]):
        m = ORDER_NUMBER.search(text)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1)
    return None


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
    if kind in ("botting_expense", "order"):
        total = find_total(email.body) if total is None else check_total(total, email.body)

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

    signal = cancel_signal(email) if kind in ("order", "other") else None
    if kind == "other" and signal == "cancelled" and (data.get("order_number") or find_order_number(email)) \
            and re.search(r"\b(order|purchase)\b", f"{email.subject}\n{(email.body or '')[:1500]}", re.I) \
            and not re.search(r"\b(subscription|membership|plan|trial)\b", email.subject or "", re.I):
        kind = "order"  # an order cancellation the model filed as a plain notice
    if kind == "other":
        return {"kind": "other"}

    items = []
    for item in data.get("items") or []:
        if isinstance(item, dict) and _str(item.get("name")):
            items.append({
                "name": _str(item.get("name")),
                "quantity": item.get("quantity") if isinstance(item.get("quantity"), (int, float)) else 1,
                "price": check_cents(_money(item.get("price")), email.body),
            })

    items = drop_suggested(items, email.body)

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
    if signal == "cancelled" or (signal == "refunded" and status not in ("returned", "refunded")):
        status = signal  # the email says so outright; the model often calls a cancel email "ordered"

    return {
        "kind": "order",
        "retailer": _str(data.get("retailer")) or _sender_name(email.sender),
        "order_number": _str(data.get("order_number")) or find_order_number(email),
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
    """The address the store sent the order email to: the profile's email, even when it forwards to this
    inbox (the forwarding inbox itself is skipped)."""
    inbox = (getattr(email, "account", "") or "").lower()
    for a in re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", getattr(email, "to", "") or ""):
        if a.lower() != inbox:
            return a.lower()
    return None


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
            if p and re.search(rf"(?<![a-z0-9])(?:{SELL_ALIASES.get(p, re.escape(p))})(?![a-z0-9])", head, re.I)]


FOOTER = re.compile(r"\bunsubscribe\b|\bprivacy (policy|notice)\b|\bmanage (your )?(email )?preferences\b|"
                    r"\ball rights reserved\b|\bthis (e-?mail|message) was sent to\b", re.I)


# Store emails end with products they'd like to sell you; those are never the order's items.
RECS = re.compile(r"^\W*(you (may|might) (also )?(like|love)|recommended (for you|products|items)|(customers|shoppers|guests) "
                  r"(also|who)|(based on|inspired by) your|recently viewed|(more|items|picks|picked|top picks) (for|to) "
                  r"(you|love|explore)|frequently bought|similar (items|products)|shop (similar|more|the look)|"
                  r"(trending|popular) (now|items|products)|complete the look|related (items|products)|don't forget|"
                  r"still (thinking|interested)|deals (for|picked for) you|our picks|more to (love|explore))\b", re.I)
# What ends a suggestions block: the order's own details coming back.
ORDER_LINE = re.compile(r"\b(order|subtotal|total|tax|ship(ping|ped|s)? to|deliver(y|ing)|address|payment|paid|"
                        r"tracking|qty|quantity|arriv)\b", re.I)
REC_LINES = 60


def split_recommendations(text):
    """(the email without its suggestion blocks, the text of those blocks)."""
    keep, recs, left = [], [], 0
    for line in (text or "").splitlines():
        if RECS.search(line) and len(line) < 80:
            left = REC_LINES
            recs.append(line)
            continue
        if left and not ORDER_LINE.search(line):
            left -= 1
            recs.append(line)
            continue
        left = 0
        keep.append(line)
    return "\n".join(keep), "\n".join(recs)


def _name_key(name):
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"(\.\.\.|…)\s*$", "", (name or "").lower())).strip()[:30]


def truncated(name):
    return bool(re.search(r"(\.\.\.|…)\s*$", name or ""))


def drop_suggested(items, body=None):
    """The order's items without products the email only suggested: names found only in its suggestion blocks,
    and cut-off names (ending in "...") listed beside full ones, which is how store carousels show them."""
    if body:
        main, recs = split_recommendations(body)
        main_k = re.sub(r"[^a-z0-9]+", " ", main.lower())
        recs_k = re.sub(r"[^a-z0-9]+", " ", recs.lower())
        items = [i for i in items if not (_name_key(i["name"]) and _name_key(i["name"]) in recs_k
                                          and _name_key(i["name"]) not in main_k)]
    full = [i for i in items if not truncated(i.get("name"))]
    if full and len(full) < len(items):
        items = full
    return items


def compact(body):
    """The email text with what costs the model time but tells it nothing taken out: the empty cells and
    spacer rows receipt HTML leaves behind, repeated lines, and the legal footer at the end. Every token
    counts on a local model: it reads the whole prompt before it can answer."""
    lines, last = [], None
    for line in (body or "").splitlines():
        line = re.sub(r"(\s*\|)+\s*", " | ", line).strip(" |")
        line = re.sub(r"[ \t]{2,}", " ", line)
        # A repeated line with a number in it may be a second of the same item, so only wording repeats go.
        if not re.search(r"\w", line) or (line == last and not re.search(r"\d", line)):
            continue
        lines.append(line)
        last = line
    text = split_recommendations("\n".join(lines))[0]
    # Footers come after the order details; only cut one that starts in the back half of a long email.
    m = next((m for m in FOOTER.finditer(text) if m.start() > max(2000, len(text) // 2)), None)
    if m:
        text = text[:text.rfind("\n", 0, m.start()) + 1 or m.start()].rstrip()
    return text


def trim(text, limit):
    """At most `limit` characters: the start (items, order number) and the end (totals, address) of a long email."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    return text[:head].rstrip() + "\n[...]\n" + text[-(limit - head - 7):].lstrip()


def email_prompt(email, max_body_chars=12000, platforms=()):
    user = (
        f"From: {email.sender}\nSubject: {email.subject}\nDate: {email.date}\n\n"
        f"{trim(compact(email.body), max_body_chars)}"
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
