import json
from datetime import date

from jigstream import db, tracking
from jigstream.app import create_app


def _o(con, key, **kw):
    f = dict(merge_key=key, retailer="Target", order_number=key, order_date="2026-10-25",
             items=json.dumps([{"name": "Prismatic ETB", "quantity": 1}]), carrier="UPS", shipping_status="in_transit",
             message_ids="[]", account="me@gmail.com")
    f.update(kw)
    con.execute(f"INSERT INTO tracked_orders ({', '.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))


def _seed(path):
    con = db.connect(path)
    _o(con, "t1", estimated_delivery="2026-10-30", shipping_status="out_for_delivery")
    _o(con, "p1", retailer="Pokemon Center", estimated_delivery="2026-10-25")                      # overdue
    _o(con, "w1", retailer="Walmart", shipping_status="delivered", estimated_delivery="2026-10-28", last_email_date="2026-10-29T12:00:00")
    _o(con, "b1", retailer="Best Buy", shipping_status="ordered")                                   # no day yet
    _o(con, "c1", retailer="Amazon", shipping_status="cancelled", estimated_delivery="2026-10-30")  # left off
    con.commit()
    con.close()


def test_calendar_rows(tmp_path):
    p = str(tmp_path / "c.db")
    _seed(p)
    from jigstream.orderstore import Store
    dated, undated = tracking.calendar_rows(Store(db.connect(p)).orders(), today=date(2026, 10, 30))
    by = {r["retailer"]: r for r in dated}
    assert by["Target"]["state"] == "out" and by["Target"]["date"] == "2026-10-30"
    assert by["Pokemon Center"]["state"] == "late"
    assert by["Walmart"]["state"] == "delivered" and by["Walmart"]["date"] == "2026-10-29"   # the day it arrived
    assert "Amazon" not in by and [r["retailer"] for r in undated] == ["Best Buy"]


def test_calendar_pages(tmp_path):
    p = str(tmp_path / "c2.db")
    _seed(p)
    app = create_app(p)
    cl = app.test_client()
    assert cl.get("/tracking/calendar").status_code == 200
    data = cl.get("/tracking/calendar/data").get_json()
    assert len(data["rows"]) == 3 and len(data["undated"]) == 1 and data["summary"]["late"] >= 1
    assert b"/tracking/calendar" in cl.get("/tracking").data
