from jigstream import app as appmod, carriers, db
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
