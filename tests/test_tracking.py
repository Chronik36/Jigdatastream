import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from jigstream import db, tracking
from jigstream.app import create_app
from jigstream.mail import Email
from jigstream.orderstore import Store

SAMPLE = {"data": {"trackings": [{
    "tracker": {"trackerId": "trk-1", "trackingNumber": "1Z999AA10123456784"},
    "shipment": {"statusMilestone": "out_for_delivery", "delivery": {"estimatedDeliveryDate": "2026-10-22T00:00:00"}},
    "events": [
        {"occurrenceDatetime": "2026-10-21T08:00:00", "status": "Out For Delivery Today",
         "location": "Austin, TX", "statusMilestone": "out_for_delivery"},
        {"occurrenceDatetime": "2026-10-20T08:00:00", "status": "Departed Facility",
         "location": "Dallas, TX", "statusMilestone": "in_transit"},
    ],
}]}}


def order(store, number="1Z999AA10123456784", status="shipped", mid="<a>"):
    store.save(Email(mid, "Nike", "s", "2026-10-19T00:00:00", "", "me@gmail.com"), {
        "kind": "order", "retailer": "Nike", "order_number": mid, "order_date": "2026-10-18", "items": [],
        "total": 100.0, "currency": "USD", "shipping_address": None, "tracking_number": number, "carrier": "UPS",
        "shipping_status": status, "estimated_delivery": None})


class FakeProvider:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or tracking.parse_tracking(SAMPLE["data"]["trackings"][0])
        self.error = error

    def register(self, number, carrier):
        self.calls.append(("register", number, carrier))
        if self.error:
            raise tracking.TrackingError(self.error)
        return "trk-1", self.result

    def results(self, tracker_id):
        self.calls.append(("results", tracker_id))
        return self.result


def test_parse_ship24_payload():
    r = tracking.parse_tracking(SAMPLE["data"]["trackings"][0])
    assert r["status"] == "out_for_delivery" and r["estimated_delivery"] == "2026-10-22"
    assert r["events"][0] == {"time": "2026-10-21T08:00:00", "status": "out_for_delivery",
                              "location": "Austin, TX", "description": "Out For Delivery Today"}


def test_refresh_registers_once_then_reuses_tracker():
    con = db.connect(":memory:")
    store = Store(con)
    order(store)
    order(store, number="9400100000000000000000", status="delivered", mid="<b>")  # done: never checked
    p = FakeProvider()
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    assert tracking.refresh(con, p, now=now, log=lambda *a: None) == 1
    o = {x["order_number"]: x for x in store.orders()}["<a>"]
    assert o["shipping_status"] == "out_for_delivery" and o["estimated_delivery"] == "2026-10-22"
    assert tracking.latest_events(con)["1Z999AA10123456784"]["location"] == "Austin, TX"

    assert tracking.refresh(con, p, now=now + timedelta(hours=1), log=lambda *a: None) == 0  # too soon
    tracking.refresh(con, p, now=now + timedelta(hours=5), log=lambda *a: None)
    assert p.calls == [("register", "1Z999AA10123456784", "UPS"), ("results", "trk-1")]
    assert con.execute("SELECT COUNT(*) FROM shipment_events").fetchone()[0] == 2  # no duplicate scans

    # Live status never rolls back, and delivered parcels stop being refreshed.
    p.result = dict(p.result, status="in_transit")
    tracking.refresh(con, p, now=now + timedelta(hours=10), log=lambda *a: None)
    assert store.orders()[0]["shipping_status"] in ("out_for_delivery", "delivered")
    p.result = dict(p.result, status="delivered")
    tracking.refresh(con, p, now=now + timedelta(hours=15), log=lambda *a: None)
    calls = len(p.calls)
    tracking.refresh(con, p, now=now + timedelta(hours=20), log=lambda *a: None)
    assert len(p.calls) == calls


def test_email_cannot_undo_delivered():
    con = db.connect(":memory:")
    store = Store(con)
    order(store, status="delivered", mid="<x>")
    store.save(Email("<late>", "Nike", "s", "2026-10-25T00:00:00", "", ""), {
        "kind": "order", "retailer": "Nike", "order_number": "<x>", "order_date": None, "items": [],
        "total": None, "currency": "USD", "shipping_address": None, "tracking_number": None, "carrier": None,
        "shipping_status": "shipped", "estimated_delivery": None})
    assert store.orders()[0]["shipping_status"] == "delivered"


def test_bad_key_stops_after_first_error():
    con = db.connect(":memory:")
    store = Store(con)
    order(store, mid="<a>")
    order(store, number="9400100000000000000001", mid="<b>")
    p = FakeProvider(error="Ship24 401: unauthorized")
    assert tracking.refresh(con, p, log=lambda *a: None) == 0
    assert len(p.calls) == 1


def test_ship24_client_http(monkeypatch):
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers["Authorization"],
                         json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self._reply()

        def do_GET(self):
            seen.append((self.path, self.headers["Authorization"], None))
            self._reply()

        def _reply(self):
            out = json.dumps(SAMPLE).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(tracking, "API", f"http://127.0.0.1:{server.server_port}/public/v1")
    client = tracking.Ship24("key123")
    tracker_id, result = client.register("1Z999AA10123456784", "UPS")
    assert tracker_id == "trk-1" and result["status"] == "out_for_delivery"
    client.results("trk-1")
    server.shutdown()
    assert seen[0] == ("/public/v1/trackers/track", "Bearer key123",
                       {"trackingNumber": "1Z999AA10123456784", "courierCode": ["ups"]})
    assert seen[1][0] == "/public/v1/trackers/trk-1/results"


def test_orders_page_shows_latest_scan(tmp_path, monkeypatch):
    monkeypatch.delenv("SHIP24_API_KEY", raising=False)
    path = str(tmp_path / "w.db")
    con = db.connect(path)
    order(Store(con))
    tracking.refresh(con, FakeProvider(), log=lambda *a: None)
    page = create_app(path).test_client().get("/orders").get_data(as_text=True)
    assert "Out For Delivery Today · Austin, TX" in page
    assert "SHIP24_API_KEY" in page
