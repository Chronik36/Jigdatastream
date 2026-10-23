"""Load bot profile exports (JSON or CSV) into a common shape."""
import csv
import io
import json
import re

FIELD_ALIASES = {
    "name": ["profilename", "profile_name", "name", "profile", "title"],
    "first_name": ["firstname", "first_name", "first"],
    "last_name": ["lastname", "last_name", "last"],
    "phone": ["phone", "phonenumber", "phone_number"],
    "email": ["email", "emailaddress"],
    "address1": ["address1", "address", "addressline1", "line1", "street", "addr1"],
    "address2": ["address2", "addressline2", "line2", "apt", "addr2"],
    "city": ["city", "town"],
    "state": ["state", "province", "region"],
    "zip": ["zip", "zipcode", "postcode", "postalcode", "zip_code"],
    "country": ["country", "countrycode"],
    "card_last4": ["last4", "cardlast4"],
}
SHIP_KEYS = ("shipping", "delivery", "ship", "shippingaddress")
BILL_KEYS = ("billing", "bill", "billingaddress")


def _norm_key(k):
    return str(k).lower().replace(" ", "").replace("-", "").replace("_", "")


def _get(d, key):
    low = {_norm_key(k): v for k, v in d.items()}
    for a in FIELD_ALIASES[key]:
        if a in low and low[a] not in (None, ""):
            return str(low[a]).strip()
    return ""


def _addr(d, prefix_keys, flat_prefixes):
    low = {_norm_key(k): v for k, v in d.items()}
    for k in prefix_keys:
        if isinstance(low.get(k), dict):
            sub = low[k]
            return {f: _get(sub, f) for f in ("address1", "address2", "city", "state", "zip", "country")}
    # flat columns like shipAddress1 / billing_city
    out = {}
    for f in ("address1", "address2", "city", "state", "zip", "country"):
        out[f] = ""
        for p in flat_prefixes:
            for a in FIELD_ALIASES[f]:
                if (p + a) in low and low[p + a] not in (None, ""):
                    out[f] = str(low[p + a]).strip()
                    break
            if out[f]:
                break
    return out


CARD_PARENTS = ("card", "payment", "payments", "paymentinfo", "creditcard", "cardinfo")
CARD_FIELDS = {
    "number": ("cardnumber", "ccnumber", "cardnum", "pan", "number"),
    "cvv": ("cvv", "cvc", "cvn", "cardcvv", "cardcvc", "securitycode", "cardsecuritycode"),
    "month": ("expmonth", "cardmonth", "expirymonth", "expirationmonth", "cardexpmonth", "month"),
    "year": ("expyear", "cardyear", "expiryyear", "expirationyear", "cardexpyear", "year"),
    "exp": ("exp", "expiry", "expiration", "expdate", "expirydate", "expirationdate", "cardexp", "cardexpiry"),
    "holder": ("cardholder", "cardholdername", "nameoncard", "cardname", "holder", "holdername"),
    "brand": ("cardtype", "cardbrand", "brand", "type"),
}
# Generic words that only mean a card field inside a card/payment section.
CARD_ONLY_INSIDE = {"number", "month", "year", "holder", "brand", "type", "exp"}


def card_field(key, in_card):
    k = _norm_key(key)
    for field, names in CARD_FIELDS.items():
        if k in names and (in_card or k not in CARD_ONLY_INSIDE):
            return field
    return None


