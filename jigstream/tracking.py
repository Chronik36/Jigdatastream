"""Optional live carrier tracking through Ship24 (https://www.ship24.com).

Set SHIP24_API_KEY to turn it on. Each tracking number is registered once
(one shipment on Ship24's per-shipment plans); later refreshes read the
existing tracker's results, and refreshing stops once a parcel is delivered,
returned or cancelled. Without a key, status comes from emails only.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import load_dotenv
from .extract import status_advances

API = "https://api.ship24.com/public/v1"
FINAL = ("delivered", "cancelled", "returned", "refunded")

# Ship24 status milestones -> our shipping statuses.
MILESTONES = {
    "info_received": "shipped",
    "in_transit": "in_transit",
    "failed_attempt": "in_transit",
    "out_for_delivery": "out_for_delivery",
    "available_for_pickup": "out_for_delivery",
    "delivered": "delivered",
    "exception": "exception",
}
COURIER_CODES = {"UPS": "ups", "USPS": "us-post", "FedEx": "fedex", "DHL": "dhl", "OnTrac": "ontrac"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS shipments (
    tracking_number TEXT PRIMARY KEY,
    tracker_id TEXT,
    status TEXT,
    estimated_delivery TEXT,
    last_checked TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS shipment_events (
    tracking_number TEXT,
    time TEXT,
    status TEXT,
    location TEXT,
    description TEXT,
    UNIQUE (tracking_number, time, description)
);
"""


class TrackingError(RuntimeError):
    pass


class Ship24:
    def __init__(self, api_key, timeout=30):
        self.api_key = api_key
        self.timeout = timeout

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise TrackingError(f"Ship24 {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise TrackingError(f"Could not reach Ship24: {e}") from e

    def register(self, tracking_number, carrier=None):
        """Create a tracker and return (tracker_id, result)."""
        body = {"trackingNumber": tracking_number}
        if COURIER_CODES.get(carrier or ""):
            body["courierCode"] = [COURIER_CODES[carrier]]
        tracking = _first_tracking(self._call("POST", "/trackers/track", body))
        return (tracking.get("tracker") or {}).get("trackerId"), parse_tracking(tracking)

    def results(self, tracker_id):
        return parse_tracking(_first_tracking(self._call("GET", f"/trackers/{tracker_id}/results")))


def _first_tracking(payload):
    trackings = ((payload or {}).get("data") or {}).get("trackings") or []
    return trackings[0] if trackings else {}


def parse_tracking(tracking):
    shipment = tracking.get("shipment") or {}
    milestone = shipment.get("statusMilestone")
    events = []
    for e in tracking.get("events") or []:
        events.append({
            "time": e.get("occurrenceDatetime") or e.get("datetime"),
            "status": MILESTONES.get(e.get("statusMilestone"), e.get("statusMilestone")),
            "location": e.get("location"),
            "description": e.get("status"),
        })
    eta = (shipment.get("delivery") or {}).get("estimatedDeliveryDate")
    return {
        "status": MILESTONES.get(milestone),
        "estimated_delivery": eta[:10] if eta else None,
        "events": events,
    }


def provider_from_env():
    load_dotenv()
    key = os.environ.get("SHIP24_API_KEY", "").strip()
    return Ship24(key) if key else None


def refresh(con, provider, min_hours=4, now=None, log=print):
    """Refresh live status for every undelivered order with a tracking number."""
    con.executescript(SCHEMA)
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=min_hours)).isoformat()
    rows = con.execute(
        "SELECT o.id, o.tracking_number, o.carrier, o.shipping_status, s.tracker_id FROM tracked_orders o"
        " LEFT JOIN shipments s ON s.tracking_number = o.tracking_number"
        " WHERE o.tracking_number IS NOT NULL"
        f" AND COALESCE(o.shipping_status, '') NOT IN ({','.join('?' * len(FINAL))})"
        " AND (s.last_checked IS NULL OR s.last_checked < ?)",
        (*FINAL, cutoff),
    ).fetchall()
    updated = 0
    for row in rows:
        number = row["tracking_number"]
        try:
            if row["tracker_id"]:
                tracker_id, result = row["tracker_id"], provider.results(row["tracker_id"])
            else:
                tracker_id, result = provider.register(number, row["carrier"])
        except TrackingError as e:
            con.execute(
                "INSERT INTO shipments (tracking_number, last_checked, error) VALUES (?,?,?)"
                " ON CONFLICT(tracking_number) DO UPDATE SET last_checked = excluded.last_checked,"
                " error = excluded.error",
                (number, now.isoformat(), str(e)[:300]),
            )
            con.commit()
            log(f"  ! {number}: {e}")
            if "401" in str(e) or "403" in str(e) or "Could not reach" in str(e):
                break  # bad key or offline: no point trying the rest
            continue
        con.execute(
            "INSERT INTO shipments VALUES (?,?,?,?,?,NULL) ON CONFLICT(tracking_number) DO UPDATE SET"
            " tracker_id = excluded.tracker_id, status = excluded.status,"
            " estimated_delivery = excluded.estimated_delivery, last_checked = excluded.last_checked, error = NULL",
            (number, tracker_id, result["status"], result["estimated_delivery"], now.isoformat()),
        )
        for e in result["events"]:
            con.execute(
                "INSERT OR IGNORE INTO shipment_events VALUES (?,?,?,?,?)",
                (number, e["time"], e["status"], e["location"], e["description"]),
            )
        if status_advances(row["shipping_status"], result["status"]):
            con.execute("UPDATE tracked_orders SET shipping_status = ? WHERE id = ?", (result["status"], row["id"]))
        if result["estimated_delivery"]:
            con.execute("UPDATE tracked_orders SET estimated_delivery = ? WHERE id = ?",
                        (result["estimated_delivery"], row["id"]))
        con.commit()
        updated += 1
    return updated


def latest_events(con):
    """Most recent scan per tracking number, for the Orders page."""
    con.executescript(SCHEMA)
    rows = con.execute(
        "SELECT e.* FROM shipment_events e JOIN (SELECT tracking_number, MAX(time) t FROM shipment_events"
        " GROUP BY tracking_number) m ON m.tracking_number = e.tracking_number AND m.t = e.time"
    ).fetchall()
    return {r["tracking_number"]: dict(r) for r in rows}


def start_background_refresh(connect, hours=None, log=print):
    """Refresh tracking every few hours in a daemon thread while the dashboard runs."""
    provider = provider_from_env()
    if not provider:
        return None
    hours = hours or float(os.environ.get("TRACKING_REFRESH_HOURS", 4))

    def loop():
        while True:
            try:
                refresh(connect(), provider, min_hours=hours, log=log)
            except Exception as e:  # keep the loop alive through network hiccups
                log(f"Tracking refresh failed: {e}")
            time.sleep(hours * 3600)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
