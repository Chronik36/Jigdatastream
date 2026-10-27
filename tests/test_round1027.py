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


def test_go_through_expense_emails_one_by_one(tmp_path):
    path = str(tmp_path / "x.db")
    store = Store(db.connect(path))
    def from_email(mid, vendor, amount, category, day):
        return store.db.execute("INSERT INTO expenses (message_id, account, vendor, amount, category, date)"
                                " VALUES (?, 'me@gmail.com', ?, ?, ?, ?)", (mid, vendor, amount, category, day)).lastrowid
    a = from_email("<a@x>", "Webshare", 30.0, "proxy", "2026-10-20")
    b = from_email("<b@x>", "Shell", 4.5, "other", "2026-10-19")
    store.add_expense({"vendor": "Typed", "amount": 1.0, "category": "other"})
    store.db.commit()
    c = create_app(path).test_client()
    page = c.get("/expenses/review").get_data(as_text=True)
    assert "Webshare" in page and "Typed" not in page and 'value="gas"' in page and 'value="shipping"' in page
    r = c.post(f"/expenses/{b}/review", data={"vendor": "Shell", "amount": "45.00", "category": "gas",
                                              "next": str(a), "show": ""})
    assert r.status_code == 302 and f"id={a}" in r.headers["Location"]
    e = Store(db.connect(path)).expense(b)
    assert (e["amount"], e["category"], e["reviewed"]) == (45.0, "gas", 1)
    page = c.get("/expenses/review").get_data(as_text=True)
    assert "1</b> left to go through" in page and "1 done" in page


def test_tradepost_trade_ins_are_read_as_sales():
    from jigstream.config import Config
    from jigstream.extract import email_prompt, looks_like_purchase, platforms_named
    from jigstream.sync import find_sales, sale_query
    from test_orders import FakeLLM, mail as email
    e = email("Your trade-in is complete", sender="Trade Post <hello@tradepostapp.com>",
              body="We received your cards. $84.50 has been sent to your account.", mid="<tp1@x>")
    assert platforms_named(e) == ["Tradepost"] and looks_like_purchase(e)
    assert "selling platform: Tradepost" in email_prompt(e)
    assert '"trade post"' in sale_query(["StockX", "Tradepost"])
    store = Store(db.connect(":memory:"))
    store.record_email(e, "other")  # an older sync called it not relevant
    store.db.commit()
    llm = FakeLLM({"Your trade-in is complete": {"kind": "sale", "platform": "Tradepost", "payout": 84.5}})
    counts = find_sales(Config(accounts=[("me@gmail.com", "pw")], use_examples=False), store, llm=llm,
                        fetch=lambda config, a, p, days, skip: iter([e]))
    assert counts["sale"] == 1
    assert [(r["platform"], r["payout"]) for r in store.db.execute("SELECT * FROM sales")] == [("Tradepost", 84.5)]


def test_sold_in_person_from_an_order(tmp_path):
    path = str(tmp_path / "s.db")
    store = Store(db.connect(path))
    order(store, 1, "delivered")
    oid = store.orders()[0]["id"]
    c = create_app(path).test_client()
    assert f"/expenses/in-person?order={oid}" in c.get(f"/orders/{oid}/sheet").get_data(as_text=True)
    page = c.get(f"/expenses/in-person?order={oid}").get_data(as_text=True)
    assert f'<option value="{oid}" selected>' in page and 'id="ipfind"' in page
