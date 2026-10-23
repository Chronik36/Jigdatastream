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
STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA", "COLORADO": "CO",
    "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "PUERTO RICO": "PR",
}
STATES = set(STATE_NAMES.values())


def state_code(state):
    """'New York', 'new york' or 'NY' -> 'NY'; '' if it isn't a US state. Bots accept either form."""
    s = re.sub(r"\s+", " ", (state or "").strip().upper().replace(".", ""))
    return s if s in STATES else STATE_NAMES.get(s, "")


def norm_line(s):
    s = re.sub(r"[.,]", " ", (s or "").upper())
    words = []
    for w in s.split():
        w = UNIT.get(w, DIRECTION.get(w, SUFFIX.get(w, w)))
        words.append(w)
    return " ".join(words)


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
    if re.search(r"[^\w\s.,#'-]", a1 or ""):
        issues.append("unusual characters in line 1")
    if (country or "US").upper() in ("US", "USA", "UNITED STATES", ""):
        if not re.fullmatch(r"\d{5}(-\d{4})?", (zip_ or "").strip()):
            issues.append("ZIP is not 5 digits (or ZIP+4)")
        if not state_code(state):
            issues.append("state isn't a US state name or code")
    if not (city or "").strip():
        issues.append("missing city")
    return issues


def suggest(a1, a2, city, state, zip_):
    """A cleaned-up version. Units stay where they are (bots accept them in line 1) and a
    valid state keeps its form, full name or code."""
    return {
        "address1": re.sub(r"\s+", " ", (a1 or "").strip()),
        "address2": re.sub(r"\s+", " ", (a2 or "").strip()),
        "city": (city or "").strip(),
        "state": (state or "").strip() if state_code(state) else (state or "").strip().upper()[:2],
        "zip": zip5(zip_) or (zip_ or "").strip(),
    }
