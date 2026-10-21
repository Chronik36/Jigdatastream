import json

from jigstream import analysis, db, emails, parsers, profiles
from jigstream.app import create_app

HOOK_OK = {"username": "Valor AIO", "embeds": [{"title": "Successful Checkout", "timestamp": "2026-01-01T00:00:00Z",
    "fields": [{"name": "Product", "value": "Dunk Low"}, {"name": "Site", "value": "footlocker.com"},
               {"name": "Profile", "value": "||prof1||"}, {"name": "Order #", "value": "ABC12345"}]}]}
HOOK_BAD = {"username": "Stellar", "embeds": [{"title": "Card Declined", "timestamp": "2026-01-02T00:00:00Z",
    "description": "AVS mismatch", "fields": [{"name": "Product", "value": "Dunk Low"},
    {"name": "Site", "value": "footlocker.com"}, {"name": "Profile", "value": "prof2"}]}]}
PROFILES = [{"name": "prof1", "shipping": {"address1": "1 Main Street", "city": "Austin", "state": "TX", "zip": "78701"}},
            {"name": "prof2", "shipping": {"address1": "22 Oak Avenue Apt 4", "city": "Austin", "state": "Texas", "zip": "7870"},
             "billing": {"address1": "23 Oak Ave", "city": "Austin", "state": "TX", "zip": "78701"}}]


def test_pipeline(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    assert parsers.ingest(c, json.dumps([HOOK_OK, HOOK_BAD])) == 2
    assert parsers.ingest(c, json.dumps([HOOK_OK])) == 0  # dedupe
    assert profiles.ingest(c, json.dumps(PROFILES), "Valor") == 2
    rows = {r["profile"]: r for r in c.execute("SELECT * FROM checkouts")}
    assert rows["prof1"]["status"] == "success" and rows["prof1"]["bot"] == "Valor"
    assert rows["prof2"]["status"] == "decline"
    report, unmatched = analysis.failure_report(c)
    assert report[0]["profile"] == "prof2" and report[0]["suggested"]["address2"] == "APT 4"
    assert any("ZIP" in i for i in report[0]["issues"])


def test_profiles_csv():
    csv_text = "Profile Name,First Name,Address 1,City,State,Zip\nA,Bob,5 Elm St,Reno,NV,89501\n"
    assert profiles.parse_profiles(csv_text)[0]["ship_zip"] == "89501"


def test_email_classify():
    assert emails.classify_kind("Your order has been cancelled") == "cancelled"
    assert emails.retailer_for("Target <orders@oe1.target.com>") == "Target"
    assert emails.retailer_for("Some Store <store+123@shopifyemail.com>") == "Shopify"


def test_web(tmp_path):
    app = create_app(str(tmp_path / "w.db"))
    assert app.test_client().get("/").status_code == 200
    for p in ("/checkouts", "/orders", "/profiles"):
        assert app.test_client().get(p).status_code == 200


def test_card_data_not_stored(tmp_path):
    c = db.connect(str(tmp_path / "c.db"))
    prof = {"name": "p", "shipping": {"address1": "1 A St", "city": "X", "state": "TX", "zip": "78701"},
            "card": {"number": "4111111111111111", "cvv": "123", "exp": "12/30"}}
    profiles.ingest(c, json.dumps([prof]), "Valor")
    row = c.execute("SELECT * FROM profiles").fetchone()
    assert row["card_last4"] == "1111"
    assert "4111111111111111" not in "".join(str(v) for v in tuple(row)) and '"123"' not in row["raw"]


def test_aycd_style(tmp_path):
    c = db.connect(str(tmp_path / "a.db"))
    data = [{"name": "Group1 #1", "shipping": {"name": "Bob Smith", "email": "b@x.com", "phone": "5125550100",
             "line1": "10 Pine Street", "line2": "Apt 2", "postCode": "78701", "city": "Austin", "state": "TX", "country": "US"},
             "billing": {"name": "Bob Smith", "line1": "10 Pine Street", "postCode": "78701", "city": "Austin", "state": "TX"},
             "payment": {"cardNumber": "4111 1111 1111 9999", "cardCvv": "321", "cardMonth": "01", "cardYear": "30"}}]
    assert profiles.ingest(c, json.dumps(data), "Valor") == 1
    r = c.execute("SELECT * FROM profiles").fetchone()
    assert (r["ship_address1"], r["ship_zip"], r["card_last4"]) == ("10 Pine Street", "78701", "9999")
    assert "4111" not in r["raw"] and "321" not in r["raw"]


def test_tracker(tmp_path):
    from jigstream import tracker
    c = db.connect(str(tmp_path / "t2.db"))
    hooks = [{"username": "Valor", "embeds": [{"title": "Successful Checkout", "timestamp": "2026-02-01T10:00:00Z",
              "fields": [{"name": "Product", "value": "Pokemon Prismatic ETB"}, {"name": "Site", "value": "pokemoncenter.com"},
                         {"name": "Profile", "value": "p1"}]}]},
             {"username": "Valor", "embeds": [{"title": "Successful Checkout", "timestamp": "2026-02-01T11:00:00Z",
              "fields": [{"name": "Product", "value": "Pack"}, {"name": "Site", "value": "target.com"},
                         {"name": "Order #", "value": "912003"}, {"name": "Profile", "value": "p2"}]}]}]
    parsers.ingest(c, json.dumps(hooks))
    emails.store(c, {"message_id": "1", "retailer": "Pokemon Center", "kind": "confirmed", "order_number": "",
                     "product": "", "total": "", "ts": "2026-02-01T10:20:00+00:00", "subject": "Thanks for your order",
                     "sender": "orders@pokemoncenter.com", "store": "Pokemon Center", "body": "Prismatic ETB x1"})
    emails.store(c, {"message_id": "2", "retailer": "Target", "kind": "cancelled", "order_number": "912003",
                     "product": "", "total": "", "ts": "2026-02-02T10:00:00+00:00", "subject": "Order cancelled",
                     "sender": "orders@target.com", "store": "Target", "body": ""})
    c.commit()
    st = {r["checkout"]["site"]: (r["status"], r["matched_by"]) for r in tracker.link(c)}
    assert st["pokemoncenter.com"] == ("confirmed", "retailer+product+time")
    assert st["target.com"] == ("cancelled", "order #")
    assert tracker.retailer_stats(c)["target.com"]["cancel_rate"] == 100


def test_order_number_regex():
    from jigstream.emails import ORDER_RE
    m = ORDER_RE.search("Your order #112-1234567-7654321 has shipped")
    assert (m.group(1) or m.group(2)) == "112-1234567-7654321"
    assert ORDER_RE.search("Your order has been confirmed") is None
    m = ORDER_RE.search("Order #1001 confirmed")
    assert (m.group(1) or m.group(2)) == "1001"
