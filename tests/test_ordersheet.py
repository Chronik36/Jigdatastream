import json

from jigstream import db, ordersheet
from jigstream.app import create_app
from jigstream.mail import Email, images_in, parse_message
from jigstream.orderstore import Store, with_pictures

ETB = "Pokemon Trading Card Game 30th Celebration Elite Trainer Box"
HTML = (f'<img src="https://target.scene7.com/logo.png" alt="Target logo"><img src="https://t.co/p.gif" width="1" height="1">'
        f'<img src="https://target.scene7.com/is/image/Target/GUEST_123" alt="{ETB}" width="120"><p>Order Total: $151.18</p>')


def _order(con, **kw):
    fields = dict(merge_key="target:1", retailer="Target", order_number="1020", order_date="2026-10-15",
                  items=json.dumps([{"name": ETB, "quantity": 2}]), total=151.18, tracking_number="1ZWY0657YW09443220",
                  carrier="UPS", shipping_status="in_transit", message_ids=json.dumps(["<a>"]), account="me@gmail.com")
    fields.update(kw)
    cur = con.execute(f"INSERT INTO tracked_orders ({', '.join(fields)}) VALUES ({','.join('?' * len(fields))})",
                      list(fields.values()))
    return cur.lastrowid


def test_product_pictures_come_from_the_email():
    pics = images_in(HTML)
    assert pics == [(ETB, "https://target.scene7.com/is/image/Target/GUEST_123")]
    items = [{"name": "Pokémon TCG: 30th Celebration Elite Trainer Box"}]
    assert with_pictures(items, pics) and items[0]["image"].endswith("GUEST_123")
    two = [{"name": "Charizard plush"}, {"name": "Booster bundle"}]
    assert not with_pictures(two, pics)  # nothing names them, and with two items a guess could be wrong
    one = [{"name": "Mystery item"}]
    assert with_pictures(one, [("", "https://target.scene7.com/is/image/Target/GUEST_9")])
    raw = (b"From: Target <orders@target.com>\r\nSubject: Thanks for your order\r\nMessage-ID: <x@t>\r\n"
           b"Content-Type: text/html\r\n\r\n" + HTML.encode())
    assert parse_message(raw).images == pics


def test_the_sheet_shows_the_order_and_its_timeline(tmp_path):
    app = create_app(str(tmp_path / "s.db"))
    con = Store(db.connect(str(tmp_path / "s.db"))).db
    oid = _order(con, shipping_name="Jacob B", shipping_address="1 Main St, Plattsburgh, NY 12901", estimated_delivery="2026-10-25")
    con.execute("INSERT INTO emails (message_id, subject, date, kind) VALUES ('<a>', 'Thanks for your order!', '2026-10-15T14:03:00+00:00', 'order')")
    ordersheet.note_carrier(con, oid, "On the way: Watertown, NY")
    ordersheet.note_carrier(con, oid, "On the way: Watertown, NY")  # said again: not a new line
    ordersheet.note_carrier(con, oid, "Couldn't check: timeout")
    events = ordersheet.timeline(con, Store(con).order(oid))
    assert [e["text"] for e in events] == ["On the way: Watertown, NY", "Thanks for your order!"]
    c = app.test_client()
    html = c.get(f"/orders/{oid}/sheet").get_data(as_text=True)
    for want in ("Order #1020", "Oct 15, 2026", "Oct 25, 2026", "$151.18", "1ZWY0657YW09443220", "Latest events · 2",
                 'data-copy="1020"', "Plattsburgh"):
        assert want in html, want
    assert c.get("/orders/999/sheet").status_code == 404
    assert 'data-sheet="%d"' % oid in c.get("/orders?day=all").get_data(as_text=True)


def test_rename_and_delete_from_the_sheet(tmp_path):
    app = create_app(str(tmp_path / "r.db"))
    con = Store(db.connect(str(tmp_path / "r.db"))).db
    oid = _order(con, items=json.dumps([{"name": ETB, "image": "https://x/p.jpg"}, {"name": "Second"}]))
    c = app.test_client()
    c.post(f"/orders/{oid}/rename", data={"name": "30th ETB"})
    items = json.loads(con.execute("SELECT items FROM tracked_orders WHERE id=?", (oid,)).fetchone()[0])
    assert items[0] == {"name": "30th ETB", "image": "https://x/p.jpg"} and items[1]["name"] == "Second"
    c.post(f"/orders/{oid}/delete")
    assert con.execute("SELECT COUNT(*) FROM tracked_orders").fetchone()[0] == 0


