"""Hold the model's order fields against the email it read.

A small local model sometimes writes an order number, tracking number, name or address that the email never
shows: one copied from a training example, a number it misread, or the order number given as the tracking
number. Each field is checked against the email text, and a value the email doesn't back up is replaced by what
the email's own text says, or left empty. The total is also checked against the email's Subtotal, Shipping, Tax
and Discount lines when the model took the subtotal for the total.
"""
import logging
import re

from . import tracknum

logger = logging.getLogger("jigstream.grounding")
ENABLED = True  # tests of other features (fake models answering short made-up emails) turn it off


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def shown(value, text):
    """True when the email shows value, ignoring spaces, dashes, # signs and case."""
    v = _squash(value)
    return bool(v) and v in _squash(text)


def _words(value):
    return [w for w in re.findall(r"[a-z0-9]+", (value or "").lower()) if len(w) > 1]


def name_shown(name, text):
    """Most of the name's words are in the email (a middle initial or a nickname may differ)."""
    words = _words(name)
    if not words:
        return False
    low = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return sum(w in low for w in words) * 2 >= len(words)


def address_shown(address, text):
    """The address's ZIP code and house number are both in the email."""
    zips = re.findall(r"\b\d{5}\b(?:-\d{4})?", address or "")
    house = re.match(r"\s*(\d+)", address or "")
    low = text or ""
    if zips and not re.search(rf"\b{zips[-1][:5]}\b", low):
        return False
    if house and not re.search(rf"\b{house.group(1)}\b", low):
        return False
    return bool(zips or house) or name_shown(address, text)


LINE = r"[^\n\d$]{0,25}[$£€]?\s?(-?\d[\d,]*\.\d{2})"
SUBTOTAL = re.compile(rf"\b(sub ?total|merchandise|items? total|item\(s\) subtotal){LINE}", re.I)
SHIPPING = re.compile(rf"\b(shipping|delivery|s&h|handling)(?! address| to\b){LINE}", re.I)
TAX = re.compile(rf"\b(estimated tax|sales tax|tax(es)?|vat|gst|hst){LINE}", re.I)
DISCOUNT = re.compile(rf"\b(discount|promo(tion)?|coupon|savings|gift card){LINE}", re.I)


def _first(pattern, text):
    m = pattern.search(text)
    return round(abs(float(m.group(m.lastindex).replace(",", ""))), 2) if m else None


def summed_total(text):
    """Subtotal + shipping + tax - discount from the email's own lines, when it shows a subtotal and a tax or
    shipping line. None otherwise."""
    text = text or ""
    sub = _first(SUBTOTAL, text)
    ship, tax, off = _first(SHIPPING, text), _first(TAX, text), _first(DISCOUNT, text)
    if sub is None or (ship is None and tax is None):
        return None
    return round(sub + (ship or 0) + (tax or 0) - (off or 0), 2)


def check_sum(total, items, text):
    """The model gave the subtotal (or the items' sum) as the total although the email adds shipping and tax on
    top and shows that sum: the sum wins. Any other total stands."""
    from .extract import amounts_in
    if total is None:
        return total
    whole = summed_total(text)
    if whole is None or whole == round(total, 2) or whole not in amounts_in(text):
        return total
    item_sum = round(sum((i.get("price") or 0) * (i.get("quantity") or 1) for i in items or []), 2)
    if round(total, 2) in (_first(SUBTOTAL, text), item_sum):
        return whole
    return total


def ground_order(result, email):
    """result: a normalized order. Returns it with unbacked fields fixed, and logs which fields changed (never
    their values: logs can be sent to the developer)."""
    if not ENABLED:
        return result
    from .extract import find_order_number, find_tracking, find_tracking_url
    text = f"{email.subject or ''}\n{email.body or ''}"
    fixed = []

    number = result.get("order_number")
    if number and not shown(number, text):
        result["order_number"] = find_order_number(email)
        fixed.append("order number" + (" (taken from the email)" if result["order_number"] else " (dropped)"))

    tracking = result.get("tracking_number")
    if tracking:
        valid = tracknum.identify(tracking)[0]
        if not shown(tracking, text) and not valid:
            found, carrier = find_tracking(email.body or "")
            fixed.append("tracking number" + (" (taken from the email)" if found else " (dropped)"))
            result.update(tracking_number=found, carrier=carrier if found else None,
                          tracking_url=find_tracking_url(email.body, found) if found else None)
        elif not valid:
            # In the email but not a real tracking number (often the order number): take a valid one if shown.
            better = [n for n, _ in tracknum.find_all(email.body or "") if n != tracking]
            if better and _squash(tracking) == _squash(result.get("order_number")):
                found, carrier = better[0], tracknum.identify(better[0])[0]
                fixed.append("tracking number (was the order number)")
                result.update(tracking_number=found, carrier=carrier,
                              tracking_url=find_tracking_url(email.body, found))

    name = result.get("shipping_name")
    if name and not name_shown(name, text):
        result["shipping_name"] = None
        fixed.append("ship-to name (dropped)")

    address = result.get("shipping_address")
    if address and not address_shown(address, text):
        result["shipping_address"] = None
        fixed.append("ship-to address (dropped)")

    total = check_sum(result.get("total"), result.get("items"), email.body)
    if total != result.get("total"):
        fixed.append("total (was the subtotal; now subtotal + shipping + tax)")
        result["total"] = total

    if fixed:
        logger.info("fields the email didn't back up: %s", "; ".join(fixed))
    return result