def _scrub(node, in_card=False):
    """Blank card values so card data never reaches the database. The keys stay, so a
    stored profile still shows its bot's file layout when profiles are exported."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            child_in_card = in_card or _norm_key(k) in CARD_PARENTS
            if isinstance(v, (dict, list)):
                out[k] = _scrub(v, child_in_card)
            elif card_field(k, in_card) in ("number", "cvv") or (in_card and not card_field(k, in_card)
                                                                 and _norm_key(k) not in ("id", "name")):
                out[k] = ""  # expiry and brand stay: they're shown on the card and aren't secret
            else:
                out[k] = v
        return out
    if isinstance(node, list):
        return [_scrub(x, in_card) for x in node]
    return node


def card_details(node, in_card=False, out=None):
    """Card fields found anywhere in one profile: {"number", "cvv", "month", "year", "holder", "brand"}."""
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                card_details(v, in_card or _norm_key(k) in CARD_PARENTS, out)
            elif v not in (None, ""):
                field = card_field(k, in_card)
                if field and field not in out:
                    out[field] = str(v).strip()
    elif isinstance(node, list):
        for x in node:
            card_details(x, in_card, out)
    if "exp" in out and not ("month" in out and "year" in out):
        m = re.match(r"\s*(\d{1,2})\s*[/-]\s*(\d{2,4})", out["exp"])
        if m:
            out.setdefault("month", m.group(1).zfill(2))
            out.setdefault("year", m.group(2))
    return out


def card_brand(number, given=""):
    digits = re.sub(r"\D", "", number or "")
    if given and not given.isdigit():
        return given.strip().title()
    if re.match(r"3[47]", digits):
        return "Amex"
    if digits[:1] == "4":
        return "Visa"
    if re.match(r"5[1-5]|2[2-7]", digits):
        return "Mastercard"
    if re.match(r"6(011|5)", digits):
        return "Discover"
    return ""


def _card_meta(d):
    c = card_details(d)
    digits = re.sub(r"\D", "", c.get("number", ""))
    exp = ""
    if c.get("month") and c.get("year"):
        exp = f"{c['month'].zfill(2)[-2:]}/{c['year'][-2:]}"
    return digits[-4:], card_brand(digits, c.get("brand", "")), exp


def _looks_like_profile(d):
    low = {_norm_key(k) for k in d}
    has_addr = any(k in low for k in ("shipping", "billing", "shippingaddress", "billingaddress")) or any(
        a in low or ("ship" + a) in low or ("shipping" + a) in low or ("billing" + a) in low
        for a in FIELD_ALIASES["address1"]
    )
    return has_addr


def _find_profile_dicts(node, found):
    if isinstance(node, list):
        for x in node:
            _find_profile_dicts(x, found)
    elif isinstance(node, dict):
        if _looks_like_profile(node):
            found.append(node)
        else:
            for v in node.values():
                _find_profile_dicts(v, found)


def parse_profiles(text, bot=""):
    text = text.strip()
    try:
        data, fmt = json.loads(text), "json"
    except json.JSONDecodeError:
        data, fmt = list(csv.DictReader(io.StringIO(text))), "csv"
    found = []
    _find_profile_dicts(data, found)
    out = []
    for d in found:
        ship = _addr(d, SHIP_KEYS, ("ship", "shipping", "delivery", ""))
        bill = _addr(d, BILL_KEYS, ("bill", "billing", ""))
        if not any(bill.values()):
            bill = dict(ship)
        last4, brand, exp = _card_meta(d)
        # Some bots keep name, email and phone inside the shipping section instead of the top level.
        low = {_norm_key(k): v for k, v in d.items()}
        inner = next((low[k] for k in SHIP_KEYS + BILL_KEYS if isinstance(low.get(k), dict)), {})
        person = {f: _get(d, f) or _get(inner, f) for f in ("first_name", "last_name", "phone", "email")}
        if not (person["first_name"] or person["last_name"]) and _get(inner, "name"):
            person["first_name"], _, person["last_name"] = _get(inner, "name").partition(" ")
        out.append({
            "name": _get(d, "name") or f"{person['first_name']} {person['last_name']}".strip(),
            "bot": bot, **person,
            **{"ship_" + k: v for k, v in ship.items()},
            **{"bill_" + k: v for k, v in bill.items()},
            "card_last4": last4 or _get(d, "card_last4"),
            "card_brand": brand, "card_exp": exp, "fmt": fmt,
            "raw": json.dumps(_scrub(d)),
        })
    return out


# Retail sites offered when importing profiles; any other site can be typed in.
RETAIL_SITES = ["Amazon", "Best Buy", "Pokemon Center", "Target", "Walmart", "Topps", "Shopify", "Menards",
                "Crunchyroll", "Bandai", "Riot"]
FIELDS = ["bot", "first_name", "last_name", "phone", "email", "ship_address1", "ship_address2", "ship_city", "ship_state",
          "ship_zip", "ship_country", "bill_address1", "bill_address2", "bill_city", "bill_state", "bill_zip",
          "bill_country", "card_last4", "raw", "card_brand", "card_exp", "fmt"]


def ingest(con, text, bot="", site="", group=""):
    """Import a bot's profile export. A profile already saved under the same name is updated in place, so its
    linked card stays; the retail site and group are set when given."""
    site, group = (site or "").strip(), (group or "").strip()
    if group:
        con.execute("INSERT OR IGNORE INTO profile_groups VALUES (?)", (group,))
    extra = [("site", site)] * bool(site) + [("group_name", group)] * bool(group)
    cols = ["name", *FIELDS, *(k for k, _ in extra)]
    n = 0
    for p in parse_profiles(text, bot):
        if not p["name"]:
            continue
        values = {**p, **dict(extra)}
        con.execute(f"INSERT INTO profiles ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
                    f" ON CONFLICT(name) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols[1:])}", values)
        n += 1
    con.commit()
    return n
