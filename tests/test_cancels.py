from jigstream import db
from jigstream.extract import cancel_signal, normalize
from jigstream.mail import Email
from jigstream.orderstore import Store


def mail(mid, subject, body, date, to="buyer1@x.com", sender="Target <orders@target.com>"):
    return Email(mid, sender, subject, date, body, account="me@gmail.com", to=to)


def test_cancel_signal_reads_the_email_not_the_model():
    assert cancel_signal(mail("a", "Your order has been canceled", "", "")) == "cancelled"
    assert cancel_signal(mail("a", "Update on your order", "Sorry, your order #9021 has been cancelled.", "")) == "cancelled"
    assert cancel_signal(mail("a", "Order cancellation confirmation", "", "")) == "cancelled"
    assert cancel_signal(mail("a", "Your refund has been issued", "", "")) == "refunded"
    assert cancel_signal(mail("a", "Thanks for your order", "Cancel anytime from your account.", "")) is None
    assert cancel_signal(mail("a", "One item in your order was cancelled", "", "")) is None
    assert cancel_signal(mail("a", "Your order shipped", "Want to cancel? Contact us.", "")) is None


def test_second_email_cancels_the_order_even_when_the_model_says_ordered(tmp_path):
    store = Store(db.connect(tmp_path / "t.db"))
    first = mail("<1>", "Thanks for your order #112-334", "Order #112-334 ...", "2026-10-20T10:00:00+00:00")
    store.save(first, normalize({"kind": "order", "retailer": "Target", "order_number": "#112-334",
                                 "shipping_status": "ordered"}, first))
    second = mail("<2>", "Your order was canceled", "We cancelled order 112334.", "2026-10-20T11:00:00+00:00",
                  sender="Target.com <no-reply@target.com>")
    store.save(second, normalize({"kind": "order", "retailer": "Target.com", "order_number": "112334",
                                  "shipping_status": "ordered"}, second))
    rows = store.db.execute("SELECT * FROM tracked_orders").fetchall()
    assert len(rows) == 1 and rows[0]["shipping_status"] == "cancelled"


def test_cancel_with_no_order_number_links_by_store_and_to_address(tmp_path):
    store = Store(db.connect(tmp_path / "t.db"))
    first = mail("<1>", "Order confirmed", "Order ABC12345", "2026-10-20T10:00:00+00:00")
    store.save(first, normalize({"kind": "order", "retailer": "Target", "order_number": "ABC12345",
                                 "shipping_status": "ordered"}, first))
    other = mail("<3>", "Order confirmed", "Order ZZZ99999", "2026-10-20T10:05:00+00:00", to="buyer2@x.com")
    store.save(other, normalize({"kind": "order", "retailer": "Target", "order_number": "ZZZ99999",
                                 "shipping_status": "ordered"}, other))
    cancel = mail("<2>", "Your order has been cancelled", "We're sorry.", "2026-10-21T09:00:00+00:00")
    store.save(cancel, normalize({"kind": "order", "retailer": "Target", "shipping_status": "unknown"}, cancel))
    by_to = {r["recipient_email"]: r["shipping_status"] for r in store.db.execute("SELECT * FROM tracked_orders")}
    assert by_to == {"buyer1@x.com": "cancelled", "buyer2@x.com": "ordered"}


def test_older_confirmation_read_after_the_cancel_doesnt_undo_it(tmp_path):
    store = Store(db.connect(tmp_path / "t.db"))
    cancel = mail("<2>", "Order #5551234 cancelled", "", "2026-10-21T09:00:00+00:00")
    store.save(cancel, normalize({"kind": "order", "retailer": "Target", "order_number": "5551234"}, cancel))
    first = mail("<1>", "Thanks for your order #5551234", "", "2026-10-20T09:00:00+00:00")
    store.save(first, normalize({"kind": "order", "retailer": "Target", "order_number": "5551234",
                                 "shipping_status": "ordered"}, first))
    assert store.db.execute("SELECT shipping_status FROM tracked_orders").fetchone()[0] == "cancelled"


def test_cancel_filed_as_other_still_cancels_the_order(tmp_path):
    store = Store(db.connect(tmp_path / "t.db"))
    first = mail("<1>", "Thanks for your order #5551234", "", "2026-10-20T09:00:00+00:00")
    store.save(first, normalize({"kind": "order", "retailer": "Target", "order_number": "5551234",
                                 "shipping_status": "ordered"}, first))
    cancel = mail("<2>", "Your order was cancelled", "Order #5551234 has been cancelled.", "2026-10-21T09:00:00+00:00")
    result = normalize({"kind": "other"}, cancel)
    assert result["kind"] == "order" and result["order_number"] == "5551234"
    store.save(cancel, result)
    assert store.db.execute("SELECT shipping_status FROM tracked_orders").fetchone()[0] == "cancelled"


def test_repair_folds_lone_cancel_orders_into_the_original(tmp_path):
    store = Store(db.connect(tmp_path / "t.db"))
    first = mail("<1>", "Thanks for your order", "", "2026-10-20T09:00:00+00:00")
    store.save(first, normalize({"kind": "order", "retailer": "Target", "order_number": "A1B2C3D4",
                                 "shipping_status": "ordered"}, first))
    # Saved the old way: its own row with a made-up key, status from the model.
    store.db.execute("INSERT INTO tracked_orders (merge_key, retailer, shipping_status, message_ids, recipient_email,"
                     " last_email_date) VALUES ('msg:<2>', 'Target.com', 'ordered', '[\"<2>\"]', 'buyer1@x.com',"
                     " '2026-10-21T09:00:00+00:00')")
    store.record_email(mail("<2>", "Your order has been canceled", "", "2026-10-21T09:00:00+00:00"), "order")
    assert store.repair_cancels() == (1, 1)
    rows = store.db.execute("SELECT * FROM tracked_orders").fetchall()
    assert len(rows) == 1 and rows[0]["shipping_status"] == "cancelled" and "<2>" in rows[0]["message_ids"]
