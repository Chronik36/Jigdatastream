"""Match email orders to saved bot profiles.

An order is matched on the shipping address, the name on it and the email it was sent to. Several
profiles often share an address or a name, so all three are scored together and the best one wins;
when two profiles score the same the order is left for you to pick on the Orders page. A checkout
from a bot's Discord webhook with the same order number settles it outright.
"""
import json
import re

from . import address as A

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
# Words that say nothing about which street it is.
FILLER = set(A.SUFFIX.values()) | set(A.SUFFIX) | set(A.DIRECTION.values()) | set(A.DIRECTION) | \
    set(A.UNIT.values()) | set(A.UNIT) | A.STATES | {"US", "USA", "UNITED", "STATES", "OF", "THE"}


def _words(text):
    return re.findall(r"[A-Z0-9]+", A.norm_line(text))


def _alpha(text):
    return [w.lower() for w in re.findall(r"[A-Za-z]+", text or "")]


def norm_email(e):
    """Gmail ignores dots and +tags, so j.smith+nike@gmail.com is jsmith@gmail.com."""
    e = (e or "").strip().lower()
    m = re.search(r"[\w.+'-]+@[\w-]+(\.[\w-]+)+", e)
    if not m:
        return ""
    user, _, domain = m.group(0).partition("@")
    user = user.split("+")[0]
    if domain in ("gmail.com", "googlemail.com"):
        user, domain = user.replace(".", ""), "gmail.com"
    return f"{user}@{domain}"


def parse_order_address(text, name=None):
    """Pieces of a one-line address from an email: house numbers, street words, ZIP and the name in front."""
    text = text or ""
    zips = ZIP_RE.findall(text)
    zip5 = zips[-1] if zips else ""
    words = _words(text)
    numbers = {w for w in words if re.fullmatch(r"\d+[A-Z]?", w) and w != zip5}
    street = {w for w in words if w.isalpha() and len(w) > 1 and w not in FILLER}
    if not name:  # "Jacob Smith, 123 Main St, ..." -> "Jacob Smith"
        head = re.split(r"[,\n]|\d", text, maxsplit=1)[0].strip()
        if 1 < len(head.split()) <= 4:
            name = head
    return {"zip": zip5, "numbers": numbers, "street": street, "name": _alpha(name)}


def _profile_key(p):
    pre = "ship_" if (p["ship_address1"] or "").strip() else "bill_"
    words = _words(p[pre + "address1"])
    number = next((w for w in words if re.fullmatch(r"\d+[A-Z]?", w)), "")
    return {
        "name": p["name"],
        "zip": A.zip5(p[pre + "zip"]),
        "number": number,
        "street": {w for w in words if w.isalpha() and len(w) > 1 and w not in FILLER},
        "first": _alpha(p["first_name"]),
        "last": _alpha(p["last_name"]),
        "email": norm_email(p["email"]),
    }


def address_score(order, prof):
    """3 = number, street and ZIP agree; 2 = number plus ZIP or street; 1 = same street and ZIP, other number."""
    if not prof["number"] and not prof["street"]:
        return 0
    if order["zip"] and prof["zip"] and order["zip"] != prof["zip"]:
        return 0
    same_zip = bool(order["zip"]) and order["zip"] == prof["zip"]
    same_number = bool(prof["number"]) and prof["number"] in order["numbers"]
    same_street = bool(prof["street"] & order["street"])
    if same_number and same_zip and same_street:
        return 3
    if same_number and (same_zip or same_street):
        return 2
    if same_zip and same_street:
        return 1
    return 0


def name_score(order, prof):
    """2 = first and last name (or first initial and last), 1 = last name only."""
    words = set(order["name"])
    if not words or not prof["last"] or not set(prof["last"]) <= words:
        return 0
    first = prof["first"][:1]
    if first and (first[0] in words or any(len(w) == 1 and w == first[0][0] for w in words)):
        return 2
    return 1


def score(order, prof, recipient, inbox):
    """(points, reasons) or None when this profile can't be the one."""
    addr, name = address_score(order, prof), name_score(order, prof)
    email = 3 if prof["email"] and prof["email"] == recipient else (1 if prof["email"] and prof["email"] == inbox else 0)
    if not (addr >= 2 or email == 3 or (name == 2 and addr >= 1)):
        return None
    reasons = [r for r, on in (("address", addr), ("name", name), ("email", email == 3)) if on]
    return addr + name + email, reasons