def test_older_orders_get_their_pictures_once(tmp_path):
    store = Store(db.connect(str(tmp_path / "b.db")))
    oid = _order(store.db)
    store.db.execute("INSERT INTO emails (message_id, account) VALUES ('<a>', 'me@gmail.com')")

    class Config:
        accounts = [("me@gmail.com", "pw")]
    opened = []

    def fetch(config, address, password, ids):
        opened.extend(ids)
        return [(m, Email(m, "Target", "Thanks", "", "", address, images=images_in(HTML))) for m in ids]
    assert ordersheet.backfill_pictures(Config(), store, fetch=fetch) == 1
    assert store.order(oid)["items"][0]["image"].endswith("GUEST_123")
    assert ordersheet.backfill_pictures(Config(), store, fetch=fetch) == 0 and opened == ["<a>"]


def test_an_ebay_sale_read_as_an_order_becomes_a_sale(tmp_path):
    from jigstream import sales
    app = create_app(str(tmp_path / "e.db"))
    con = Store(db.connect(str(tmp_path / "e.db"))).db
    bought = _order(con, merge_key="target:2", order_number="2001", order_date="2026-10-01", total=49.99,
                    message_ids=json.dumps(["<b>"]))
    oid = _order(con, merge_key="ebay:9", retailer="eBay", order_number="12-345", order_date="2026-10-20",
                 items=json.dumps([{"name": ETB}]), total=120.0, message_ids=json.dumps(["<sold>"]))
    sales.ensure(con).execute("INSERT INTO sales (message_id, platform, item, payout, source) VALUES ('<sold>', 'eBay', 'x', 5, 'email')")
    con.commit()
    c = app.test_client()
    html = c.get(f"/orders/{oid}/sheet").get_data(as_text=True)
    assert "It's a sale" in html and 'value="eBay"' in html and 'value="13.25"' in html
    r = c.post(f"/orders/{oid}/sale", data={"platform": "eBay", "item": ETB, "sale_price": "120", "fee_pct": "13.25",
                                            "bought": str(bought), "back": "/shipping"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/shipping")  # stays on the page
    assert con.execute("SELECT 1 FROM tracked_orders WHERE id = ?", (oid,)).fetchone() is None
    [s] = [dict(x) for x in con.execute("SELECT * FROM sales")]  # the AI's earlier read is replaced, not doubled
    assert (s["message_id"], s["sale_price"], s["fees"], s["payout"]) == ("<sold>", 120, 15.9, 104.1)
    assert (s["order_id"], s["cost"], s["link_manual"], s["source"]) == (bought, 49.99, 1, "fixed")
    assert sales.fee_pct(con, "ebay") == 13.25
    # a later read of the same email only fills blanks
    email = Email(message_id="<sold>", account="me@gmail.com", sender="eBay", subject="You sold it", date=None, body="")
    sales.save_email(con, email, {"platform": "eBay", "item": "Wrong", "sale_price": 1, "payout": 1, "size": "M"})
    s2 = dict(con.execute("SELECT * FROM sales").fetchone())
    assert (s2["item"], s2["payout"], s2["size"]) == (ETB, 104.1, "M")
    # a second email about the same sale adds no second sale
    sales.save_email(con, Email(message_id="<paid>", account="a", sender="eBay", subject="Payout", date=None, body=""),
                     {"platform": "eBay", "item": "Other thing", "payout": 104.1})
    sales.keep_one(con, s["id"], "<paid>")
    assert con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1


def test_sale_amounts_fill_in():
    from jigstream.sales import amounts
    assert amounts(100, 13.25) == (100, 13.25, 86.75)
    assert amounts(100, None, None, 80) == (100, 20, 80)
    assert amounts(None, 20, None, 80) == (100, 20, 80)
    assert amounts(100, 10, 5) == (100, 5, 95)  # typed fees win over the percent
    assert amounts(None, None, None, 50) == (None, None, 50)


def test_the_teaching_example_gets_the_typed_sale(monkeypatch):
    from jigstream import inbox
    email = Email(message_id="<s>", account="a", sender="eBay", subject="You made the sale for Elite Trainer Box",
                  date=None, body="Sold for $120.00. Fees $15.90. Your earnings: $104.10")
    fix = inbox._sale_fix(email, {"platform": "eBay", "item": "Elite Trainer Box", "sale_price": 120.0, "fees": 15.9,
                                  "payout": 104.1})
    assert fix == {"platform": "eBay", "sale_price": 120.0, "fees": 15.9, "payout": 104.1,
                   "items": [{"name": "Elite Trainer Box"}]}
    assert inbox._sale_fix(email, {"platform": "eBay", "payout": 99.0}) == {"platform": "eBay"}


def test_sale_emails_from_any_platform_are_told_apart():
    from jigstream.extract import as_sale, normalize, sale_signal
    t = lambda sender, subject, body="": sale_signal(Email("<x>", sender, subject, "", body))  # noqa: E731
    assert t("eBay <ebay@ebay.com>", "You made the sale for Pokemon ETB") == "eBay"
    assert t("Vinted <no-reply@vinted.com>", "You sold an item!") == "Vinted"
    assert t("Mercari <no-reply@mercari.com>", "Congrats! Your item sold") == "Mercari"
    assert t("StockX <noreply@stockx.com>", "Your Ask Has Been Matched", "Ship your item by Oct 30") == "StockX"
    assert t("eBay <ebay@ebay.com>", "Your payout has been sent") == "eBay"
    assert t("eBay <ebay@ebay.com>", "Order confirmed: Pokemon ETB", "Sold by: cardshop") is None  # you bought it
    assert t("Mercari <no-reply@mercari.com>", "Your order has shipped") is None
    assert t("Target <orders@target.com>", "Your item sold out") is None  # not a selling platform
    email = Email("<v>", "Vinted <no-reply@vinted.com>", "You sold an item!", "2026-10-20T10:00:00", "")
    r = normalize(as_sale({"kind": "order", "retailer": "Vinted", "total": 35.0, "items": [{"name": "Hoodie"}],
                           "shipping_status": "ordered"}, "Vinted"), email)
    assert (r["kind"], r["platform"], r["item"], r["sale_price"], r["payout"]) == ("sale", "Vinted", "Hoodie", 35.0, None)


def test_orders_that_were_sales_move_to_sales(tmp_path):
    from jigstream import sales
    con = Store(db.connect(str(tmp_path / "m.db"))).db
    bought = _order(con, merge_key="t:1", order_date="2026-10-01", total=20.0, items=json.dumps([{"name": "Nike Tech Fleece Hoodie"}]),
                    message_ids=json.dumps(["<buy>"]))
    sold = _order(con, merge_key="v:1", retailer="Vinted", order_number=None, order_date="2026-10-20", total=55.0,
                  items=json.dumps([{"name": "Nike Tech Fleece Hoodie"}]), message_ids=json.dumps(["<v1>", "<v2>"]))
    real = _order(con, merge_key="e:1", retailer="eBay", order_number="9", total=10.0, message_ids=json.dumps(["<e1>"]))
    con.executemany("INSERT INTO emails (message_id, sender, subject, kind) VALUES (?,?,?, 'order')",
                    [("<buy>", "Target <o@target.com>", "Thanks for your order"),
                     ("<v1>", "Vinted <no-reply@vinted.com>", "You sold an item!"),
                     ("<v2>", "Vinted <no-reply@vinted.com>", "Your parcel was delivered"),
                     ("<e1>", "eBay <ebay@ebay.com>", "Order confirmed: Charizard")])
    con.commit()
    moved = sales.move_misread(con)
    assert [m[0]["platform"] for m in moved] == ["Vinted"] and len(moved[0][1]) == 2
    left = {r[0] for r in con.execute("SELECT id FROM tracked_orders")}
    assert left == {bought, real} and sold not in left
    [s] = [dict(r) for r in con.execute("SELECT * FROM sales")]
    assert (s["sale_price"], s["payout"], s["source"], s["order_id"], s["cost"]) == (55.0, 55.0, "moved", bought, 20.0)
    assert con.execute("SELECT kind FROM emails WHERE message_id = '<v1>'").fetchone()[0] == "sale"
    assert sales.move_misread(con) == []  # nothing left to move


def test_a_sold_email_the_model_calls_an_order_is_saved_as_a_sale():
    from jigstream.extract import extract_raw

    class Model:
        def chat_json(self, system, user, examples=None):
            return {"kind": "order", "retailer": "eBay", "order_number": "12-1", "total": 120.0,
                    "items": [{"name": "Charizard ex box"}], "shipping_status": "ordered"}
    email = Email("<s>", "eBay <ebay@ebay.com>", "You made the sale for Charizard ex box", "2026-10-20", "Sold for $120.00")
    raw, result = extract_raw(Model(), email)
    assert raw["kind"] == "order" and (result["kind"], result["platform"], result["sale_price"]) == ("sale", "eBay", 120.0)
    bought = Email("<b>", "eBay <ebay@ebay.com>", "Order confirmed: Charizard ex box", "2026-10-20", "Total $120.00")
    assert extract_raw(Model(), bought)[1]["kind"] == "order"
