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


def test_logs_and_ideas_for_the_developer_only(tmp_path, monkeypatch):
    from jigstream import inbox
    path = str(tmp_path / "d.db")
    Store(db.connect(path))
    rows = [{"message_id": "<l1@x>", "subject": "BotTrack logs (build 120)", "sender": "Friend <f@gmail.com>",
             "date": "2026-10-27T10:00:00", "account": "me@gmail.com"},
            {"message_id": "<s1@x>", "subject": "BotTrack provider suggestion: Oculus", "sender": "me@gmail.com",
             "date": "2026-10-26T10:00:00", "account": "me@gmail.com"}]
    monkeypatch.setattr(inbox, "search", lambda config, q, limit=50, after=None: (rows, [], None))
    monkeypatch.setattr(inbox, "open_email", lambda config, a, mid: type("E", (), {
        "subject": "BotTrack logs", "sender": "f@gmail.com", "date": "", "body": "What went wrong: sync stuck"})())
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@gmail.com:pw")
    c = create_app(path).test_client()
    monkeypatch.delenv("BOTTRACK_SIGNING_KEY", raising=False)
    assert c.get("/developer/mail").status_code == 404
    monkeypatch.setenv("BOTTRACK_SIGNING_KEY", "k")
    page = c.get("/developer/mail").get_data(as_text=True)
    assert "BotTrack logs (build 120)" in page and "Provider ideas · 1" in page and "from your own copy" in page
    assert "Logs &amp; ideas" in page and page.count(">New<") == 2
    assert "sync stuck" in c.get("/developer/mail/read?account=me@gmail.com&id=<l1@x>").get_json()["body"]
    assert c.get("/developer/mail").get_data(as_text=True).count(">New<") == 1


def test_orders_cards_open_their_orders(tmp_path):
    path = str(tmp_path / "o.db")
    store = Store(db.connect(path))
    order(store, 1, "ordered")
    order(store, 2, "in_transit")
    order(store, 3, "delivered")
    order(store, 4, "cancelled")
    ids = {o["order_number"]: o["id"] for o in store.orders()}
    c = create_app(path).test_client()
    page = c.get("/orders").get_data(as_text=True)
    assert 'href="/orders?show=moving"' in page and 'href="/orders?show=delivered"' in page
    moving = c.get("/orders?show=moving").get_data(as_text=True)
    assert f'id="order-{ids["N2"]}"' in moving and f'id="order-{ids["N1"]}"' not in moving and "Show all" in moving
    spent = c.get("/orders?show=spent").get_data(as_text=True)
    assert f'id="order-{ids["N4"]}"' not in spent and f'id="order-{ids["N1"]}"' in spent
    gone = c.get("/orders?show=not_spent").get_data(as_text=True)
    assert f'id="order-{ids["N4"]}"' in gone and f'id="order-{ids["N3"]}"' not in gone


def test_every_stat_card_is_a_link(tmp_path):
    import re
    path = str(tmp_path / "c.db")
    store = Store(db.connect(path))
    order(store, 1, "in_transit")
    c = create_app(path).test_client()
    for page in ("/", "/orders", "/drop", "/drop?base=all", "/stuck", "/drop/cards", "/expenses", "/expenses/budget",
                 "/expenses/recurring", "/bank", "/tracking", "/training/share"):
        html = c.get(page).get_data(as_text=True)
        for block in re.findall(r'<div class="stats[^"]*">(.*?)</div>\s*(?:<|$)', html, re.S):
            assert '<div class="card' not in block, page  # every card opens what it counts
    assert "Show all" in c.get("/expenses/recurring?show=soon").get_data(as_text=True)
    assert "only ✕" in c.get("/drop?only=decline").get_data(as_text=True)
    assert c.get("/bank?show=orders").status_code == 200


def test_orders_filter_by_base_and_product(tmp_path):
    from jigstream import bases
    path = str(tmp_path / "f.db")
    store = Store(db.connect(path))
    order(store, 1, "in_transit")
    bases.ensure(store.db)
    store.db.commit()
    c = create_app(path).test_client()
    page = c.get("/orders?status=in_transit&product=thing").get_data(as_text=True)
    assert 'id="prf"' in page and 'data-items="' in page and 'data-param="status"' in page
    assert "<option value=\"in_transit\" selected" in page and "<option value=\"thing\" selected" in page
