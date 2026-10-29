import json

from jigstream import bank, cancels, db, stuck
from jigstream.app import create_app
from jigstream.extract import normalize
from jigstream.mail import Email
from jigstream.orderstore import Store


def setup(tmp_path):
    c = db.connect(str(tmp_path / "c.db"))
    return c, Store(c)


def order(store, mid, number, date, total=63.98, to="buyer1@x.com", subject=None, status="ordered"):
    e = Email(mid, "Target <orders@target.com>", subject or f"Thanks for your order #{number}", date,
              f"Order #{number}\nTotal ${total}", account="me@gmail.com", to=to)
    store.save(e, normalize({"kind": "order", "retailer": "Target", "order_number": number, "total": total,
                             "shipping_status": status}, e))


def decline(c, number="", email="", ts="2026-10-28T14:00:00Z", reason="Policy - Item Demand"):
    c.execute("INSERT INTO checkouts (fingerprint, bot, status, reason, site, product, profile, order_number, email, ts)"
              " VALUES (?, 'Hayha', 'decline', ?, 'Target', 'Booster Bundle', 'Ana', ?, ?, ?)",
              (f"d{number}{email}{ts}", reason, number, email, ts))
    c.commit()


def tx(c, desc, amount, day, mask="4242"):
    acct = bank.account_for(c, "Card " + mask, mask=mask)
    bank.import_rows(c, acct, [{"date": day, "description": desc, "amount": amount, "category": ""}], "file")


def status(c, number):
    row = c.execute("SELECT shipping_status, cancel_why FROM tracked_orders WHERE order_number = ?", (number,)).fetchone()
    return row[0], json.loads(row[1] or "[]")


def test_cancel_email_a_minute_before_the_confirmation_still_cancels(tmp_path):
    # The cancel is really a minute later, but its forwarded Date is in another time zone, so as text it sorts first.
    c, store = setup(tmp_path)
    order(store, "<1>", "912000111", "2026-10-28T14:01:00+00:00")
    order(store, "<2>", "912000111", "2026-10-28T10:02:00-04:00", subject="Your order #912000111 was canceled")
    assert status(c, "912000111")[0] == "cancelled"


def test_repair_reads_every_email_not_just_the_latest(tmp_path):
    c, store = setup(tmp_path)
    order(store, "<1>", "912000222", "2026-10-28T14:01:00+00:00")
    store.db.execute("UPDATE tracked_orders SET message_ids = '[\"<1>\", \"<2>\"]'")
    store.record_email(Email("<2>", "Target", "Your order #912000222 was canceled", "2026-10-28T10:00:00-04:00",
                             "", account="me@gmail.com"), "order")
    store.repair_cancels()
    assert status(c, "912000222") == ("cancelled", ["cancel email"])


def test_bot_decline_with_the_order_number_cancels_it(tmp_path):
    c, store = setup(tmp_path)
    order(store, "<1>", "912000333", "2026-10-28T14:01:00+00:00")
    order(store, "<2>", "912000444", "2026-10-28T14:01:00+00:00")
    decline(c, number="912000333")
    assert cancels.reconcile(c) == 1
    assert status(c, "912000333") == ("cancelled", ["Hayha decline"])
    assert status(c, "912000444")[0] == "ordered"


def test_bot_decline_without_a_number_matches_by_account_email(tmp_path):
    c, store = setup(tmp_path)
    order(store, "<1>", "912000555", "2026-10-28T14:01:00+00:00", to="Buyer1@X.com")
    order(store, "<2>", "912000666", "2026-10-28T14:01:00+00:00", to="other@x.com")
    decline(c, email="buyer1@x.com", ts="2026-10-28T14:00:30Z")
    cancels.reconcile(c)
    assert status(c, "912000555")[0] == "cancelled" and status(c, "912000666")[0] == "ordered"


def test_card_refund_cancels_by_matched_charge_or_exact_total(tmp_path):
    c, store = setup(tmp_path)
    order(store, "<1>", "912000777", "2026-10-28T14:01:00+00:00", total=63.98)
    order(store, "<2>", "912000888", "2026-10-28T14:05:00+00:00", total=41.10)
    tx(c, "TARGET.COM * 800-591-3869", 63.98, "2026-10-28")
    bank.auto_match(c)  # the charge is matched to its order, still open: nothing given back yet
    assert status(c, "912000777")[0] == "ordered"
    tx(c, "TARGET.COM * 800-591-3869", -63.98, "2026-10-28")
    tx(c, "TARGET.COM", -41.10, "2026-10-29")  # no charge on the statement, but the exact total from Target
    bank.auto_match(c)
    assert status(c, "912000777") == ("cancelled", ["card refund"])
    assert status(c, "912000888") == ("cancelled", ["card refund"])


def test_a_refund_two_orders_could_be_cancels_neither(tmp_path):
    c, store = setup(tmp_path)
    order(store, "<1>", "912001111", "2026-10-28T14:01:00+00:00", total=63.98)
    order(store, "<2>", "912002222", "2026-10-28T14:02:00+00:00", total=63.98)
    tx(c, "TARGET.COM", -63.98, "2026-10-28")
    cancels.reconcile(c)
    assert status(c, "912001111")[0] == status(c, "912002222")[0] == "ordered"


def test_all_three_agree_and_the_sheet_says_why(tmp_path):
    c, store = setup(tmp_path)
    order(store, "<1>", "912003333", "2026-10-28T14:01:00+00:00", total=63.98)
    order(store, "<2>", "912003333", "2026-10-28T14:03:00+00:00", subject="Your order #912003333 was canceled")
    decline(c, number="912003333")
    tx(c, "TARGET.COM", 63.98, "2026-10-28")
    tx(c, "TARGET.COM", -63.98, "2026-10-28")
    bank.auto_match(c)
    assert status(c, "912003333") == ("cancelled", ["cancel email", "Hayha decline", "card refund"])
    oid = c.execute("SELECT id FROM tracked_orders").fetchone()[0]
    page = create_app(str(tmp_path / "c.db")).test_client().get(f"/orders/{oid}/sheet").get_data(as_text=True)
    assert "cancel email + Hayha decline + card refund" in page


def test_shipped_and_hand_set_orders_are_left_alone(tmp_path):
    c, store = setup(tmp_path)
    order(store, "<1>", "912004444", "2026-10-28T14:01:00+00:00", total=20.00, status="shipped")
    order(store, "<2>", "912005555", "2026-10-28T14:01:00+00:00", total=30.00)
    c.execute("UPDATE tracked_orders SET status_manual = 1 WHERE order_number = '912005555'")
    tx(c, "TARGET.COM", -20.00, "2026-10-29")
    decline(c, number="912005555")
    cancels.reconcile(c)
    assert status(c, "912004444")[0] == "shipped" and status(c, "912005555")[0] == "ordered"


def test_decline_with_a_success_checkouts_number_cancels_it_on_drops(tmp_path):
    c, store = setup(tmp_path)
    c.execute("INSERT INTO checkouts (fingerprint, bot, status, site, product, profile, order_number, ts)"
              " VALUES ('s', 'Hayha', 'success', 'Target', 'Booster Bundle', 'Ana', '912006666', '2026-10-28T13:59:00Z')")
    decline(c, number="912006666")
    rows = {r["fingerprint"]: r for r in stuck.cross_reference(c)}
    assert rows["s"]["state"] == "cancelled"
