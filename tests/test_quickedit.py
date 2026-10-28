import json

from jigstream import db
from jigstream.app import create_app


def test_prices_and_statuses_are_editable_everywhere(tmp_path, monkeypatch):
    monkeypatch.setattr("jigstream.pricecheck._learn_later", lambda *a: None)
    app = create_app(str(tmp_path / "q.db"))
    con = db.connect(str(tmp_path / "q.db"))
    oid = con.execute("INSERT INTO tracked_orders (merge_key, retailer, order_number, items, total, shipping_status, message_ids)"
                      " VALUES ('t:1', 'Target', '1', ?, 45.0, 'ordered', '[]')", (json.dumps([{"name": "ETB"}]),)).lastrowid
    con.commit()
    c = app.test_client()
    for page in ("/orders?day=all", "/tracking?day=all", f"/orders/{oid}/sheet"):
        html = c.get(page).get_data(as_text=True)
        assert f'data-edit-price="{oid}"' in html and f'data-edit-status="{oid}"' in html, page
    assert 'id="qe"' in c.get("/orders?day=all").get_data(as_text=True)
    c.post(f"/prices/order/{oid}", data={"price": "54.99", "back": "/tracking"})
    assert tuple(con.execute("SELECT total, total_manual FROM tracked_orders WHERE id=?", (oid,)).fetchone()) == (54.99, 1)
