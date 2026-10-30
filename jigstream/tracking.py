"""Tracking: every live order laid out by where its package is, with a progress bar under each one.

Orders come from the emails (and the carriers, when key-free tracking or a carrier key is on). Lanes:
Ordered (no package yet), On the way, Out for delivery, and Delivered in the last DELIVERED_DAYS days.
Cancelled, returned and refunded orders are left off. Each card has the status picker from Orders, so a
wrong stage can be fixed here and becomes a training example the same way.
"""
import os
from datetime import date, timedelta

from flask import Blueprint, current_app, render_template, request

from . import bases, carriers, db, profilematch
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
            "image": first.get("image") or picture(first.get("name"), products, con),
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


def base_details(con, res, base_id):
    """The picked base: its name, the profiles that ship there and how their checkouts went."""
    from .drop import rows, tally
    b = res.by_id.get(base_id) or {"id": "none", "name": "Not matched to a base", "full": ""}
    names = sorted((n for n in res.homes if res.of_profile(n) == base_id), key=str.lower)
    try:
        here = [c for c in rows(con) if res.of_checkout(c) == base_id]
    except Exception:
        here = []
    per = {}
    for c in here:
        if c["profile"]:
            per.setdefault(c["profile"], []).append(c)
    groups = {r["name"]: r["group_name"] or "No group" for r in con.execute("SELECT name, group_name FROM profiles")}
    profiles = [{"name": n, "tally": tally(per.get(n, []))} for n in names]
    profiles += [{"name": n, "tally": tally(cs)} for n, cs in per.items() if n not in names]
    by_group = {}
    for p in profiles:  # picked by group, then profile: bots make hundreds of profiles
        p["group"] = groups.get(p["name"], "No group")
        by_group.setdefault(p["group"], []).append({"name": p["name"], "real": p["tally"]["real"],
                                                    "tries": p["tally"]["tries"]})
    return {"id": b["id"], "name": b["name"], "full": b["full"], "profiles": profiles, "tally": tally(here),
            "groups": dict(sorted(by_group.items(), key=lambda g: g[0].lower()))}


CAL_STATES = {"ordered": "Ordered", "moving": "On the way", "out": "Out for delivery", "late": "Running late",
              "delivered": "Delivered"}


def calendar_rows(orders, today=None):
    """Live orders for the delivery calendar: (dated, undated) lists of plain dicts.

    A package sits on the store's estimated day; a delivered one on the day it was delivered (the last email).
    Orders with no day at all go to `undated`. Cancelled, returned and refunded orders have no lane and are left off.
    """
    today = (today or date.today()).isoformat()
    dated, undated = [], []
    for o in orders:
        lane = lane_of(o)
        if not lane:
            continue
        eta = (o["estimated_delivery"] or "")[:10]
        day = (o["last_email_date"] or eta or o["order_date"] or "")[:10] if lane == "delivered" else eta
        state = "late" if lane != "delivered" and eta and eta < today else lane
        first = (o["items"] or [{}])[0] if o["items"] else {}
        first = first if isinstance(first, dict) else {}
        row = {"id": o["id"], "date": day, "state": state, "retailer": o["retailer"] or "Unknown",
               "carrier": o["carrier"] or "Unknown", "tracking": o["tracking_number"] or "",
               "title": first.get("name") or o["retailer"] or "Order", "order_number": o["order_number"] or ""}
        (dated if day else undated).append(row)
    return dated, undated


@bp.get("/tracking/calendar")
def calendar():
    return render_template("tracking_calendar.html", states=CAL_STATES)


@bp.get("/tracking/calendar/data")
def calendar_data():
    store = Store(db.connect(current_app.config.get("DB_PATH")))
    dated, undated = calendar_rows(store.orders())
    today = date.today().isoformat()
    live = [r for r in dated + undated if r["state"] != "delivered"]
    return {"rows": dated, "undated": undated, "today": today,
            "summary": {"in_transit": len(live), "today": sum(r["date"] == today for r in live),
                        "late": sum(r["state"] == "late" for r in live)}}


