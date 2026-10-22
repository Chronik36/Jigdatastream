"""Write profiles back out in a bot's own file layout.

There is no list of bot formats to keep up to date: a profile imported from a bot keeps its
original layout (with card values blanked), and that layout is the template for every profile
exported to that bot. Card numbers are never stored, so they only reach an export when the
original bot files are uploaded again at export time; they are read in memory and dropped.
"""
import csv
import io
import json
import re
import uuid

from . import address as A
from .profiles import (BILL_KEYS, CARD_PARENTS, FIELD_ALIASES, SHIP_KEYS, _norm_key, card_brand,
                       card_details, card_field, parse_profiles)
from .profiles import _find_profile_dicts

ID_KEYS = {"id", "uuid", "profileid", "_id"}
SAME_KEYS = {"sameasshipping", "sameasbilling", "billingsameasshipping", "shippingsameasbilling",
             "samebillingandshipping", "samebillingandshippingaddress", "billingasshipping", "usebillingasshipping"}
FULL_NAME_KEYS = {"name", "fullname"}

# Neutral layout for bots nothing has been imported from yet.
GENERIC = {"name": "", "email": "", "phone": "",
           "shipping": {"firstName": "", "lastName": "", "address1": "", "address2": "", "city": "",
                        "state": "", "zip": "", "country": "US"},
           "billing": {"firstName": "", "lastName": "", "address1": "", "address2": "", "city": "",
                       "state": "", "zip": "", "country": "US"},
           "sameAsShipping": True,
           "card": {"holder": "", "number": "", "expMonth": "", "expYear": "", "cvv": ""}}


def same_address(p):
    return all(A.norm_line(p["ship_" + f] or "") == A.norm_line(p["bill_" + f] or "")
               for f in ("address1", "address2", "city", "zip")) and \
        A.state_code(p["ship_state"]) == A.state_code(p["bill_state"])


def _field(key):
    k = _norm_key(key).replace("_", "")
    for f, names in FIELD_ALIASES.items():
        if k in {n.replace("_", "") for n in names}:
            return f
    return None


def _flat_section(key):
    """'shipAddress1' -> ('ship', 'address1'); 'billing_city' -> ('bill', 'city')."""
    k = _norm_key(key).replace("_", "")
    for prefix, section in (("shipping", "ship"), ("delivery", "ship"), ("ship", "ship"),
                            ("billing", "bill"), ("bill", "bill")):
        if k.startswith(prefix) and len(k) > len(prefix):
            f = _field(k[len(prefix):])
            if f:
                return section, f
    return None, None


def _state(value, template_value):
    code = A.state_code(value)
    if code and len(str(template_value or "").strip()) == 2:
        return code
    if code and len(str(value).strip()) == 2 and len(str(template_value or "").strip()) > 2:
        names = {v: k.title() for k, v in A.STATE_NAMES.items()}
        return names.get(code, value)
    return value


def _value(p, section, f, template_value, card):
    """The profile's value for canonical field f in a section ('ship', 'bill' or None)."""
    if f == "name":
        return p["name"] if section is None else f"{p['first_name']} {p['last_name']}".strip()
    if f in ("first_name", "last_name", "phone", "email"):
        return p[f] or ""
    if f == "card_last4":
        return p["card_last4"] or ""
    col = f"{section or 'ship'}_{f}"
    v = p[col] or ""
    if f == "state":
        return _state(v, template_value)
    if f == "country" and not v:
        return template_value
    return v


def _card_value(field, card, template_value):
    if field == "brand":
        return card.get("brand") or card_brand(card.get("number", "")) or template_value
    if field == "exp":
        if card.get("month") and card.get("year"):
            return f"{card['month'].zfill(2)}/{card['year'][-2:]}"
        return ""
    if field == "year" and card.get("year") and len(str(template_value)) == 4 and len(card["year"]) == 2:
        return "20" + card["year"]
    if field == "number":
        return card.get("number", "")
    return card.get(field, "")


def fill(template, p, card=None, section=None, in_card=False):
    """Copy of a template profile with every recognised field set from profile row p."""
    card = card or {}
    if isinstance(template, list):
        return [fill(x, p, card, section, in_card) for x in template]
    if not isinstance(template, dict):
        return template
    out = {}
    for k, v in template.items():
        nk = _norm_key(k).replace("_", "")
        if isinstance(v, (dict, list)):
            child = "ship" if nk in SHIP_KEYS else "bill" if nk in BILL_KEYS else section
            out[k] = fill(v, p, card, child, in_card or nk in CARD_PARENTS)
            continue
        if in_card or card_field(k, False):
            field = card_field(k, in_card) or ("holder" if nk in FULL_NAME_KEYS else None)
            if field == "holder":
                out[k] = card.get("holder") or f"{p['first_name']} {p['last_name']}".strip()
            elif field:
                out[k] = _card_value(field, card, v)
            else:
                out[k] = v
            continue
        if nk in ID_KEYS:
            out[k] = str(uuid.uuid4()) if v else v
        elif nk in SAME_KEYS:
            out[k] = same_address(p) if isinstance(v, bool) or v in ("", None) else ("true" if same_address(p) else "false")
        else:
            flat_section, f = _flat_section(k)
            if f:
                out[k] = _value(p, flat_section, f, v, card)
            elif (f := _field(k)):
                out[k] = _value(p, section, f, v, card)
            else:
                out[k] = v
    return out


def templates(con):
    """{bot: (format, template)} from the most recent imported profile of each bot."""
    out = {}
    for r in con.execute("SELECT bot, fmt, raw FROM profiles WHERE bot != '' AND raw IS NOT NULL ORDER BY rowid"):
        try:
            out[r["bot"]] = (r["fmt"] or "json", json.loads(r["raw"]))
        except (TypeError, ValueError):
            continue
    return out


def cards_from(texts):
    """Card details by profile name, read from the bot's original export files (kept in memory only)."""
    cards = {}
    for text in texts:
        try:
            data = json.loads(text)
        except ValueError:
            data = list(csv.DictReader(io.StringIO(text)))
        found = []
        _find_profile_dicts(data, found)
        for parsed, d in zip(parse_profiles(text), found):
            c = card_details(d)
            if parsed["name"] and c.get("number"):
                cards[parsed["name"]] = c
    return cards


def export(rows, fmt, template, cards=None):
    """(filename extension, text) for profile rows written in a bot's layout."""
    cards = cards or {}
    filled = [fill(template, p, cards.get(p["name"])) for p in rows]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(template.keys()))
        w.writeheader()
        for row in filled:
            w.writerow({k: v for k, v in row.items() if not isinstance(v, (dict, list))})
        return "csv", buf.getvalue()
    return "json", json.dumps(filled, indent=2)


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "profiles"
