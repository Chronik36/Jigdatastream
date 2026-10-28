"""Orders grouped by drop day: the day each was ordered. Orders and Shipping open on these cards; tapping a
day shows its orders."""
from datetime import date

from .orderstore import NOT_SPENT

GROUPS = [("ordered", "Ordered", ("ordered", "processing", "unknown", None)),
          ("moving", "On the way", ("shipped", "in_transit", "exception", "out_for_delivery")),
          ("delivered", "Delivered", ("delivered",)),
          ("cancelled", "Cancelled", ("cancelled", "returned", "refunded"))]


def day_of(order):
    return (order.get("order_date") or order.get("last_email_date") or "")[:10] or "none"


def label(day):
    """"Tue, Oct 28" (with the year when it isn't this year), or "No date"."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return "No date"
    text = f"{d:%a}, {d:%b} {d.day}"
    return text if d.year == date.today().year else f"{text}, {d.year}"


def by_day(orders, flagged=()):
    """One card per drop day, newest first: how many orders, spend, a count per status group, the stores and
    how many shipped to the wrong address."""
    days = {}
    for o in orders:
        d = days.setdefault(day_of(o), {"orders": 0, "spend": 0.0, "shops": {}, "wrong": 0,
                                        **{k: 0 for k, _, _ in GROUPS}})
        d["orders"] += 1
        status = o.get("shipping_status")
        if status not in NOT_SPENT:
            d["spend"] += o.get("total") or 0
        for key, _, statuses in GROUPS:
            if status in statuses:
                d[key] += 1
        shop = o.get("retailer") or "Unknown store"
        d["shops"][shop] = d["shops"].get(shop, 0) + 1
        d["wrong"] += o["id"] in flagged
    out = []
    for day in sorted(days, key=lambda k: (k == "none", k), reverse=False):
        d = days[day]
        d.update(day=day, label=label(day), shops=sorted(d["shops"].items(), key=lambda s: (-s[1], s[0].lower())))
        out.append(d)
    dated = sorted((d for d in out if d["day"] != "none"), key=lambda d: d["day"], reverse=True)
    return dated + [d for d in out if d["day"] == "none"]
