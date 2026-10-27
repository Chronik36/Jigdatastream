"""Phone alerts: when an email moves an order on (shipped, out for delivery, delivered, delayed, cancelled), post a
short note to this Mac's private alerts topic on ntfy.sh (free, no account). The Android app keeps a connection to
that topic open and shows each note as a notification within seconds; tapping it opens the order.

Only the store, the item's name and the new status go out, never addresses, names or tracking numbers. Alerts are
sent for emails from the last two days, so a first sync of a whole year doesn't buzz the phone hundreds of times.
Turned off with PHONE_ALERTS=0, and only sent while phone access is on."""

import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("jigstream.notify")

NTFY = "https://ntfy.sh/"
RECENT = timedelta(days=2)
NEWS = {"shipped": "Shipped", "in_transit": "On the way", "out_for_delivery": "Out for delivery",
        "delivered": "Delivered", "exception": "Running late", "cancelled": "Cancelled", "refunded": "Refunded"}


def topic():
    """The alerts topic, next to the phone link's own topic and as private (only paired phones know it)."""
    base = os.environ.get("PHONE_PAIR_TOPIC", "")
    if not base or os.environ.get("PHONE_ACCESS") != "1" or os.environ.get("PHONE_ALERTS", "1") == "0":
        return None
    return base + "-alerts"


def _recent(day):
    try:
        when = datetime.fromisoformat((day or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < RECENT


def message(order, status):
    """(title, text) for an order that just moved to status."""
    items = order.get("items")
    if isinstance(items, str):
        items = json.loads(items or "[]")
    name = next((i.get("name") for i in items or [] if i.get("name")), None)
    title = f"{NEWS[status]}: {order.get('retailer') or 'your order'}"
    text = (name or (f"Order {order['order_number']}" if order.get("order_number") else "An order"))[:90]
    if status in ("shipped", "in_transit") and order.get("estimated_delivery"):
        text += f" · arrives {order['estimated_delivery'][:10]}"
    return title, text


def why_off():
    """Why alerts can't go out from this Mac right now, or None when they can."""
    if os.environ.get("PHONE_ACCESS") != "1":
        return "Phone access is off on the Mac (Setup, Phone access)."
    if os.environ.get("PHONE_ALERTS", "1") == "0":
        return "Phone alerts are turned off (PHONE_ALERTS=0)."
    if not os.environ.get("PHONE_PAIR_TOPIC"):
        return "The Mac has no pairing code yet: turn phone access off and on again on Setup."
    return None


def test(post=None):
    """Send a test alert now. Returns None when ntfy.sh took it, else what went wrong."""
    t = topic()
    if not t:
        return why_off()
    body = json.dumps({"topic": t, "title": "BotTrack test alert",
                       "message": "Shipping alerts reach this phone.", "tags": ["bottrack"], "priority": 3})
    try:
        req = urllib.request.Request(NTFY, data=body.encode(), method="POST", headers={"Content-Type": "application/json"})
        (post or urllib.request.urlopen)(req, timeout=15).close()
        logger.info("test phone alert sent")
        return None
    except Exception as e:
        logger.warning("couldn't send the test alert: %s", e)
        return f"Couldn't reach ntfy.sh from the Mac: {e}"


def order_moved(order, old, new, email_date, post=None):
    """Called when an email or the carrier's tracking changes an order's status. Sends the alert in the background;
    returns True if one goes."""
    t = topic()
    if not t or new == old or new not in NEWS or not _recent(email_date):
        return False
    title, text = message(order, new)
    # The app opens /tracking at the order named by its tag.
    tags = ["bottrack"] + ([f"order-{order['id']}"] if order.get("id") else [])
    body = json.dumps({"topic": t, "title": title, "message": text, "tags": tags,
                       "priority": 4 if new in ("out_for_delivery", "delivered", "exception") else 3})

    def send():
        try:
            req = urllib.request.Request(NTFY, data=body.encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            (post or urllib.request.urlopen)(req, timeout=15).close()
            logger.info("phone alert sent: %s", title)
        except Exception as e:
            logger.warning("couldn't send the phone alert: %s", e)

    if post:
        send()
    else:
        threading.Thread(target=send, daemon=True).start()
    return True
