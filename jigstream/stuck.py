"""Cross-reference successful bot checkouts with order emails, to see which ones stuck.

A bot's webhook says "success" the moment the site takes the order, but many orders get cancelled
afterwards. Each successful checkout is matched to the order emails: first by order number, and when
the webhook has none (or the email doesn't), by the same site and profile within a few days of the
checkout. A checkout stuck when no cancel or refund email followed it.
"""
import re
from datetime import datetime, timedelta, timezone

from . import profilematch

CANCELLED = ("cancelled", "refunded")
# How far an order email's date may sit from the checkout when matching on site and profile.
BEFORE, AFTER = timedelta(days=1), timedelta(days=4)
SITE_FILLER = re.compile(r"^(www\.)|\.(com|net|co|us|ca|co\.uk)$|\s+(us|usa|store|online|inc)$")


def _number(s):
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def site_key(s):
    s = (s or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    for _ in range(3):
        s = SITE_FILLER.sub("", s).strip()
    return re.sub(r"[^a-z0-9]", "", s)


def same_site(a, b):
    a, b = site_key(a), site_key(b)
    return bool(a and b) and (a in b or b in a)


def when(s):
    """An ISO date or time as an aware UTC datetime, or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _order_time(o):
    # The order date from the email is often just a day; the email time is more precise when it's that day.
    placed, emailed = when(o["order_date"]), when(o["last_email_date"])
    if placed and emailed and placed.date() == emailed.date():
        return emailed
    return placed or emailed


def _same_buyer(c, o):
    if c["profile"] and o["profile_name"] and c["profile"] == o["profile_name"]:
        return True
    email = profilematch.norm_email(c["email"])
    return bool(email) and email in (profilematch.norm_email(o["recipient_email"]), profilematch.norm_email(o["account"]))


def state_of(order):
    if order is None:
        return "no_email"
    status = order["shipping_status"] or "unknown"
    return "cancelled" if status in CANCELLED else "emailed"


def cross_reference(con):
    """Every successful checkout, newest first, with the order email it matched (or None), how it matched,
    and its state: "cancelled", "emailed" (order emails, none of them a cancel) or "no_email". The last two stuck."""
    profilematch.match_orders(con)
    orders = [dict(o) for o in con.execute(
        "SELECT * FROM tracked_orders WHERE COALESCE(shipping_status, '') != 'not_an_order'")]
    by_number = {}
    for o in orders:
        if _number(o["order_number"]):
            by_number.setdefault(_number(o["order_number"]), o)
    checkouts = [dict(c) for c in con.execute("SELECT * FROM checkouts WHERE status='success' ORDER BY ts")]
    used, out = set(), []
    for c in checkouts:
        o = by_number.get(_number(c["order_number"]))
        c["order"], c["matched_on"] = (o, "order number") if o else (None, None)
        if o:
            used.add(o["id"])
    # The rest pair up with order emails for the same site and profile, closest in time first, so two
    # checkouts of the same drop don't both claim one email.
    by_site, times = {}, {o["id"]: _order_time(o) for o in orders}
    for o in orders:
        if o["id"] not in used:
            by_site.setdefault(site_key(o["retailer"]), []).append(o)
    pairs = []
    for i, c in enumerate(checkouts):
        if c["order"]:
            continue
        t, key = when(c["ts"]), site_key(c["site"])
        near = [o for k, group in by_site.items() if key and k and (key in k or k in key) for o in group]
        for o in near:
            if not _same_buyer(c, o):
                continue
            # Both sides have numbers that differ: that's another order.
            if _number(c["order_number"]) and _number(o["order_number"]):
                continue
            ot = times[o["id"]]
            if t and ot and not (t - BEFORE <= ot <= t + AFTER):
                continue
            pairs.append((abs((ot - t).total_seconds()) if t and ot else float("inf"), i, o["id"], o))
    taken = set()
    for _, i, oid, o in sorted(pairs, key=lambda p: p[:3]):
        if i in taken or oid in used:
            continue
        checkouts[i]["order"], checkouts[i]["matched_on"] = o, "same site and profile"
        taken.add(i)
        used.add(oid)
    for c in checkouts:
        c["state"] = state_of(c["order"])
        out.append(c)
    out.sort(key=lambda c: c["ts"] or "", reverse=True)
    return out


def counts(rows):
    n = {"all": len(rows), "emailed": 0, "cancelled": 0, "no_email": 0}
    for r in rows:
        n[r["state"]] += 1
    n["stuck"] = n["emailed"] + n["no_email"]
    return n
