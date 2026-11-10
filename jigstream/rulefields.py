"""Read an order email's fields by rule, without the model.

Store emails are laid out the same way every time: a label ("Order #", "Subtotal", "Shipping address") and its
value on the same line, on the next line, or (in column layouts) a run of labels followed by a run of values in
the same order. read() walks the email's lines that way and returns what it finds:

    order_number, total, subtotal, shipping, tax, discount, sum_ok (subtotal + shipping + tax - discount equals
    the total), name, street, city, state, zip, address, tracking, carrier, cancel ("full", "items", "refund" or
    None).

Every value is copied from the email's own text, so it can't be made up. A field the rules can't find is None and
the model's answer stands for it (see extract.normalize).
"""
import re

# Characters mail HTML uses for spacing or direction marks: zero-width, soft hyphen, combining grapheme joiner, and
# the right-to-left marks Amazon puts in front of order numbers.
INVISIBLE = re.compile("[­͏​-‏‪-‮⁠﻿]")
URL = re.compile(r"\[(?:https?|mailto):[^\]\s]*\]|\bhttps?://\S+")


def lines_of(text):
    """The email's text lines, cleaned: no table bars, links or invisible characters, no empty lines."""
    out = []
    for line in INVISIBLE.sub("", URL.sub(" ", text or "")).splitlines():
        line = re.sub(r"\s*\|\s*", " ", line).replace("\xa0", " ")
        line = re.sub(r"\s+", " ", line).strip(" :")
        if re.search(r"\w", line):
            out.append(line)
    return out


# ---------------------------------------------------------------- order number

ORDER_LABEL = re.compile(r"\b(?:order|confirmation|purchase)\s*(?:number|no\.?|num(?:ber)?|id|#)?\s*[:#]?\s*", re.I)
# A store prefix with its own # (ABC#12345), or a run of capitals, digits and dashes. Kept case-sensitive so a
# word glued on after the number ("...1234are about to ship") isn't taken as part of it.
NUMBER = re.compile(r"#?\s*((?:[A-Z]{2,5}#)?[A-Z0-9][A-Z0-9-]{3,}[A-Z0-9])(?![A-Z0-9-])")
# Formats that are order numbers wherever they appear.
SHAPES = [
    re.compile(r"\b(\d{3}-\d{7}-\d{7})\b"),  # Amazon
    re.compile(r"(?<![\d-])(\d{7}-\d{8})(?![\d-])"),  # Walmart
]
NOT_NUMBER = re.compile(r"^(?:\d{1,4}|(?:19|20)\d\d-\d\d-\d\d)$")


def _number_after(text):
    m = NUMBER.match(text)
    if not m:
        return None
    value = m.group(1)
    if not re.search(r"\d", value) or NOT_NUMBER.match(value):
        return None
    return value


def find_order_number(subject, lines):
    """The order number: after an Order / Order number / Confirmation label (on the label's line or the next
    one), or a number in a store's own order-number format."""
    for text in [subject or ""] + lines[:120]:
        for m in ORDER_LABEL.finditer(text):
            before = text[max(0, m.start() - 12):m.start()].lower()
            if re.search(r"(ending in|last \d)\s*$", before) or re.match(r"\s*ending in", text[m.end():], re.I):
                continue
            value = _number_after(text[m.end():])
            if value:
                return value
    for i, line in enumerate(lines[:120]):
        if ORDER_LABEL.fullmatch(line.strip()) and i + 1 < len(lines):
            value = _number_after(lines[i + 1])
            if value:
                return value
    for shape in SHAPES:
        for text in [subject or ""] + lines[:200]:
            m = shape.search(text)
            if m:
                return m.group(1)
    return None


# ---------------------------------------------------------------- money

MONEY_ONLY = re.compile(r"^[-–(]?\s*(?:US|CA|A)?\s?[$£€]\s?(-?\d[\d,]*(?:\.\d{1,2})?)\s*(?:USD|CAD|EUR|GBP)?\)?$"
                        r"|^(-?\d[\d,]*\.\d{2})\s*(?:USD|CAD|EUR|GBP)$", re.I)