def _checkout_profiles(con, names):
    """order number -> profile name, from successful bot checkouts."""
    out = {}
    for r in con.execute("SELECT order_number, profile FROM checkouts WHERE status='success' AND order_number != ''"
                         " AND order_number IS NOT NULL AND profile IS NOT NULL"):
        if r["profile"] in names:
            out[re.sub(r"[^A-Za-z0-9]", "", r["order_number"]).upper()] = r["profile"]
    return out


def best_match(o, keys, checkouts):
    """(profile name or None, reason text, other candidates)."""
    number = re.sub(r"[^A-Za-z0-9]", "", o.get("order_number") or "").upper()
    if number and number in checkouts:
        return checkouts[number], "bot checkout with this order number", []
    parsed = parse_order_address(o.get("shipping_address"), o.get("shipping_name"))
    recipient, inbox = norm_email(o.get("recipient_email")), norm_email(o.get("account"))
    scored = []
    for k in keys:
        s = score(parsed, k, recipient, inbox)
        if s:
            scored.append((s[0], k["name"], s[1]))
    if not scored:
        return None, None, []
    scored.sort(key=lambda t: (-t[0], t[1]))
    top = [t for t in scored if t[0] == scored[0][0]]
    if len(top) > 1:
        return None, "several profiles fit", [t[1] for t in top][:8]
    return top[0][1], " + ".join(top[0][2]), []


def match_orders(con):
    """Match every order that wasn't assigned by hand. Returns how many changed."""
    profiles = con.execute("SELECT * FROM profiles").fetchall()
    keys = [_profile_key(p) for p in profiles]
    checkouts = _checkout_profiles(con, {p["name"] for p in profiles})
    changed = 0
    for o in con.execute("SELECT * FROM tracked_orders WHERE COALESCE(profile_manual, 0) = 0").fetchall():
        name, reason, candidates = best_match(dict(o), keys, checkouts)
        cands = json.dumps(candidates) if candidates else None
        if (name, reason, cands) != (o["profile_name"], o["profile_match"], o["profile_candidates"]):
            con.execute("UPDATE tracked_orders SET profile_name=?, profile_match=?, profile_candidates=? WHERE id=?",
                        (name, reason, cands, o["id"]))
            changed += 1
    if changed:
        con.commit()
    return changed


def set_profile(con, order_id, name):
    """Assign an order to a profile by hand (or '' to mark it as no profile); syncs leave it alone.
    None hands it back to automatic matching."""
    if name is None:
        con.execute("UPDATE tracked_orders SET profile_manual=0 WHERE id=?", (order_id,))
    else:
        con.execute("UPDATE tracked_orders SET profile_name=?, profile_match='set by hand', profile_candidates=NULL,"
                    " profile_manual=1 WHERE id=?", (name or None, order_id))
    con.commit()


def history(con, orders):
    """Per profile: its orders, the sites it checked out on and a count per status."""
    out = {}
    for p in con.execute("SELECT name, group_name, first_name, last_name, email, ship_address1, ship_city,"
                         " ship_state, ship_zip FROM profiles ORDER BY name"):
        out[p["name"]] = {"profile": dict(p), "orders": [], "sites": {}, "statuses": {}, "spend": 0.0,
                          "checkouts": 0}
    for o in orders:
        h = out.get(o.get("profile_name"))
        if not h:
            continue
        h["orders"].append(o)
        site = o["retailer"] or "?"
        h["sites"][site] = h["sites"].get(site, 0) + 1
        status = o["shipping_status"] or "unknown"
        h["statuses"][status] = h["statuses"].get(status, 0) + 1
        h["spend"] += o["total"] or 0
    # Bot checkouts that never produced an email (yet) still show where the profile checked out.
    for r in con.execute("SELECT profile, site, COUNT(*) n FROM checkouts WHERE status='success' GROUP BY profile, site"):
        h = out.get(r["profile"])
        if h:
            h["checkouts"] += r["n"]
            if r["site"] and not any(r["site"].lower() in s.lower() or s.lower() in r["site"].lower() for s in h["sites"]):
                h["sites"][r["site"]] = 0
    return out
