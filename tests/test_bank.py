import io
import json

import pytest

from jigstream import bank, db, plaid, wallet
from jigstream.app import create_app

CHASE = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
10/03/2026,10/04/2026,NIKE.COM 800-806-6453 OR,Shopping,Sale,-130.00,
10/05/2026,10/06/2026,IPROYAL PROXIES,Shopping,Sale,-45.00,
10/05/2026,10/06/2026,IPROYAL PROXIES,Shopping,Sale,-45.00,
10/07/2026,10/07/2026,TRADER JOES #123,Groceries,Sale,-62.18,
10/09/2026,10/09/2026,Payment Thank You-Mobile,,Payment,500.00,
"""
AMEX = """Account: Platinum Card
Card Member: JACOB

Date,Description,Amount
10/04/2026,FOOTLOCKER.COM,210.50
10/06/2026,AMAZON MKTPLACE PMTS,15.00
"""
BANK = """Date,Description,Debit,Credit
2026-10-08,WHOP.COM CYBER,50.00,
2026-10-09,ZELLE FROM FRIEND,,20.00
"""
OFX = """OFXHEADER:100
DATA:OFXSGML
<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Capital One</FI></SONRS></SIGNONMSGSRSV1>
<CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CCACCTFROM><ACCTID>4111222233334444</CCACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261011120000[-5:EST]<TRNAMT>-99.99<FITID>A1<NAME>STRIPE*CYBERSOLE<MEMO>License</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261012<TRNAMT>25.00<FITID>A2<NAME>REFUND KITH</STMTTRN>
</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>
"""


@pytest.fixture(autouse=True)
def key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setattr(wallet, "_key", None)


def seed(c):
    c.execute("INSERT INTO tracked_orders (merge_key, retailer, order_number, order_date, total, shipping_status)"
              " VALUES ('nike:1','Nike','C001','2026-10-02',130.0,'shipped')")
    c.execute("INSERT INTO tracked_orders (merge_key, retailer, order_number, order_date, total)"
              " VALUES ('footlocker:9','Foot Locker','FL9','2026-10-03',210.5)")
    c.execute("INSERT INTO expenses (message_id, vendor, category, amount, date) VALUES ('m1','IPRoyal','proxy',45,'2026-10-05')")
    c.execute("INSERT INTO expenses (message_id, vendor, category, amount, date) VALUES ('m2','IPRoyal','proxy',45,'2026-10-05')")
    c.execute("INSERT INTO expenses (message_id, vendor, category, amount, date) VALUES ('m3','Cybersole','bot',99.99,'2026-10-10')")
    c.commit()


def test_reads_common_statement_formats():
    _, rows = bank.parse_csv(CHASE)
    assert [r["amount"] for r in rows] == [130.0, 45.0, 45.0, 62.18, -500.0]
    _, rows = bank.parse_csv(AMEX)  # a few lines above the header, charges positive
    assert [(r["date"], r["amount"]) for r in rows] == [("2026-10-04", 210.5), ("2026-10-06", 15.0)]
    _, rows = bank.parse_csv(BANK)
    assert [r["amount"] for r in rows] == [50.0, -20.0]
    assert [r["amount"] for r in bank.parse_csv(AMEX, charges="negative")[1]] == [-210.5, -15.0]
    name, rows = bank.parse_ofx(OFX)
    assert name == "Capital One …4444"
    assert [(r["date"], r["amount"], r["fitid"]) for r in rows] == [("2026-10-11", 99.99, "A1"), ("2026-10-12", -25.0, "A2")]
    assert rows[0]["description"] == "STRIPE*CYBERSOLE License"
    with pytest.raises(bank.ImportProblem):
        bank.parse_csv("hello,world\n1,2")


def test_import_dedupes_and_matches(tmp_path):
    c = db.connect(str(tmp_path / "b.db"))
    seed(c)
    name, added, skipped, matched = bank.import_file(c, "Chase.csv", CHASE.encode(), "Chase Freedom")
    assert (name, added, skipped) == ("Chase Freedom", 5, 0)
    assert bank.import_file(c, "Chase.csv", CHASE.encode(), "Chase Freedom")[1:3] == (0, 5)
    bank.import_file(c, "amex.csv", AMEX.encode())
    bank.import_file(c, "c1.ofx", OFX.encode())
    m = {t["description"]: (t["match_kind"], t["order_number"] or t["expense_vendor"])
         for t in bank.transactions(c, "matched")}
    assert m["NIKE.COM 800-806-6453 OR"] == ("order", "C001")
    assert m["FOOTLOCKER.COM"] == ("order", "FL9")
    assert m["STRIPE*CYBERSOLE License"] == ("expense", "Cybersole")
    # Two identical IPRoyal charges claim the two identical receipts, one each.
    ids = sorted(r[0] for r in c.execute("SELECT match_id FROM bank_transactions WHERE description='IPROYAL PROXIES'"))
    assert len(set(ids)) == 2
    s = bank.stats(c)
    assert (s["orders"], s["expenses"]) == (2, 3)
    assert {t["description"] for t in bank.transactions(c, "unmatched")} == {"TRADER JOES #123", "AMAZON MKTPLACE PMTS"}


def test_ambiguous_charge_is_left_for_you(tmp_path):
    c = db.connect(str(tmp_path / "b.db"))
    for n in ("A", "B"):
        c.execute("INSERT INTO tracked_orders (merge_key, retailer, order_number, order_date, total)"
                  f" VALUES ('{n}','Shop {n}','{n}','2026-10-02',99.0)")
    c.commit()
    bank.import_file(c, "x.csv", b"Date,Description,Amount\n10/03/2026,SQ *SOMETHING,-99.00\n")
    assert bank.stats(c)["unmatched"] == 1
    tx = bank.transactions(c)[0]
    assert len(bank.candidates(c, tx)) == 2


def test_manual_choices_stick(tmp_path):
    c = db.connect(str(tmp_path / "b.db"))
    seed(c)
    bank.import_file(c, "Chase.csv", CHASE.encode(), "Chase")
    nike = next(t for t in bank.transactions(c) if t["description"].startswith("NIKE"))
    groceries = next(t for t in bank.transactions(c) if t["description"].startswith("TRADER"))
    bank.set_match(c, nike["id"], None)
    assert bank.auto_match(c) == 0  # an unmatch you made isn't undone
    bank.set_match(c, groceries["id"], None, how="ignored")
    assert bank.stats(c)["unmatched"] == 1  # Nike, which you unmatched
    bank.reset_match(c, nike["id"])
    assert next(t for t in bank.transactions(c) if t["id"] == nike["id"])["match_kind"] == "order"
    # Matching a charge to an order another charge had moves it.
    bank.set_match(c, groceries["id"], "order", 1)
    assert next(t for t in bank.transactions(c) if t["id"] == nike["id"])["match_kind"] is None
    eid = bank.add_as_expense(c, groceries["id"], "Groceries?", "other")
    row = c.execute("SELECT * FROM expenses WHERE id = ?", (eid,)).fetchone()
    assert (row["amount"], row["date"], row["vendor"]) == (62.18, "2026-10-07", "Groceries?")
    assert next(t for t in bank.transactions(c) if t["id"] == groceries["id"])["match_kind"] == "expense"


def test_bank_pages(tmp_path):
    path = str(tmp_path / "w.db")
    seed(db.connect(path))
    client = create_app(path).test_client()
    r = client.post("/bank/import", data={"files": [(io.BytesIO(CHASE.encode()), "chase.csv"),
                                                    (io.BytesIO(b"junk"), "junk.csv")], "account": ""},
                    content_type="multipart/form-data", follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "chase.csv: 5 new into chase" in page and "junk.csv: couldn" in page
    assert "NIKE.COM" in page and "C001" in page
    tx = bank.transactions(db.connect(path), "unmatched")[0]
    opts = client.get(f"/bank/{tx['id']}/candidates?q=Nike").get_json()["options"]
    assert opts and opts[0]["kind"] == "order"
    client.post(f"/bank/{tx['id']}/match", data={"action": "match", "target": f"order:{opts[0]['id']}"})
    assert bank.transactions(db.connect(path))[0] is not None
    assert "Card charge" in client.get("/expenses").get_data(as_text=True)
    assert "Get your free Plaid keys" in client.get("/bank/plaid").get_data(as_text=True)
    assert client.get("/bank?show=unmatched").status_code == 200


class FakePlaid:
    def __init__(self):
        self.pages = []
        self.calls = []

    def __call__(self, con, path, payload, creds=None, timeout=30):
        self.calls.append(path)
        if path == "/link/token/create":
            return {"link_token": "link-sandbox-1"}
        if path == "/item/public_token/exchange":
            return {"item_id": "item1", "access_token": "access-sandbox-SECRET"}
        if path == "/accounts/get":
            return {"accounts": [{"account_id": "acc1", "name": "Sapphire", "mask": "1234"}]}
        if path == "/transactions/sync":
            return self.pages.pop(0)
        if path == "/item/remove":
            return {}
        raise AssertionError(path)


def test_plaid_bring_your_own_keys(tmp_path, monkeypatch):
    c = plaid.ensure(db.connect(str(tmp_path / "p.db")))
    seed(c)
    fake = FakePlaid()
    monkeypatch.setattr(plaid, "call", fake)
    plaid.save_keys(c, "client-abcdef123456", "sandbox-secret-xyz987", "sandbox")
    assert plaid.keys(c) == ("client-abcdef123456", "sandbox-secret-xyz987", "sandbox")
    assert plaid.settings(c)["client_hint"] == "…3456"
    plaid.exchange(c, "public-sandbox-1", "Chase")
    dump = "\n".join(c.iterdump())
    assert "sandbox-secret-xyz987" not in dump and "access-sandbox-SECRET" not in dump and "client-abcdef" not in dump
    tx = lambda tid, amount, day, pending=False, replaces=None: {
        "transaction_id": tid, "account_id": "acc1", "amount": amount, "date": day, "name": "NIKE.COM",
        "merchant_name": "Nike", "pending": pending, "pending_transaction_id": replaces, "iso_currency_code": "USD"}
    fake.pages = [{"added": [tx("p1", 130.0, "2026-10-02", pending=True), tx("t9", 5.0, "2026-10-01")],
                   "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}]
    plaid.run(c)
    assert bank.stats(c)["orders"] == 0  # pending charges wait until they post
    bank.set_match(c, c.execute("SELECT id FROM bank_transactions WHERE uid='plaid:p1'").fetchone()[0], "order", 1)
    fake.pages = [{"added": [tx("t1", 130.0, "2026-10-03", replaces="p1")], "modified": [],
                   "removed": [{"transaction_id": "t9"}], "next_cursor": "c2", "has_more": False}]
    plaid.run(c)
    rows = {r["uid"]: r for r in c.execute("SELECT * FROM bank_transactions")}
    assert set(rows) == {"plaid:t1"} and rows["plaid:t1"]["match_kind"] == "order"
    assert rows["plaid:t1"]["match_how"] == "manual"  # your match carried over to the posted charge
    assert c.execute("SELECT cursor FROM plaid_items").fetchone()[0] == "c2"
    assert bank.accounts(c)[0]["name"] == "Chase Sapphire …1234"
    plaid.remove_item(c, "item1", keep_transactions=False)
    assert not plaid.items(c) and not c.execute("SELECT * FROM bank_transactions").fetchall()


def test_plaid_errors_are_kept_and_due(tmp_path, monkeypatch):
    c = plaid.ensure(db.connect(str(tmp_path / "p.db")))
    fake = FakePlaid()
    monkeypatch.setattr(plaid, "call", fake)
    assert not plaid.due(c, 6)
    plaid.save_keys(c, "cid", "sec", "production")
    plaid.exchange(c, "pub", "Amex")
    assert plaid.due(c, 6)

    def broken(con, path, payload, creds=None, timeout=30):
        raise plaid.PlaidError("the login details changed", "ITEM_LOGIN_REQUIRED")
    monkeypatch.setattr(plaid, "call", broken)
    plaid.run(c)
    assert "Reconnect" in plaid.items(c)[0]["error"]
    client = create_app(str(tmp_path / "p.db")).test_client()
    assert client.post("/bank/plaid/link-token").get_json()["error"].startswith("the login details changed")
    assert "Reconnect" in client.get("/bank/plaid").get_data(as_text=True)