MONEY_IN = re.compile(r"(?:US|CA|A)?\s?[$£€]\s?(-?\d[\d,]*(?:\.\d{1,2})?)(?![\d/])|"
                      r"\b(\d[\d,]*\.\d{2})\s*(?:USD|CAD|EUR|GBP)\b")

_LABEL_TAIL = r"[\w\s().,&:/#*-]{0,24}"
LABELS = [
    ("subtotal", re.compile(rf"^(?:order |item(?:s|\(s\))? |merchandise |refund )?sub ?-?total{_LABEL_TAIL}$|"
                            rf"^(?:item(?:s|\(s\))?|merchandise) total{_LABEL_TAIL}$", re.I)),
    ("shipping", re.compile(rf"^(?:estimated |standard )?(?:shipping|delivery|s&h|shipping (?:&|and) handling)"
                            rf"(?: fees?| charges?| cost)?(?: \([^)]*\))?$", re.I)),
    ("tax", re.compile(rf"^[\w\s&.-]{{0,40}}\b(?:tax(?:es)?|vat|gst|hst)\b(?! included){_LABEL_TAIL}$", re.I)),
    ("discount", re.compile(rf"^(?:discounts?|promo(?:tion)?s?|coupons?|savings|you saved|order discount){_LABEL_TAIL}$",
                            re.I)),
    ("total", re.compile(rf"^(?:order |grand |estimated |payment |final )?total(?: paid| charged| due| amount)?"
                         rf"(?: \([^)]*\))?$|^amount (?:paid|charged)$", re.I)),
]
LABEL_INLINE = re.compile(r"^(.*?)\s*[-–(]?\s*(?:US|CA|A)?\s?[$£€]\s?(-?\d[\d,]*(?:\.\d{1,2})?)\s*(?:USD|CAD)?\)?$",
                          re.I)


def _amount(raw):
    return round(abs(float(raw.replace(",", ""))), 2)


def _money_only(line):
    m = MONEY_ONLY.match(line)
    return _amount(next(g for g in m.groups() if g)) if m else None


def _label(text):
    text = text.strip(" :*")
    if len(text) > 48 or re.search(r"\bincludes?\b|\bbased on\b|\bper\b|\beach\b|/ ?ea\b", text, re.I):
        return None
    for kind, pattern in LABELS:
        if pattern.match(text):
            return kind
    return None


def find_amounts(lines):
    """Subtotal, shipping, tax, discount and total from the order summary. Starts at the first Subtotal label and
    pairs each label with the next amount below it, oldest label first, so both "Shipping / $5.99" and a
    column of labels followed by a column of amounts read right. Several tax lines (state and county) add up."""
    found = {}
    start = next((i for i, l in enumerate(lines) if _label(l) == "subtotal" or
                  (LABEL_INLINE.match(l) and _label(LABEL_INLINE.match(l).group(1)) == "subtotal")), None)
    if start is None:
        return found
    pending = []

    def put(kind, value):
        if kind == "tax":
            found["tax"] = round(found.get("tax", 0) + value, 2)
        elif kind not in found:
            found[kind] = value

    for line in lines[start:start + 60]:
        money = _money_only(line)
        if money is not None:
            if pending:
                put(pending.pop(0), money)
                if "total" in found:
                    break
            continue
        inline = LABEL_INLINE.match(line)
        kind = _label(inline.group(1)) if inline else None
        if kind:
            put(kind, _amount(inline.group(2)))
            if kind == "total":
                break
            continue
        kind = _label(line)
        if kind and kind not in pending:
            pending.append(kind)
        elif not kind and pending and len(line) > 60:
            pending = []  # a sentence: the summary ended
    return found


def find_total(lines, body):
    from .extract import find_total as total_line
    return total_line("\n".join(lines)) if lines else total_line(body)


