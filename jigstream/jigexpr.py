"""Expressions for changing profile fields in bulk (jigging addresses, names, phones), in the style bots use:
"123 Main St, APT 1-%c5%" -> "123 Main St, APT 1-MWMJB".

A template can use the profile's current values as {address1}, {address2}, {city}, {state}, {zip}, {first_name},
{last_name}, {email}, {phone} and {value} (the field being changed), plus the expressions in EXPRESSIONS. Each
profile gets its own random picks. Find & replace mode runs a regular expression over the field and expands
expressions in the replacement (\\1 etc. refer to the found groups).
"""
import random
import re
import string

FIRST_F = ["Emma", "Olivia", "Ava", "Sophia", "Mia", "Amelia", "Harper", "Evelyn", "Abigail", "Emily", "Ella", "Grace",
           "Chloe", "Lily", "Hannah", "Zoe", "Nora", "Riley", "Layla", "Aria"]
FIRST_M = ["Liam", "Noah", "James", "Oliver", "Elijah", "William", "Henry", "Lucas", "Benjamin", "Mason", "Ethan", "Jacob",
           "Logan", "Jack", "Owen", "Ryan", "Caleb", "Nathan", "Dylan", "Wyatt"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Martinez", "Wilson", "Anderson",
        "Taylor", "Thomas", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White", "Harris", "Clark", "Lewis", "Walker"]
SUFFIXES = ["St", "Street", "Ave", "Avenue", "Rd", "Road", "Dr", "Drive", "Ln", "Lane", "Ct", "Court", "Blvd", "Way", "Pl"]
STREETS = ["Oak", "Maple", "Cedar", "Pine", "Elm", "Washington", "Lake", "Hill", "Park", "Main", "Sunset", "Ridge", "River"]
AREA_US = ["212", "305", "312", "404", "415", "512", "602", "617", "702", "713", "214", "206", "303", "480", "919"]
AREA_CA = ["416", "514", "604", "403", "613", "780", "905", "647", "438", "587"]

# (section, expression, what it does, example) for the reference list on the page.
EXPRESSIONS = [
    ("Characters", "%c5%", "Random uppercase letters, exact length", "APT 1-%c5%"),
    ("Characters", "%c2-4%", "Random uppercase letters, random length", "%c2-4% 123 Main St"),
    ("Characters", "%l5%", "Random lowercase letters, exact length", "%l5%"),
    ("Characters", "%l2-4%", "Random lowercase letters, random length", "%l2-4%"),
    ("Words", "%w[Apt|Unit|Suite]%", "Random word from a list", "%w[Apt|Unit|Ste]% %d3%"),
    ("Words", "%wl[Apt|Unit|Suite]%", "Random word from a list, lower case", "%wl[apt|unit]%"),
    ("Words", "%wr[Apt|Unit|Suite]%", "Random word from a list, random capitalization", "%wr[apt|unit]%"),
    ("Words", "%rc[text]%", "Randomize capitalization", "%rc[{address1}]%"),
    ("Numbers", "%d4%", "Random digits, exact length", "#%d4%"),
    ("Numbers", "%d2-4%", "Random digits, random length", "%d2-4%"),
    ("Numbers", "%n[1-250]%", "Random number in a range", "Unit %n[1-250]%"),
    ("Numbers", "%n[4|12|30]%", "Random number from a list", "%n[4|12|30]%"),
    ("Symbols", "%dots[text]%", "Insert periods randomly", "%dots[{first_name}]%"),
    ("Names", "%first%", "Random first name", "%first%"),
    ("Names", "%ffirst%", "Random female first name", "%ffirst%"),
    ("Names", "%mfirst%", "Random male first name", "%mfirst%"),
    ("Names", "%last%", "Random last name", "%last%"),
    ("Names", "%emailname%", "First and last name read from the email", "%emailname%"),
    ("Address", "%suffix%", "Random street suffix", "123 Oak %suffix%"),
    ("Address", "%street%", "Random street address", "%street%"),
    ("Phone", "%phone%", "Random US phone number", "%phone%"),
    ("Phone", "%caphone%", "Random Canadian phone number", "%caphone%"),
    ("Misc", "%limit[20|text]%", "Limit to so many characters", "%limit[30|{address1}]%"),
    ("Misc", "%%", "A percent sign", "100%%"),
]
FIELDS = ("address1", "address2", "city", "state", "zip", "first_name", "last_name", "email", "phone")
TOKEN = re.compile(r"%([a-z]+)(\d+(?:-\d+)?)?(?:\[([^\[\]%]*)\])?%")
PLACE = re.compile(r"\{(" + "|".join(FIELDS + ("value",)) + r")\}")
PCT = "\x00pct\x00"


class ExprError(ValueError):
    pass


def _length(n):
    lo, _, hi = n.partition("-")
    lo, hi = int(lo), int(hi or lo)
    if hi < lo or hi > 200:
        raise ExprError(f"{n} isn't a length BotTrack can use")
    return lo, hi


def _rand_case(text, rng):
    return "".join(ch.upper() if rng.random() < .5 else ch.lower() for ch in text)


def _token(m, rng, values):
    name, num, arg = m.group(1), m.group(2), m.group(3)
    if name in ("c", "l", "d") and num:
        lo, hi = _length(num)
        pool = {"c": string.ascii_uppercase, "l": string.ascii_lowercase, "d": string.digits}[name]
        return "".join(rng.choice(pool) for _ in range(rng.randint(lo, hi)))
    if name in ("w", "wl", "wr") and arg is not None:
        word = rng.choice(arg.split("|"))
        return {"w": word, "wl": word.lower(), "wr": _rand_case(word, rng)}[name]
    if name == "rc" and arg is not None:
        return _rand_case(arg, rng)
    if name == "n" and arg:
        span = re.fullmatch(r"(-?\d+)-(-?\d+)", arg.strip())
        if not span:
            return rng.choice(arg.split("|")).strip()
        lo, hi = map(int, span.groups())
        return str(rng.randint(min(lo, hi), max(lo, hi)))
    if name == "dots" and arg is not None:
        out = arg[:1]
        for ch in arg[1:]:
            out += ("." if ch.isalnum() and out[-1:].isalnum() and rng.random() < .35 else "") + ch
        return out
    if name == "limit" and arg and "|" in arg:
        n, _, text = arg.partition("|")
        return text[:int(n)] if n.strip().isdigit() else text
    if name == "first":
        return rng.choice(FIRST_F + FIRST_M)
    if name == "ffirst":
        return rng.choice(FIRST_F)
    if name == "mfirst":
        return rng.choice(FIRST_M)
    if name == "last":
        return rng.choice(LAST)
    if name == "emailname":
        local = re.sub(r"[+].*", "", (values.get("email") or "").split("@")[0])
        parts = [p for p in re.split(r"[._\d-]+", local) if p]
        return " ".join(p.capitalize() for p in parts[:2])
    if name == "suffix":
        return rng.choice(SUFFIXES)
    if name == "street":
        return f"{rng.randint(10, 9999)} {rng.choice(STREETS)} {rng.choice(SUFFIXES)}"
    if name in ("phone", "caphone"):
        area = rng.choice(AREA_US if name == "phone" else AREA_CA)
        return f"{area}{rng.randint(200, 999)}{rng.randint(0, 9999):04d}"
    raise ExprError(f"Unknown expression {m.group(0)}")


def expand(template, values=None, rng=None):
    """Fill {placeholders} from values and expand every expression, innermost first."""
    values = values or {}
    rng = rng or random.Random()
    text = template.replace("%%", PCT)
    text = PLACE.sub(lambda m: str(values.get(m.group(1)) or ""), text)
    for _ in range(12):  # nested expressions (%rc[%first%]%) expand from the inside out
        new = TOKEN.sub(lambda m: _token(m, rng, values), text)
        if new == text:
            break
        text = new
    return text.replace(PCT, "%")


def rewrite(value, values, mode, find="", replace="", rng=None):
    """The new value of one field. mode "template": replace's expansion; mode "regex": find -> replace."""
    rng = rng or random.Random()
    values = dict(values, value=value or "")
    if mode == "regex":
        try:
            pattern = re.compile(find, re.I)
        except re.error as e:
            raise ExprError(f"The find pattern isn't valid: {e}") from None
        # Expressions in the replacement are expanded per match; \1 style groups are filled in by re.
        return pattern.sub(lambda m: m.expand(expand(replace, values, rng)), value or "")
    return expand(replace, values, rng)
