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
    assert emails.retailer_for("Foot Locker <orders@footlocker.com>") == "Footlocker"


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