@bp.get("/tracking")
def page():
    from .app import STATUS_CHOICES
    store = Store(db.connect(current_app.config.get("DB_PATH")))
    profilematch.match_orders(store.db)
    orders = store.orders()
    by_lane = lanes(orders)
    shop = request.args.get("store", "")
    shops = sorted({o["retailer"] for lane in by_lane.values() for o in lane if o["retailer"]})
    if shop:
        by_lane = {k: [o for o in v if o["retailer"] == shop] for k, v in by_lane.items()}
    today = date.today().isoformat()
    late = [o for o in by_lane["moving"] + by_lane["ordered"]
            if o["estimated_delivery"] and o["estimated_delivery"] < today]
    # With base addresses, the page opens on how many packages go to each; picking one shows its cards.
    res = bases.Resolver(store.db)
    base = request.args.get("base", "")
    lane_counts = {k: len(v) for k, v in by_lane.items()}
    # A tapped tile (Ordered, On the way, ..., Running late): only those packages, every base, to check and fix.
    only = request.args.get("lane", "")
    if only in by_lane or only == "late":
        late_ids = {o["id"] for o in late}
        by_lane = {k: ([o for o in v if o["id"] in late_ids] if only == "late" else v if k == only else [])
                   for k, v in by_lane.items()}
        base = base or "all"
    # The page opens on one card per drop day; ?day= shows that day's packages (every base), ?view=bases the
    # base address cards.
    from . import addresscheck, dropdays
    wrong = addresscheck.check(store.db, [o for lane in by_lane.values() for o in lane])
    day, view = request.args.get("day", ""), request.args.get("view", "")
    days = None
    if day and day != "all":
        by_lane = {k: [o for o in v if dropdays.day_of(o) == day] for k, v in by_lane.items()}
        lane_counts = {k: len(v) for k, v in by_lane.items()}
        late = [o for o in late if dropdays.day_of(o) == day]
        base = base or "all"
    elif not (base or only or day or view):
        days = dropdays.by_day([o for lane in by_lane.values() for o in lane], wrong)
        by_lane = {k: [] for k in by_lane}
        base = "all"
    overview, place = None, None
    if res.bases and not base:
        live = [o for lane in by_lane.values() for o in lane]
        late_ids = {o["id"] for o in late}

        def count(items):
            n = {key: sum(1 for o in items if lane_of(o) == key) for key, _, _ in LANES}
            return dict(n, total=len(items), late=sum(1 for o in items if o["id"] in late_ids),
                        value=sum(o["total"] or 0 for o in items))
        overview = bases.overview(live, res.of_order, res.bases, count)
        by_lane = {k: [] for k in by_lane}
    elif res.bases and base != "all":
        want = None if base == "none" else int(base) if base.isdigit() else -1
        by_lane = {k: [o for o in v if res.of_order(o) == want] for k, v in by_lane.items()}
        lane_counts = {k: len(v) for k, v in by_lane.items()}
        place = base_details(store.db, res, want)
    config = Config.from_env()
    from . import products as prod
    try:
        products = prod.load().get("products", [])
    except Exception:
        products = []
    cards = {o["id"]: card(store.db, o, products) for lane in by_lane.values() for o in lane}
    return render_template("tracking.html", days=days, day=day, day_label=dropdays.label(day), wrong=wrong,
                           day_groups=dropdays.GROUPS, overview=overview, place=place, lane_counts=lane_counts, only=only, base=base, has_bases=bool(res.bases),
                           cards=cards, shown_events=SHOWN_EVENTS, lanes=LANES, by_lane=by_lane, shops=shops, shop=shop, late={o["id"] for o in late},
                           can_check=bool(carriers.available(os.environ)), checking=carriers.state,
                           training=config.training_mode, labels=store.labels(),
                           label_choices=STATUS_CHOICES)
