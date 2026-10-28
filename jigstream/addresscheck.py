"""Orders that shipped somewhere other than their profile's address.

Some stores (Target most of all) ship to the first address saved on the account instead of the one the bot
checked out with. The email an order was sent to names the account, and so the profile; when the address
on the order email isn't that profile's address, the order is flagged. With several profiles on one email,
any of their addresses counts as right. A bot checkout with the same order number names the profile too.
"""
import re

from . import profilematch as pm


def _line(p):
    pre = "ship_" if (p["ship_address1"] or "").strip() else "bill_"
    parts = [p[pre + "address1"], p[pre + "address2"], p[pre + "city"], p[pre + "state"], p[pre + "zip"]]
    return ", ".join(x.strip() for x in parts if x and str(x).strip())


def _profiles(con):
    rows = con.execute("SELECT * FROM profiles").fetchall()
    by_email, by_name = {}, {}
    for p in rows:
        k = dict(pm._profile_key(p), line=_line(p))
        by_name[p["name"]] = k
        if k["email"]:
            by_email.setdefault(k["email"], []).append(k)
    return by_email, by_name


def check(con, orders):
    """order id -> {"profiles": names the account belongs to, "expected": their address, "why": how the
    profile was found} for every order whose ship-to address isn't its profile's. Orders you marked fine,
    with no address, or with no profile to compare against are left out."""
    by_email, by_name = _profiles(con)
    if not by_name:
        return {}
    bot = pm._checkout_profiles(con, set(by_name))
    out = {}
    for o in orders:
        if o.get("address_ok") or not (o.get("shipping_address") or "").strip():
            continue
        parsed = pm.parse_order_address(o["shipping_address"], o.get("shipping_name"))
        if not parsed["zip"] and not parsed["numbers"]:
            continue  # "Pick up in store" or a name only: nothing to compare
        keys = by_email.get(pm.norm_email(o.get("recipient_email")), [])
        why = "the email it was sent to"
        number = re.sub(r"[^A-Za-z0-9]", "", o.get("order_number") or "").upper()
        if not keys and number in bot:
            keys, why = [by_name[bot[number]]], "the bot checkout with this order number"
        keys = [k for k in keys if k["number"] or k["street"]]
        if not keys or any(pm.address_score(parsed, k) >= 2 for k in keys):
            continue
        out[o["id"]] = {"profiles": [k["name"] for k in keys], "expected": keys[0]["line"], "why": why}
    return out


def set_ok(con, order_id, ok=True):
    con.execute("UPDATE tracked_orders SET address_ok = ? WHERE id = ?", (1 if ok else 0, order_id))
    con.commit()
