import json

from jigstream import db, pricecheck
from jigstream.app import create_app
from jigstream.config import Config
from jigstream.extract import normalize
from jigstream.mail import Email, html_to_text
from jigstream.orderstore import Store

AMAZON = ('<table><tr><td>Order Total:</td><td><span class="a-price-symbol">$</span><span class="a-price-whole">45</span>'
          '<span class="a-price-fraction">00</span></td></tr></table>')


def mail(mid, subject, html, account="me@gmail.com"):
    return Email(mid, '"Amazon.com" <auto-confirm@amazon.com>', subject, "2026-09-30T10:00:00+00:00", html_to_text(html), account)


def test_small_cents_keep_their_decimal_point():
    assert "$45.00" in html_to_text(AMAZON)
    assert "$1,114.00" in html_to_text("<p>Total $1,114<sup>00</sup></p>")
    assert html_to_text("<p>the 2<sup>nd</sup> box, note<sup>12</sup></p>") == "the 2nd box, note12"


def test_a_total_the_email_never_shows_is_read_again():
    e = mail("<a>", "Ordered: Pokemon", "<p>Item $45.00</p><p>Shipping $0.00</p><p>Order Total: $45.00</p>")
    assert normalize({"kind": "order", "retailer": "Amazon", "total": 4500, "items": [{"name": "ETB", "price": "4500"}]}, e)["total"] == 45.0
    assert normalize({"kind": "order", "retailer": "Amazon", "total": "45,00"}, e)["total"] == 45.0  # European comma
    r = normalize({"kind": "order", "retailer": "Amazon", "total": 999, "items": [{"name": "ETB", "price": 4500}]}, e)
    assert r["total"] == 45.0 and r["items"][0]["price"] == 45.0
    shipped = mail("<b>", "Shipped", "<p>Your package is on the way</p>")
    assert normalize({"kind": "order", "retailer": "Amazon", "total": 4500}, shipped)["total"] == 4500  # nothing to check against


def _store(tmp_path):
    store = Store(db.connect(str(tmp_path / "p.db")))
    pricecheck.ensure(store.db)
    return store


def _order(store, mid, total, manual=0):
    store.db.execute("INSERT INTO tracked_orders (merge_key, retailer, order_number, total, items, message_ids, total_manual)"
                     " VALUES (?,?,?,?,?,?,?)", (f"amazon:{mid}", "Amazon", mid, total, json.dumps([{"name": "ETB", "price": total}]),
                                                json.dumps([mid]), manual))
    store.db.execute("INSERT INTO emails (message_id, account) VALUES (?, 'me@gmail.com')", (mid,))
    store.db.commit()


def test_saved_prices_are_fixed_once_from_their_emails(tmp_path):
    store = _store(tmp_path)
    _order(store, "o1", 4500)                     # cents ran into the dollars
    _order(store, "o2", 45)                       # already right
    _order(store, "o3", 1114, manual=1)           # typed in by hand: left alone
    store.db.execute("INSERT INTO expenses (message_id, vendor, amount) VALUES ('e1', 'Whop', 5000)")
    store.db.execute("INSERT INTO emails (message_id, account) VALUES ('e1', 'me@gmail.com')")
    store.db.commit()
    bodies = {"o1": AMAZON, "o2": AMAZON, "o3": "<p>Total $11.14</p>", "e1": "<p>Receipt: you paid $50.00</p>"}
    opened = []

    def fetch(config, address, password, ids):
        opened.extend(ids)
        for m in ids:
            yield m, mail(m, "Your order", bodies[m])
    config = Config.from_env()
    config.accounts = [("me@gmail.com", "pw")]
    assert pricecheck.recheck(config, store, fetch) == 2
    totals = {r["order_number"]: (r["total"], json.loads(r["items"])[0]["price"]) for r in store.db.execute("SELECT * FROM tracked_orders")}
    assert totals == {"o1": (45.0, 45.0), "o2": (45, 45), "o3": (1114, 1114)}
    assert store.db.execute("SELECT amount FROM expenses").fetchone()[0] == 50.0
    assert [r["how"] for r in store.db.execute("SELECT how FROM price_fixes ORDER BY id")] == ["decimal", "decimal"]
    opened.clear()
    assert pricecheck.recheck(config, store, fetch) == 0 and opened == []  # done once


class LLM:
    def chat_json(self, system, user, examples=()):
        return {"kind": "order", "retailer": "Amazon", "order_number": "o1", "total": 4500}


def test_a_hand_fixed_price_teaches_the_ai(tmp_path):
    store = _store(tmp_path)
    _order(store, "o1", 4500)
    assert pricecheck.set_price(store, "order", 1, 45.0) == 4500
    config = Config.from_env()
    config.accounts = [("me@gmail.com", "pw")]
    msg = pricecheck.learn(config, store, LLM(), "order", 1, 45.0,
                           fetch=lambda c, a, p, ids: ((m, mail(m, "Ordered: Pokemon", AMAZON)) for m in ids))
    assert "re-read 1 email" in msg and "4500" in msg
    ex = store.examples()[0]
    assert ex["status"] == "price" and json.loads(ex["corrected"])["total"] == 45.0 and ex["model_was_right"] == 0
    assert store.db.execute("SELECT total, total_manual FROM tracked_orders").fetchone()[:] == (45.0, 1)


def test_price_form_and_copy_on_go_through(tmp_path, monkeypatch):
    path = str(tmp_path / "p.db")
    app = create_app(path)
    store = Store(db.connect(path))
    _order(store, "114-0548992-7369002", 4500)
    monkeypatch.setattr(pricecheck, "_learn_later", lambda *a: None)
    c = app.test_client()
    html = c.get("/review?src=order&name=1&at=o1").get_data(as_text=True)
    assert "Wrong price?" in html and 'action="/prices/order/1"' in html and "data-copy" in html
    c.post("/prices/order/1", data={"price": "$45.00"})
    assert store.db.execute("SELECT total, total_manual FROM tracked_orders").fetchone()[:] == (45.0, 1)
    c.post("/prices/order/1", data={"price": "abc"})
    assert store.db.execute("SELECT total FROM tracked_orders").fetchone()[0] == 45.0
