"""Load bot profile exports (JSON or CSV) into a common shape."""
import csv
import io
import json

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
    return str(k).lower().replace(" ", "").replace("-", "")


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


def _looks_like_profile(d):
    low = {_norm_key(k) for k in d}
    has_addr = any(k in low for k in ("shipping", "billing", "shippingaddress", "billingaddress")) or any(
        a in low or ("ship" + a) in low or ("billing" + a) in low
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
        data = json.loads(text)
    except json.JSONDecodeError:
        data = list(csv.DictReader(io.StringIO(text)))
    found = []
    _find_profile_dicts(data, found)
    out = []
    for d in found:
        ship = _addr(d, SHIP_KEYS, ("ship", "shipping", "delivery", ""))
        bill = _addr(d, BILL_KEYS, ("bill", "billing", ""))
        if not any(bill.values()):
            bill = dict(ship)
        card = d.get("card") or d.get("payment") or {}
        out.append({
            "name": _get(d, "name") or f"{_get(d, 'first_name')} {_get(d, 'last_name')}".strip(),
            "bot": bot, "first_name": _get(d, "first_name"), "last_name": _get(d, "last_name"),
            "phone": _get(d, "phone"), "email": _get(d, "email"),
            **{"ship_" + k: v for k, v in ship.items()},
            **{"bill_" + k: v for k, v in bill.items()},
            "card_last4": (str(card.get("number", ""))[-4:] if isinstance(card, dict) else "") or _get(d, "card_last4"),
            "raw": json.dumps(d),
        })
    return out


def ingest(con, text, bot=""):
    n = 0
    for p in parse_profiles(text, bot):
        if not p["name"]:
            continue
        con.execute(
            """INSERT OR REPLACE INTO profiles VALUES
            (:name,:bot,:first_name,:last_name,:phone,:email,
             :ship_address1,:ship_address2,:ship_city,:ship_state,:ship_zip,:ship_country,
             :bill_address1,:bill_address2,:bill_city,:bill_state,:bill_zip,:bill_country,
             :card_last4,:raw)""", p)
        n += 1
    con.commit()
    return n
