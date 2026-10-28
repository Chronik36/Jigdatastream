import pytest

from jigstream import app as appmod, browsertrack, carriers, db
from jigstream.extract import normalize
from jigstream.mail import Email
from jigstream.orderstore import Store

ENV = {"UPS_CLIENT_ID": "id", "UPS_CLIENT_SECRET": "secret"}


def test_status_from_carrier_wording():
    s = carriers.status_from_text
    assert s("Delivered") == "delivered"
    assert s("Out For Delivery Today") == "out_for_delivery"
    assert s("On the Way") == "in_transit"
    assert s("Label Created") == "processing"
    assert s("Delivery Attempted") == "exception"
    assert s("Returned to Sender") == "returned"
    assert s("Estimated delivery Friday") == "in_transit"


def order(store, tracking, status="shipped", carrier="UPS"):
    mail = Email(f"<{tracking}>", "Nike <n@nike.com>", "Shipped", "2026-10-20T10:00:00+00:00", "x", "me@gmail.com")
    store.save(mail, normalize({"kind": "order", "retailer": "Nike", "order_number": tracking,
                                "tracking_number": tracking, "carrier": carrier, "shipping_status": status}, mail))


def test_refresh_moves_open_orders_forward_only(tmp_path, monkeypatch):
    con = db.connect(str(tmp_path / "t.db"))
    store = Store(con)
    order(store, "1Z999AA10123456784")
    order(store, "1Z999AA10123456785", status="out_for_delivery")
    order(store, "1Z999AA10123456786", status="delivered")
    order(store, "9400111899223033005282", carrier="USPS")  # no USPS key: skipped
    asked = []

    def fake_ups(number, env):
        asked.append(number)
        return ("delivered", "Delivered") if number.endswith("4") else ("in_transit", "On the Way")
    monkeypatch.setitem(carriers.TRACKERS, "UPS", fake_ups)
    monkeypatch.setattr(carriers.time, "sleep", lambda s: None)
    assert carriers.refresh(con, ENV) == (2, 1)
    assert sorted(asked) == ["1Z999AA10123456784", "1Z999AA10123456785"]
    by = {o["tracking_number"]: o for o in store.orders()}
    assert by["1Z999AA10123456784"]["shipping_status"] == "delivered"
    assert by["1Z999AA10123456785"]["shipping_status"] == "out_for_delivery"  # never moves back
    assert by["1Z999AA10123456784"]["carrier_status"] == "Delivered"


def test_rejected_key_is_not_saved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("UPS_CLIENT_ID", "UPS_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)

    def refuse(carrier, env):
        raise carriers.CarrierError("HTTP 401")
    monkeypatch.setattr(carriers, "_token", refuse)
    client = appmod.create_app(str(tmp_path / "a.db")).test_client()
    client.post("/setup/carriers", data={"carrier": "UPS", "key": "k", "secret": "s"})
    assert not (tmp_path / ".env").exists() or "UPS_CLIENT_ID=k" not in (tmp_path / ".env").read_text()
    assert b"Live package tracking" in client.get("/setup").data


UPS_PAGE = """Skip to Main Content
Shipping
Tracking
Delivered
Delivered On: Tuesday, 10/21/2026 at 2:14 P.M.
Label Created
We Have Your Package
On the Way
Out for Delivery
Delivered
Help and Support"""


def test_page_headline_skips_the_progress_bar():
    h = browsertrack.headline
    assert h(UPS_PAGE) == "Delivered"
    assert h("Track Another Package\nLabel Created\nOn the Way\nOut for Delivery\nDelivered\n\n\n"
             "Status\nMoving Through Network\nIn Transit to Next Facility, Arriving Late") == "Moving Through Network"
    assert h("Delivery & Pickup\nDelivered, In/At Mailbox\nYour item was delivered") == "Delivered, In/At Mailbox"
    assert h("Shipping\nTracking\nSupport") is None
    assert carriers.status_from_text(h("Out for Delivery, Expected Delivery by 9:00pm")) == "out_for_delivery"


def test_refresh_reads_carrier_pages_without_keys(tmp_path, monkeypatch):
    con = db.connect(str(tmp_path / "t.db"))
    store = Store(con)
    order(store, "1Z999AA10123456784")
    order(store, "1Z999AA10123456785")
    order(store, "9400111899223033005282", carrier="USPS")
    order(store, "123456789012", carrier="FedEx")
    opened = []

    def reader(url, done, timeout):
        opened.append(url)
        if "fedex" in url:
            return "Access Denied"
        page = UPS_PAGE if url.endswith("4&loc=en_US") else "Status\nOut for Delivery\nToday by 8pm"
        assert done(page)
        return page
    monkeypatch.setattr(browsertrack, "_reader", reader)
    monkeypatch.setattr(carriers.time, "sleep", lambda s: None)
    assert carriers.available({}) == ["UPS", "USPS", "FedEx"]
    assert carriers.available({"BROWSER_TRACKING": "0"}) == []
    assert carriers.refresh(con, {}) == (3, 3)
    assert len(opened) == 4
    by = {o["tracking_number"]: o for o in store.orders()}
    assert by["1Z999AA10123456784"]["shipping_status"] == "delivered"
    assert by["9400111899223033005282"]["shipping_status"] == "out_for_delivery"
    assert by["123456789012"]["shipping_status"] == "shipped"  # robot check: keeps the email status


def test_keys_win_over_pages(tmp_path, monkeypatch):
    con = db.connect(str(tmp_path / "t.db"))
    order(Store(con), "1Z999AA10123456784")
    monkeypatch.setattr(browsertrack, "_reader", lambda *a: pytest.fail("opened a page for a keyed carrier"))
    monkeypatch.setitem(carriers.TRACKERS, "UPS", lambda n, env: ("in_transit", "On the Way"))
    monkeypatch.setattr(carriers.time, "sleep", lambda s: None)
    assert carriers.refresh(con, ENV) == (1, 1)


def test_out_for_delivery_round_only_asks_about_those(tmp_path, monkeypatch):
    con = db.connect(str(tmp_path / "t.db"))
    store = Store(con)
    order(store, "1Z999AA10123456784")
    order(store, "1Z999AA10123456785", status="out_for_delivery")
    asked = []

    def fake_ups(number, env):
        asked.append(number)
        return "delivered", "Delivered"
    monkeypatch.setitem(carriers.TRACKERS, "UPS", fake_ups)
    monkeypatch.setattr(carriers.time, "sleep", lambda s: None)
    assert carriers.out_for_delivery(con) == 1
    carriers.run(con, ENV, ("out_for_delivery",))
    assert asked == ["1Z999AA10123456785"]
    assert carriers.out_for_delivery(con) == 0
