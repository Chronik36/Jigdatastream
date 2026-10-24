"""Tracking: every live order laid out by where its package is, with a progress bar under each one.

Orders come from the emails (and the carriers, when key-free tracking or a carrier key is on). Lanes:
Ordered (no package yet), On the way, Out for delivery, and Delivered in the last DELIVERED_DAYS days.
Cancelled, returned and refunded orders are left off. Each card has the status picker from Orders, so a
wrong stage can be fixed here and becomes a training example the same way.
"""
import os
from datetime import date, timedelta

from flask import Blueprint, current_app, render_template, request

from . import carriers, db, profilematch
from .config import Config
from .orderstore import Store

bp = Blueprint("tracking", __name__)

DELIVERED_DAYS = 14
LANES = [("ordered", "Ordered", "Waiting for the store to ship"),
         ("moving", "On the way", "Shipped, with the carrier"),
         ("out", "Out for delivery", "Arriving today"),
         ("delivered", "Delivered", f"In the last {DELIVERED_DAYS} days")]
LANE_OF = {"ordered": "ordered", "processing": "ordered", "unknown": "ordered", "shipped": "moving",
           "in_transit": "moving", "exception": "moving", "out_for_delivery": "out", "delivered": "delivered"}


def lane_of(order):
    return LANE_OF.get(order["shipping_status"] or "unknown")


SHOWN_EVENTS = 6  # the rest open with "+ N more"


def arrival(order, today=None):
    """(headline, long day) for a card: "Arrives tomorrow", "Sun, Oct 25"."""
    today = today or date.today()
    status = order["shipping_status"] or "unknown"
    try:
        due = date.fromisoformat((order["estimated_delivery"] or "")[:10])
    except ValueError:
        due = None
    day = f"{due:%a}, {due:%b} {due.day}" if due else ""
    if status == "delivered":
        return "Delivered", day
    if status == "out_for_delivery":
        return "Out for delivery today", day
    if status == "exception":
        return "The carrier reported a problem", day
    if not due:
        return ("Waiting for the store to ship" if lane_of(order) == "ordered" else "On the way"), ""
    days = (due - today).days
    if days < 0:
        return f"Running late, was due {due:%b} {due.day}", day
    if days == 0:
        return "Arrives today", day
    if days == 1:
        return "Arrives tomorrow", day
    return (f"Arrives {due:%A}" if days < 7 else f"Arrives {due:%b} {due.day}"), day


def card(con, order, products, today=None):
    """What a Shipping card shows beyond the order itself: title, picture, arrival and the latest events."""
    from .ordersheet import picture, timeline
    first = (order["items"] or [{}])[0] if order["items"] else {}
    first = first if isinstance(first, dict) else {}
    headline, day = arrival(order, today)
    events = [e for e in timeline(con, order) if e["source"] != "order"]
    return {"title": first.get("name") or order["retailer"] or "Order",
            "image": first.get("image") or picture(first.get("name"), products),
            "arrives": headline, "day": day, "events": events}


def lanes(orders, today=None):
    """Live orders by lane, newest first; recent deliveries only. Returns {lane: [orders]}."""
    today = today or date.today()
    since = (today - timedelta(days=DELIVERED_DAYS)).isoformat()
    out = {key: [] for key, _, _ in LANES}
    for o in orders:
        lane = lane_of(o)
        if not lane:
            continue
        if lane == "delivered" and (o["last_email_date"] or o["order_date"] or "")[:10] < since:
            continue
        out[lane].append(o)
    # Packages arriving soonest first; orders without an ETA after them, newest order first.
    for lane in ("moving", "out", "ordered"):
        out[lane].sort(key=lambda o: (o["estimated_delivery"] or "9999", "" if not o["order_date"] else o["order_date"]))
    return out


@bp.get("/tracking")
def page():
    store = Store(db.connect(current_app.config.get("DB_PATH")))
    profilematch.match_orders(store.db)
    orders = store.orders()
    by_lane = lanes(orders)
    shop = request.args.get("store", "")
    shops = sorted({o["retailer"] for lane in by_lane.values() for o in lane if o["retailer"]})
    if shop:
        by_lane = {k: [o for o in v if o["retailer"] == shop] for k, v in by_lane.items()}
    late = [o for o in by_lane["moving"] + by_lane["ordered"]
            if o["estimated_delivery"] and o["estimated_delivery"] < date.today().isoformat()]
    config = Config.from_env()
    from . import products as prod
    try:
        products = prod.load().get("products", [])
    except Exception:
        products = []
    cards = {o["id"]: card(store.db, o, products) for lane in by_lane.values() for o in lane}
    return render_template("tracking.html", cards=cards, shown_events=SHOWN_EVENTS, lanes=LANES, by_lane=by_lane, shops=shops, shop=shop, late={o["id"] for o in late},
                           can_check=bool(carriers.available(os.environ)), checking=carriers.state,
                           training=config.training_mode, labels=store.labels(),
                           label_choices=["ordered", "in_transit", "out_for_delivery", "delivered", "cancelled", "returned",
                                          "not_an_order"])