def sums_up(parts, total):
    if total is None or "subtotal" not in parts:
        return False
    whole = parts["subtotal"] + parts.get("shipping", 0) + parts.get("tax", 0) - parts.get("discount", 0)
    return abs(round(whole, 2) - total) < 0.015


# ---------------------------------------------------------------- ship-to address

SHIP_LABEL = re.compile(r"^(?:shipping address|ship(?:ping)? to|deliver(?:s|ing)? to|delivery address|"
                        r"recipient|shipped to|sending to)\b\s*:?\s*(.*)$", re.I)
STOP = re.compile(r"^(?:billing|bill(?:ed)? to|payment|shipping method|delivery method|order|items?\b|"
                  r"contact|phone|tel\b|email|qty|track|united states|usa|us|canada|estimated|arriv|\$)", re.I)
STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
          "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR").split()
STATE_NAMES = ("alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho "
               "illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota "
               "mississippi missouri montana nebraska nevada new_hampshire new_jersey new_mexico new_york "
               "north_carolina north_dakota ohio oklahoma oregon pennsylvania rhode_island south_carolina "
               "south_dakota tennessee texas utah vermont virginia washington west_virginia wisconsin wyoming")
STATE = rf"(?:{'|'.join(n.replace('_', ' ') for n in STATE_NAMES.split())}|{'|'.join(STATES)})"
STATE_ZIP = re.compile(rf"^(?P<state>{STATE}),?\s+(?P<zip>\d{{5}})(?:-\d{{4}})?$", re.I)
CITY_LINE = re.compile(rf"^(?P<city>[A-Za-z][A-Za-z .'-]{{1,40}}?),?\s+(?:(?P<state>{STATE})\b,?\s*)?"
                       rf"(?P<zip>\d{{5}})(?:-\d{{4}})?(?:,?\s*(?:USA?|United States))?$", re.I)
STREET = re.compile(r"^\d+[A-Za-z]?\s+\S.*$")
UNIT = re.compile(r"^(?:apt|apartment|unit|ste|suite|#|bldg|building|fl|floor|lot|rm|room)\b\.?\s*\S+", re.I)
DELIVERY_WORDS = re.compile(r"\b(deliver(y|s|ing)?|arriv(es|ing)|ship(ping|s)?)\b", re.I)


def _parse_parts(parts):
    """Name, street, city, state and ZIP from an address's parts (lines, or a comma-split single line)."""
    name = street = city = state = zipc = None
    street_parts = []
    for part in parts:
        part = part.strip(" ,")
        if not part:
            continue
        m = STATE_ZIP.match(part)
        if m and street_parts:
            state, zipc = m.group("state"), m.group("zip")
            break
        m = CITY_LINE.match(part)
        if m and street_parts:
            city, state, zipc = m.group("city").strip(), m.group("state"), m.group("zip")
            break
        if re.fullmatch(r"\d{5}(?:-\d{4})?", part) and street_parts:
            zipc = part[:5]
            break
        if re.fullmatch(STATE, part, re.I) and street_parts:
            state = part
            continue
        if STREET.match(part) or (street_parts and UNIT.match(part)):
            street_parts.append(part)
        elif street_parts and city is None and re.fullmatch(r"[A-Za-z .'-]{2,40}", part):
            city = part
        elif not street_parts and name is None and not re.search(r"\d", part) and len(part.split()) <= 5:
            name = part
        elif street_parts:
            break
    street = ", ".join(street_parts) or None
    return name, street, city, state, zipc


def _from_block(block):
    if len(block) == 1 and "," in block[0]:
        return _parse_parts(block[0].split(","))
    parts = []
    for line in block:
        # "Testville, TX 75001" stays one part; "Pat Example, 12 Sample Road, ..." is split.
        if "," in line and not CITY_LINE.match(line):
            parts.extend(line.split(","))
        else:
            parts.append(line)
    return _parse_parts(parts)


