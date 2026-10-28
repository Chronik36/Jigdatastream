from jigstream import addresscheck, db
from jigstream.app import create_app
from jigstream.extract import normalize
from jigstream.mail import Email
from jigstream.orderstore import Store

from test_profile_match import profile


def order(store, mid, to, address, date="2026-10-20T16:00:00+00:00", number=None, retailer="Target"):
    e = Email(f"<{mid}@x>", f"{retailer} <orders@x.com>", "Thanks for your order", date, "Total $50.00",
              "inbox@gmail.com", to)
    store.save(e, normalize({"kind": "order", "retailer": retailer, "order_number": number or mid,
                             "shipping_address": address, "shipping_status": "ordered"}, e))
    return store.db.execute("SELECT id FROM tracked_orders WHERE order_number = ?", (number or mid,)).fetchone()[0]


def test_flags_orders_shipped_away_from_the_profiles_address(tmp_path):
    c = db.connect(str(tmp_path / "m.db"))
    store = Store(c)
    profile(c, "T1", "Jacob", "Smith", "j.smith+t1@gmail.com", "XJ 123 Main Street Apt 4")
    good = order(store, "A1001", "jsmith@gmail.com", "Jacob Smith, XJ 123 Main St Apt 4, Austin, TX 78701")
    bad = order(store, "A1002", "jsmith@gmail.com", "Jacob Smith, 55 Old Rd, Austin, TX 78702")
    nobody = order(store, "A1003", "other@gmail.com", "Someone, 1 Elm St, Austin, TX 78701")
    wrong = addresscheck.check(c, store.orders())
    assert set(wrong) == {bad}
    assert wrong[bad]["profiles"] == ["T1"] and "123 Main Street" in wrong[bad]["expected"]
    assert good not in wrong and nobody not in wrong

    app = create_app(str(tmp_path / "m.db"))
    cl = app.test_client()
    page = cl.get("/orders?day=all").get_data(as_text=True)
    assert "Wrong address" in page and "Not the profile's address" in page
    page = cl.get("/orders?show=wrong_address").get_data(as_text=True)
    assert f'id="order-{bad}"' in page and f'id="order-{good}"' not in page
    assert "Not the profile's address" in cl.get(f"/orders/{bad}/sheet").get_data(as_text=True)
    cl.post(f"/orders/{bad}/address-ok", data={"back": "/orders?day=all"})
    assert addresscheck.check(c, store.orders()) == {}


def test_orders_and_shipping_open_on_drop_days(tmp_path):
    c = db.connect(str(tmp_path / "m.db"))
    store = Store(c)
    a = order(store, "B1", "x@gmail.com", "1 Elm St, Austin, TX 78701", date="2026-10-20T16:00:00+00:00")
    b = order(store, "B2", "x@gmail.com", "1 Elm St, Austin, TX 78701", date="2026-10-22T16:00:00+00:00",
              retailer="Walmart")
    cl = create_app(str(tmp_path / "m.db")).test_client()
    page = cl.get("/orders").get_data(as_text=True)
    assert "By drop date" in page and 'href="/orders?day=2026-10-22"' in page and f'id="order-{a}"' not in page
    assert page.index("2026-10-22") < page.index("2026-10-20")  # newest day first
    page = cl.get("/orders?day=2026-10-22").get_data(as_text=True)
    assert f'id="order-{b}"' in page and f'id="order-{a}"' not in page and "All drop dates" in page
    page = cl.get("/tracking").get_data(as_text=True)
    assert "By drop date" in page and 'class="scard' not in page
    page = cl.get("/tracking?day=2026-10-20").get_data(as_text=True)
    assert f'id="order-{a}"' in page and f'id="order-{b}"' not in page
    assert cl.get("/review?src=day&name=2026-10-20").status_code == 200
