import json

from jigstream import db, market, sales
from jigstream.app import create_app
from jigstream.orderstore import Store

ETB = "Pokemon Prismatic Evolutions Elite Trainer Box"


def _order(con, key="o1", qty=4, total=200.0, **kw):
    f = dict(merge_key=key, retailer="Target", order_number=key, order_date="2026-10-20", total=total,
             items=json.dumps([{"name": ETB, "quantity": qty, "price": round(total / qty, 2)}]),
             shipping_status="delivered", message_ids="[]", account="me@gmail.com")
    f.update(kw)
    cur = con.execute(f"INSERT INTO tracked_orders ({', '.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    con.commit()
    return cur.lastrowid


def _client(tmp_path):
    p = str(tmp_path / "s.db")
    con = db.connect(p)
    Store(con)
    sales.ensure(con)
    market.ensure(con)
    return p, con, create_app(p).test_client()


def test_selling_part_of_an_order_records_each_unit_and_keeps_the_rest_held(tmp_path):
    p, con, cl = _client(tmp_path)
    oid = _order(con)
    assert sum(h["qty"] for h in market.held(con)) == 4
    r = cl.post(f"/orders/{oid}/sold", data={"place": "StockX", "price": "80", "fees": "8", "qty": "2", "back": "1"})
    assert r.status_code == 302
    rows = con.execute("SELECT * FROM sales").fetchall()
    assert len(rows) == 2 and {x["platform"] for x in rows} == {"StockX"}
    assert all(x["sale_price"] == 80 and x["payout"] == 72 and x["order_id"] == oid for x in rows)
    held = market.held(con)
    assert sum(h["qty"] for h in held) == 2          # two left, not none


def test_selling_everything_clears_it_from_holdings(tmp_path):
    p, con, cl = _client(tmp_path)
    oid = _order(con, qty=2, total=100.0)
    cl.post(f"/orders/{oid}/sold", data={"place": "eBay", "price": "70", "qty": "2"})
    assert market.held(con) == []


def test_what_it_sold_for_becomes_a_market_source(tmp_path):
    p, con, cl = _client(tmp_path)
    oid = _order(con)
    cl.post(f"/orders/{oid}/sold", data={"place": "eBay", "price": "80", "qty": "1"})
    cl.post(f"/orders/{oid}/sold", data={"place": "A friend", "price": "100", "qty": "1"})
    m = market.get(con, market.key(ETB))
    assert m["prices"]["yours"]["price"] == 90 and m["prices"]["yours"]["count"] == 2    # the middle of 80 and 100
    assert market.market_of(m) == 90
    assert not m.get("market_manual")


def test_tradepost_stays_its_own_line_and_is_not_counted_twice(tmp_path):
    p, con, cl = _client(tmp_path)
    oid = _order(con)
    cl.post(f"/orders/{oid}/sold", data={"place": "Tradepost", "price": "70", "qty": "1"})
    m = market.get(con, market.key(ETB))
    assert "yours" not in m["prices"] and "yours" in m["errors"]
    assert market.tradepost(con, ETB, market.cleanup(ETB))["price"] == 70


def test_set_as_market_price_overrides_the_median(tmp_path):
    p, con, cl = _client(tmp_path)
    oid = _order(con)
    market.ensure_rows(con, [{"key": market.key(ETB), "raw": ETB, "store": "Target"}])
    m = market.get(con, market.key(ETB))
    m["prices"]["ebay"] = {"price": 150, "low": 150, "high": 150, "count": 1, "how": "x", "link": "", "manual": True}
    market.save(con, m)
    cl.post(f"/orders/{oid}/sold", data={"place": "eBay", "price": "95", "qty": "1", "set_market": "1"})
    m = market.get(con, market.key(ETB))
    assert m["market_manual"] == 95 and market.market_of(m) == 95


def test_bad_input_changes_nothing(tmp_path):
    p, con, cl = _client(tmp_path)
    oid = _order(con)
    cl.post(f"/orders/{oid}/sold", data={"place": "", "price": "80"})
    cl.post(f"/orders/{oid}/sold", data={"place": "eBay", "price": "abc"})
    cl.post(f"/orders/{oid}/sold", data={"place": "eBay", "price": "0"})
    assert con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0
    assert cl.post("/orders/9999/sold", data={"place": "eBay", "price": "5"}).status_code == 302


def test_the_sheet_offers_the_sold_form(tmp_path):
    p, con, cl = _client(tmp_path)
    oid = _order(con)
    page = cl.get(f"/orders/{oid}/sheet").data.decode()
    assert 'id="os-sold"' in page and "Sold to / where" in page and "market price" in page


def test_older_multi_item_sales_still_hide_the_order(tmp_path):
    p, con, cl = _client(tmp_path)
    two = json.dumps([{"name": "Item A", "quantity": 1, "price": 10}, {"name": "Item B", "quantity": 1, "price": 10}])
    oid = _order(con, key="m1", items=two, total=20.0)
    sales.ensure(con)
    sales.add(con, {"platform": "eBay", "item": "Some other name", "sale_price": 30, "order_id": oid})
    assert market.held(con) == []
