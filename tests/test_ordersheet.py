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
    assert 'data-sheet="%d"' % oid in c.get("/orders").get_data(as_text=True)


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
