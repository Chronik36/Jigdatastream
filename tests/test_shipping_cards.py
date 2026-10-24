from datetime import date

from jigstream import db, ordersheet, tracking
from jigstream.app import create_app
from jigstream.orderstore import Store
from tests.test_round4 import order


def test_arrival_headlines():
    today = date(2026, 10, 24)
    o = lambda status, eta=None: {"shipping_status": status, "estimated_delivery": eta}  # noqa: E731
    assert tracking.arrival(o("shipped", "2026-10-25"), today) == ("Arrives tomorrow", "Sun, Oct 25")
    assert tracking.arrival(o("in_transit", "2026-10-24"), today)[0] == "Arrives today"
    assert tracking.arrival(o("in_transit", "2026-10-27"), today)[0] == "Arrives Tuesday"
    assert tracking.arrival(o("in_transit", "2026-10-20"), today)[0] == "Running late, was due Oct 20"
    assert tracking.arrival(o("delivered", "2026-10-20"), today)[0] == "Delivered"
    assert tracking.arrival(o("ordered"), today) == ("Waiting for the store to ship", "")


def test_cards_show_events_dropdown(tmp_path):
    path = str(tmp_path / "s.db")
    store = Store(db.connect(path))
    order(store, 1, "in_transit", tracking_number="9200190000000000000000001", carrier="USPS",
          estimated_delivery="2026-10-25", items=[{"name": "Squishmallow Reindoh", "quantity": 1}])
    oid = store.orders()[0]["id"]
    ordersheet.ensure(store.db)
    for i in range(8):
        ordersheet.note_carrier(store.db, oid, f"Event number {i}")
    store.db.commit()
    page = create_app(path).test_client().get("/tracking").get_data(as_text=True)
    assert "Squishmallow Reindoh" in page and "Latest events" in page
    assert 'data-copy="9200190000000000000000001"' in page
    assert "+ 3 more" in page  # 8 carrier lines + the order email, 6 shown
    assert "Expected delivery" in page
