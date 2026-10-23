import json

from jigstream import analysis, db, parsers, profiles
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
    assert report[0]["profile"] == "prof2" and report[0]["suggested"]["address1"] == "22 Oak Avenue Apt 4"
    assert report[0]["suggested"]["state"] == "Texas"  # full state names are fine for bots
    assert any("ZIP" in i for i in report[0]["issues"])
    assert not any("unit" in i or "state" in i for i in report[0]["issues"])


def test_profiles_csv():
    csv_text = "Profile Name,First Name,Address 1,City,State,Zip\nA,Bob,5 Elm St,Reno,NV,89501\n"
    assert profiles.parse_profiles(csv_text)[0]["ship_zip"] == "89501"


def test_web(tmp_path):
    app = create_app(str(tmp_path / "w.db"))
    assert app.test_client().get("/").status_code == 200
    for p in ("/checkouts", "/orders", "/expenses", "/profiles", "/api/orders"):
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


def test_expense_total_read_from_email_when_model_misses_it():
    from jigstream.extract import find_total, normalize
    from jigstream.mail import Email
    body = "Alpine Proxies\nISP Subnet /24 | $40.00\nSubtotal | $40.00\nTax | $3.20\nTotal | $43.20\nPaid with Visa 4242"
    assert find_total(body) == 43.2
    assert find_total("Amount paid\n$15.00 USD") == 15.0
    assert find_total("Your plan: Everest\nPrice: US$99.00") == 99.0
    assert find_total("Thanks for joining!") is None
    mail = Email("<a@x>", "Alpine <a@x.com>", "Your receipt", "2026-10-18T00:00:00+00:00", body, "me@gmail.com")
    assert normalize({"kind": "botting_expense", "vendor": "Alpine Proxies"}, mail)["amount"] == 43.2
    reminder = Email("<r@x>", "Stella <s@x.com>", "Invoice Payment Reminder", "2026-09-26", "Total $70.00", "me@gmail.com")
    assert normalize({"kind": "botting_expense", "vendor": "Stella"}, reminder) == {"kind": "other"}


def test_pages_and_writes_work_while_another_thread_is_writing(tmp_path):
    """Build 59 logs: Setup crashed with "database is locked" while carrier tracking held a write open."""
    import sqlite3
    from jigstream.app import create_app
    path = str(tmp_path / "busy.db")
    client = create_app(path).test_client()
    client.get("/orders")
    writer = sqlite3.connect(path)
    writer.execute("BEGIN IMMEDIATE")  # another thread mid-write, not committed
    writer.execute("UPDATE tracked_orders SET carrier_status = 'x'")
    try:
        assert client.get("/setup").status_code == 200  # reading never waits on a write (WAL)
        assert client.get("/orders").status_code == 200
    finally:
        writer.rollback()
    con = db.connect(path)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.isolation_level is None  # each write saved at once; no lock held across slow work


def test_model_http_500_is_a_failed_email_not_an_engine_that_is_off(monkeypatch):
    import io
    import urllib.error
    from jigstream import llm
    calls = []

    def fail(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {},
                                     io.BytesIO(b'{"error": "model runner has unexpectedly stopped"}'))
    monkeypatch.setattr(llm.urllib.request, "urlopen", fail)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    try:
        llm.LLMClient("http://127.0.0.1:1/v1", "m").chat_json("s", "u")
    except llm.LLMError as e:
        assert "runner has unexpectedly stopped" in str(e) and "Could not reach" not in str(e)
    assert len(calls) == 2  # one retry


def test_profiles_import_under_a_retail_site_and_group_and_clear(tmp_path):
    import io
    path = str(tmp_path / "t.db")
    c = db.connect(path)
    c.execute("INSERT INTO cards (id, last4) VALUES (7, '4242')") if c.execute(
        "SELECT name FROM sqlite_master WHERE name='cards'").fetchone() else None
    profiles.ingest(c, json.dumps(PROFILES), "Valor", "Target", "Target Main")
    c.execute("UPDATE profiles SET card_id = 7 WHERE name = 'prof1'")
    profiles.ingest(c, json.dumps(PROFILES[:1]), "Valor")  # importing again keeps the site, group and card
    p = dict(c.execute("SELECT site, group_name, card_id FROM profiles WHERE name = 'prof1'").fetchone())
    assert p == {"site": "Target", "group_name": "Target Main", "card_id": 7}

    client = create_app(path).test_client()
    data = {"site": "other", "site_other": "Riot Store", "group": "Riot 1", "bot": "Valor",
            "files": (io.BytesIO(json.dumps([{"name": "prof3", "shipping": {"address1": "5 Elm St", "zip": "78701"}}]).encode()), "p.json")}
    client.post("/upload/profiles", data=data, content_type="multipart/form-data")
    assert tuple(c.execute("SELECT site, group_name FROM profiles WHERE name = 'prof3'").fetchone()) == ("Riot Store", "Riot 1")
    page = client.get("/profiles?site=Target").get_data(as_text=True)
    assert "RETAIL SITES" in page and "Target Main" in page and "prof1" in page and "prof3" not in page.split('class="pgrid"')[1]
    assert "Are you sure you want to clear all profiles?" in page
    client.post("/profiles/clear", data={"confirm": "2"})  # the count on the page was out of date
    assert c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 3
    client.post("/profiles/clear", data={"confirm": "3"})
    assert c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0
    assert 'name="site"' in client.get("/imports").get_data(as_text=True)
