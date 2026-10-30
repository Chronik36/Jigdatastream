import json

from jigstream import db, extract
from jigstream.mail import Email
from jigstream.orderstore import Store


def mk(subject, body, sender="Target <orders@oe1.target.com>"):
    return Email(message_id="<a>", account="me@gmail.com", sender=sender, subject=subject,
                 date="2026-10-29T12:00:00+00:00", body=body)


def status(model_says, email, **extra):
    data = {"kind": "order", "shipping_status": model_says, "order_number": "912003",
            "items": [{"name": "ETB", "quantity": 1}], **extra}
    return extract.normalize(data, email)["shipping_status"]


CONFIRM = mk("Thanks for your order #912003", "Thanks for your order. Pokemon ETB x1. Total $49.99. "
             "Estimated delivery: Fri, Oct 30. Delivery address: 1 Main St. We'll let you know when it's delivered.")


def test_a_confirmation_is_never_delivered_whatever_the_model_says():
    for claim in ("delivered", "out_for_delivery", "in_transit", "shipped"):
        assert status(claim, CONFIRM) == "ordered", claim


def test_real_notices_keep_their_stage():
    assert status("delivered", mk("Delivered: your Target order", "Your package was delivered to your front door.")) == "delivered"
    assert status("delivered", mk("Your package has been delivered", "Left at front door.")) == "delivered"
    assert status("out_for_delivery", mk("Out for delivery", "Your order is out for delivery today.")) == "out_for_delivery"
    assert status("in_transit", mk("Update", "Your package is in transit and on the way.")) == "in_transit"
    assert status("shipped", mk("Your order has shipped", "Your order has shipped via UPS 1Z999AA10123456784.")) == "shipped"


def test_a_shipped_email_claimed_as_delivered_is_only_shipped():
    mail = mk("Your order has shipped", "Your order has shipped. It will be delivered Friday. Tracking 1Z999AA10123456784")
    assert status("delivered", mail) == "shipped"


def test_a_delivered_to_label_or_header_is_not_a_delivery():
    mail = mk("Order confirmed", "Order #912003 confirmed.\nDelivered to: 1 Main St\nDelivered-To: me@gmail.com")
    assert status("delivered", mail) == "ordered"


def test_a_tracking_number_alone_keeps_shipped_but_not_delivered():
    mail = mk("Order confirmed", "Order confirmed. Tracking: 1Z999AA10123456784")
    assert status("shipped", mail, tracking_number="1Z999AA10123456784") == "shipped"
    assert status("delivered", mail, tracking_number="1Z999AA10123456784") == "shipped"


def test_cancel_and_other_statuses_are_untouched():
    mail = mk("Your order was canceled", "We canceled order #912003.")
    assert status("cancelled", mail) == "cancelled"
    assert status("unknown", mail) in ("unknown", "cancelled")


def _order(con, key, msgs, status_="delivered", **kw):
    f = dict(merge_key=key, retailer="Target", order_number=key, shipping_status=status_, message_ids=json.dumps(msgs),
             account="a", **kw)
    con.execute(f"INSERT INTO tracked_orders ({', '.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))


def test_repair_moves_back_only_unsupported_deliveries(tmp_path):
    con = db.connect(str(tmp_path / "r.db"))
    Store(con)  # adds the columns newer than the first release
    for mid, subj in (("m1", "Thanks for your order #1"), ("m2", "Delivered: your order"), ("m3", "Your order has shipped"),
                      ("m4", "Thanks for your order #4"), ("m5", "Thanks for your order #5")):
        con.execute("INSERT INTO emails (message_id, account, subject, date, kind) VALUES (?,?,?,?, 'order')",
                    (mid, "a", subj, "2026-10-29"))
    _order(con, "1", ["m1"])                                                   # wrong: confirmation only
    _order(con, "2", ["m1", "m2"])                                             # right: a delivery email
    _order(con, "3", ["m3"], tracking_number="1Z999AA10123456784")             # wrong, has tracking -> shipped
    _order(con, "4", ["m4"], status_manual=1)                                  # set by hand: leave
    _order(con, "5", ["m5"], carrier_status="Delivered, left at front door")   # carrier confirmed: leave
    _order(con, "6", [])                                                       # nothing to judge by: leave
    con.commit()
    assert Store(con).repair_delivered() == 2
    got = {r["order_number"]: r["shipping_status"] for r in con.execute("SELECT * FROM tracked_orders")}
    assert got == {"1": "ordered", "2": "delivered", "3": "shipped", "4": "delivered", "5": "delivered", "6": "delivered"}
