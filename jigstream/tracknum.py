"""Tracking numbers for every carrier in jkeen/tracking_number_data (MIT, see carrierdata/LICENSE), with check digits.

The data file lists each carrier's number formats and how its check digit is worked out. A number only counts
when its check digit is right, so an order number or phone number that happens to be the right length is not
taken for a tracking number. Short all-digit formats (FedEx 12 and 15, DHL Express, ...) and carriers outside the
US also need a word like "tracking" or the carrier's name just before them.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).with_name("carrierdata") / "couriers.json"

NAMES = {"ups": "UPS", "usps": "USPS", "fedex": "FedEx", "dhl": "DHL", "amazon": "Amazon", "ontrac": "OnTrac",
         "lasership": "LaserShip", "canadapost": "Canada Post"}
# Formats long or distinct enough to stand on their own. The rest need a tracking word next to them (a bare
# 20-digit number passes the USPS check one time in ten, so those need one too).
STRONG = {"ups", "amazon_logistics", "ontrac_c", "ontrac_d", "lasership_lx", "lasership_1ls7", "lasership_1lscx",
          "s10", "usps_impb_n", "usps_impb_c", "fedex_34", "fedex_ground_96",
          "fedex_astra_32"}
CONTEXT = re.compile(r"track|waybill|courier|carrier|\b(ups|usps|fedex|dhl|ontrac|lasership|amazon|purolator|canpar|"
                     r"canada post|yodel|dpd|landmark|speedee|gofo|yunexpress)\b", re.I)
CONTEXT_CHARS = 60
# "Order #1234..." is the order number, whatever format it happens to fit.
ORDER_LABEL = re.compile(r"\b(order|invoice|confirmation|reference|account|phone)\s*(number|no\.?|num|id)?\s*[:#]?\s*#?\s*$", re.I)


def _pattern(regex):
    regex = "".join(regex) if isinstance(regex, list) else regex
    regex = re.sub(r"\(\?<([A-Za-z]\w*)>", r"(?P<\1>", regex)  # Ruby named groups to Python
    # The data allows any whitespace between characters; in an email only a single space or tab may split one.
    regex = regex.replace(r"\s]", " \t]").replace(r"\s*", "[ \t]?").replace(r"\s", "[ \t]")
    return regex


@lru_cache(maxsize=1)
def formats():
    out = []
    for courier in json.loads(DATA.read_text()):
        for fmt in courier["tracking_numbers"]:
            body = _pattern(fmt["regex"])
            out.append({
                "id": fmt.get("id") or fmt["name"],
                "code": courier["courier_code"],
                "carrier": NAMES.get(courier["courier_code"], courier["name"]),
                "full": re.compile(body, re.I),
                # Not a piece of a longer number, even one written in groups ("9400 1000 0000 ...").
                "search": re.compile(rf"(?<![A-Za-z0-9])(?<![0-9][ \t])(?:{body})(?![A-Za-z0-9])(?![ \t][0-9])"),
                "validation": fmt.get("validation") or {},
                "additional": fmt.get("additional") or [],
                "url": fmt.get("tracking_url"),
                "tests": fmt.get("test_numbers") or {},
            })
    return out


def _clean(s):
    return re.sub(r"\s", "", s or "")


def _mod10(seq, check, odds=None, evens=None, reverse=False, **_):
    total = 0
    for i, c in enumerate(reversed(seq) if reverse else seq):
        x = int(c) if c.isdigit() else (ord(c) - 3) % 10
        if odds and i % 2 == 1:
            x *= int(odds)
        elif evens and i % 2 == 0:
            x *= int(evens)
        total += x
    want = total % 10
    want = 10 - want if want else 0
    return str(want) == check


def _luhn(seq, check):
    total = 0
    for i, c in enumerate(reversed(seq)):
        x = int(c)
        if i % 2 == 0:
            x *= 2
            x = x - 9 if x > 9 else x
        total += x
    return str((10 - total % 10) % 10) == check


def _s10(seq, check):
    total = sum(int(a) * b for a, b in zip(seq, [8, 6, 4, 2, 3, 5, 9, 7]))
    r = total % 11
    want = 0 if r == 1 else 5 if r == 0 else 11 - r
    return str(want) == check


def _weighted(seq, check, weightings, modulo1, modulo2, **_):
    total = sum(int(a) * b for a, b in zip(seq, weightings))
    return str(total % modulo1 % modulo2) == check


def _mod37_36(seq, check):
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    mod, cd = 36, 36
    for ch in seq.upper():
        cd += alphabet.index(ch) if ch in alphabet else 0
        if cd > mod:
            cd -= mod
        cd *= 2
        if cd > mod:
            cd -= mod + 1
    cd = mod + 1 - cd
    if cd == mod:
        cd = 0
    return alphabet[cd] == check.upper()


def _valid(fmt, m):
    groups = {k: _clean(v) for k, v in m.groupdict().items() if v is not None}
    v = fmt["validation"]
    checksum = v.get("checksum")
    if checksum:
        seq, check = groups.get("SerialNumber", ""), groups.get("CheckDigit", "")
        prepend = (v.get("serial_number_format") or {}).get("prepend_if")
        if prepend and re.search(prepend["matches_regex"], seq):
            seq = prepend["content"] + seq
        name = checksum["name"]
        if name == "mod10":
            ok = _mod10(seq, check, checksum.get("odds_multiplier"), checksum.get("evens_multiplier"),
                        checksum.get("reverse"))
        elif name == "mod7":
            ok = seq.isdigit() and str(int(seq) % 7) == check
        elif name == "luhn":
            ok = seq.isdigit() and check.isdigit() and _luhn(seq, check)
        elif name == "s10":
            ok = _s10(seq, check)
        elif name == "sum_product_with_weightings_and_modulo":
            ok = _weighted(seq, check, **checksum)
        elif name == "mod_37_36":
            ok = _mod37_36(seq, check)
        else:
            ok = False
        if not ok:
            return False
    for needed in (v.get("additional") or {}).get("exists", []):
        if not _lookup(fmt, needed, groups):
            return False
    return True


def _lookup(fmt, name, groups):
    for extra in fmt["additional"]:
        if extra.get("name") != name:
            continue
        value = groups.get(extra.get("regex_group_name"), "")
        for row in extra.get("lookup") or []:
            if row.get("matches") == value or (row.get("matches_regex") and re.fullmatch(row["matches_regex"], value)):
                return row
    return None


def _carrier(fmt, m):
    if fmt["code"] == "s10":  # a postal number: the post office of the country it ends with
        row = _lookup(fmt, "Courier", {k: _clean(v) for k, v in m.groupdict().items() if v})
        if row and row.get("matches") == "US":
            return "USPS"
        return (row or {}).get("courier") or "Post"
    return fmt["carrier"]


def identify(number):
    """(carrier, format id) when the whole of number is a valid tracking number, else (None, None)."""
    number = _clean(number).upper()
    if not number:
        return None, None
    for fmt in formats():
        m = fmt["full"].fullmatch(number)
        if m and _valid(fmt, m):
            return _carrier(fmt, m), fmt["id"]
    return None, None


def find_all(text):
    """Every valid tracking number in text, in the order they appear: [(number, carrier), ...]."""
    text = text or ""
    found = []
    for fmt in formats():
        for m in fmt["search"].finditer(text):
            if not _valid(fmt, m):
                continue
            start, end = m.start(), m.end()
            while start < end and text[start] in " \t":
                start += 1
            while end > start and text[end - 1] in " \t":
                end -= 1
            if fmt["id"] not in STRONG and (ORDER_LABEL.search(text[max(0, start - 30):start])
                                            or not CONTEXT.search(text[max(0, start - CONTEXT_CHARS):start])):
                continue
            found.append((start, end, _clean(text[start:end]).upper(), _carrier(fmt, m)))
    found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
    out, taken_until, seen = [], -1, set()
    for start, end, number, carrier in found:
        if start < taken_until or number in seen:
            continue  # part of a number already found (a USPS number inside its 420+ZIP form, say)
        out.append((number, carrier))
        seen.add(number)
        taken_until = end
    return out


def url_for(number):
    """The carrier's own free tracking page for a valid number, or None."""
    number = _clean(number).upper()
    for fmt in formats():
        m = fmt["full"].fullmatch(number)
        if m and _valid(fmt, m) and fmt["url"]:
            return fmt["url"].replace("%s", number)
    return None
