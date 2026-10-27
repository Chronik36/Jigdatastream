from jigstream import db
from jigstream.app import create_app
from jigstream.orderstore import Store

from test_profile_match import profile
from test_round4 import order


def test_edit_everything_on_an_order_and_stay_on_the_page(tmp_path, monkeypatch):
    path = str(tmp_path / "e.db")
    store = Store(db.connect(path))
    order(store, 1, "in_transit", tracking_number="1Z999AA10123456784", carrier="UPS")
    profile(store.db, "Jig 7", "Jacob", "Smith", "j@x.com", "123 Main Street")
    store.db.execute("UPDATE profiles SET group_name='Target jigs' WHERE name='Jig 7'")
    store.db.commit()
    oid = store.orders()[0]["id"]
    app = create_app(path)
    relabels = []
    monkeypatch.setattr(app, "relabel", lambda st, o, status: relabels.append(status))
    c = app.test_client()
    sheet = c.get(f"/orders/{oid}/sheet").get_data(as_text=True)
    assert 'optgroup label="Target jigs"' in sheet and "What its emails are" in sheet
    r = c.post(f"/orders/{oid}/edit", data={"item_name": "Pokemon ETB", "retailer": "Target", "order_number": "N9",
                                            "total": "$54.99", "shipping_address": "1 Elm St, Austin, TX 78701",
                                            "tracking_number": "1Z999AA10123456784", "carrier": "UPS",
                                            "status": "delivered", "profile": "Jig 7", "back": "1"},
               headers={"Referer": "https://localhost/tracking?lane=moving"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/tracking?lane=moving")  # stays on the page
    o = Store(db.connect(path)).order(oid)
    assert (o["order_number"], o["total"], o["profile_name"], o["items"][0]["name"]) == ("N9", 54.99, "Jig 7", "Pokemon ETB")
    assert o["tracking_url"] is None or o["tracking_number"] == "1Z999AA10123456784"
    assert relabels == ["delivered"]
    c.post(f"/orders/{oid}/edit", data={"email_type": "not_an_order"})
    assert Store(db.connect(path)).order(oid) is None


def test_shipping_tiles_open_their_packages(tmp_path):
    path = str(tmp_path / "t.db")
    store = Store(db.connect(path))
    order(store, 1, "ordered")
    order(store, 2, "in_transit", estimated_delivery="2020-01-01")
    order(store, 3, "delivered", day="2099-01-01")
    ids = {o["order_number"]: o["id"] for o in store.orders()}
    c = create_app(path).test_client()
    page = c.get("/tracking?lane=moving").get_data(as_text=True)
    assert f'id="order-{ids["N2"]}"' in page and f'id="order-{ids["N1"]}"' not in page
    page = c.get("/tracking?lane=late").get_data(as_text=True)
    assert f'id="order-{ids["N2"]}"' in page and "Running late" in page and f'id="order-{ids["N1"]}"' not in page