def find_address(lines):
    """The ship-to name and address: the lines under a Shipping address / Ship to / Delivers to label, or else an
    address line under a Delivery / Arrives line. Footer addresses (the store's own) are never read."""
    from .extract import FOOTER
    end = next((i for i, l in enumerate(lines) if FOOTER.search(l) or l.startswith(("©", "(c)"))), len(lines))
    body = lines[:end]
    best = None
    for i, line in enumerate(body):
        m = SHIP_LABEL.match(line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest and re.match(r"(?:home|store|me)\b", rest, re.I):
            continue  # "Ship to Home (4 items)" is a delivery method
        block = [rest] if rest else []
        for nxt in body[i + 1:i + 8]:
            if STOP.match(nxt) or SHIP_LABEL.match(nxt) or len(nxt) > 90:
                break
            block.append(nxt)
        parsed = _from_block(block)
        if parsed[1] or parsed[4]:
            if parsed[1] and parsed[4]:
                return parsed
            best = best or parsed
    for i, line in enumerate(body):
        if "," in line and STREET.match(line) and re.search(r"\b\d{5}\b", line) and \
                any(DELIVERY_WORDS.search(l) for l in body[max(0, i - 6):i]):
            parsed = _parse_parts(line.split(","))
            if parsed[1] and parsed[4]:
                return parsed
    return best or (None,) * 5


# ---------------------------------------------------------------- cancels

ITEM_CANCEL = re.compile(r"\bitems? (?:in|from) (?:your|the) order (?:was|were|has been|have been) cancel+ed\b|\bcancel+ed (?:an?|one|some|the|\d+|your)? ?items?\b|\bitems? (?:\(s\) )?(?:were |was |have been |has been )?"
                         r"cancel+ed\b|\bitem\(s\) cancel+ed\b|\byour items? (?:has|have) been\s+cancel+ed\b", re.I)
WHOLE_ORDER = re.compile(r"\b(?:your|this|the) order (?:has been|was|is) cancel+ed\b|"
                         r"\byou (?:have not|haven.t) been charged for this order\b", re.I)
REST_COMING = re.compile(r"\b(?:rest of (?:your|the) order|remaining items?|other items? (?:in your order )?(?:will|are|is)|"
                         r"still (?:be )?(?:ship|on (?:its|the) way|arriv))", re.I)


def find_cancel(subject, body):
    """'full' (the order is cancelled), 'items' (the email cancels some items), 'refund' or None."""
    from .extract import cancel_signal
    from types import SimpleNamespace
    signal = cancel_signal(SimpleNamespace(subject=subject or "", body=body or ""))
    head = f"{subject}\n{(body or '')[:2000]}"
    if signal == "cancelled":
        if ITEM_CANCEL.search(head) and not WHOLE_ORDER.search(head):
            return "items"
        return "full"
    if signal == "refunded":
        return "refund"
    if ITEM_CANCEL.search(head):
        return "items"
    return None


# ---------------------------------------------------------------- all fields

def read(email):
    from .extract import find_tracking, carrier_for
    subject, body = email.subject or "", email.body or ""
    lines = lines_of(body)
    out = {"order_number": find_order_number(INVISIBLE.sub("", subject), lines)}
    parts = find_amounts(lines)
    total = find_total(lines, body)
    if total is None and parts.get("total") is not None:
        total = parts["total"]
    if "total" in parts and parts["total"] != total and sums_up(parts, parts["total"]):
        total = parts["total"]  # the summary's own Total, which its lines add up to
    out.update(total=total, subtotal=parts.get("subtotal"), shipping=parts.get("shipping"), tax=parts.get("tax"),
               discount=parts.get("discount"), sum_ok=sums_up(parts, total))
    name, street, city, state, zipc = find_address(lines)
    out.update(name=name, street=street, city=city, state=state, zip=zipc,
               address=", ".join(p for p in (street, city, " ".join(x for x in (state, zipc) if x)) if p) or None)
    tracking, carrier = find_tracking(body)
    out.update(tracking=tracking, carrier=carrier or carrier_for(tracking))
    out["cancel"] = find_cancel(subject, body)
    return out
