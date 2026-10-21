"""Address normalization and diagnostics.

Goal: find why an address likely failed (AVS mismatch, malformed fields) and
propose a clean, standardized version. It does not generate address variations
to get around retailer limits.
"""
import re

SUFFIX = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR", "ROAD": "RD",
    "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR", "PLACE": "PL", "TERRACE": "TER",
    "HIGHWAY": "HWY", "PARKWAY": "PKWY", "TRAIL": "TRL", "WAY": "WAY", "SQUARE": "SQ",
}
DIRECTION = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
             "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}
UNIT = {"APARTMENT": "APT", "SUITE": "STE", "UNIT": "UNIT", "BUILDING": "BLDG",
        "FLOOR": "FL", "ROOM": "RM", "NUMBER": "#"}
UNIT_RE = re.compile(r"^(.*?)[\s,]+(APT|STE|UNIT|BLDG|FL|RM|#)\.?\s*#?\s*([A-Z0-9-]+)$")
STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())


def norm_line(s):
    s = re.sub(r"[.,]", " ", (s or "").upper())
    words = []
    for w in s.split():
        w = UNIT.get(w, DIRECTION.get(w, SUFFIX.get(w, w)))
        words.append(w)
    return " ".join(words)


def split_unit(line1, line2=""):
    """Return (street, unit) with a unit hiding in line1 moved out."""
    street, unit = norm_line(line1), norm_line(line2)
    m = UNIT_RE.match(street)
    if m and not unit:
        street, unit = m.group(1).strip(), f"{m.group(2)} {m.group(3)}"
    return street, unit


def zip5(z):
    m = re.match(r"\s*(\d{5})", z or "")
    return m.group(1) if m else ""


def avs_key(addr1, zip_):
    """AVS checks mostly compare street number + ZIP5."""
    num = re.match(r"\s*(\d+)", addr1 or "")
    return (num.group(1) if num else "", zip5(zip_))


def diagnose(a1, a2, city, state, zip_, country="US"):
    issues = []
    if not (a1 or "").strip():
        issues.append("missing address line 1")
    elif not re.match(r"\s*\d", a1):
        issues.append("address line 1 has no street number")
    if re.search(r"\b(apt|apartment|suite|ste|unit|#)\b", a1 or "", re.I):
        issues.append("unit number is in line 1; move to line 2")
    if re.search(r"[^\w\s.,#'-]", a1 or ""):
        issues.append("unusual characters in line 1")
    if (country or "US").upper() in ("US", "USA", "UNITED STATES", ""):
        if not re.fullmatch(r"\d{5}(-\d{4})?", (zip_ or "").strip()):
            issues.append("ZIP is not 5 digits (or ZIP+4)")
        if (state or "").strip().upper() not in STATES:
            issues.append("state is not a 2-letter US code")
    if not (city or "").strip():
        issues.append("missing city")
    return issues


def suggest(a1, a2, city, state, zip_):
    street, unit = split_unit(a1, a2)
    return {
        "address1": street,
        "address2": unit,
        "city": (city or "").strip().upper(),
        "state": (state or "").strip().upper()[:2],
        "zip": zip5(zip_) or (zip_ or "").strip(),
    }
