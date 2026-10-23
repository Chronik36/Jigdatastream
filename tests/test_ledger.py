from datetime import date

import pytest

from jigstream import bank, db, ledger, wallet
from jigstream.app import create_app

STATEMENT = """Date,Description,Amount
10/03/2026,NIKE.COM 800-806-6453 OR,130.00
10/05/2026,IPROYAL PROXIES,45.00
10/08/2026,WHOP*RESTOCK ROOM,46.38
10/09/2026,SQ *CAPSOLVER,25.00
10/20/2026,TRADER JOES #123,62.18
"""


@pytest.fixture(autouse=True)
def key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setattr(wallet, "_key", None)


def expense(c, mid, vendor, amount, day, category="proxy", description=None):
    c.execute("INSERT INTO expenses (message_id, vendor, category, description, amount, date, account)"
              " VALUES (?,?,?,?,?,?, 'me@gmail.com')", (mid, vendor, category, description, amount, day))


@pytest.fixture
def con(tmp_path):
    c = ledger.ensure(db.connect(str(tmp_path / "l.db")))
    c.execute("INSERT INTO tracked_orders (merge_key, retailer, order_number, order_date, total, shipping_status)"
              " VALUES ('nike:1','Nike','C001','2026-10-02',130.0,'shipped')")
    expense(c, "m1", "IPRoyal", 45, "2026-10-05")
    expense(c, "m2", "Restock Room", 45, "2026-10-07", "cook_group")  # card charged 46.38 (fx fee)
    expense(c, "m3", "Kodai", 75, "2026-10-12", "bot")  # no charge anywhere
    expense(c, "m4", "AYCD", 200, "2026-09-01", "bot")  # before the statement starts
    c.commit()
    bank.import_file(c, "amex.csv", STATEMENT.encode(), "Amex Gold", "positive")
    return c


def states(c):
    return {(r["kind"], r["name"]): r["state"] for r in ledger.rows(c)}


def test_every_receipt_and_charge_gets_a_state(con):
    s = states(con)
    assert s[("expense", "IPRoyal")] == "matched"
    assert s[("order", "Nike")] == "matched"
    assert s[("expense", "Restock Room")] == "close"  # one row for the receipt and its charge
    assert s[("expense", "Kodai")] == "receipt_only"
    assert s[("expense", "AYCD")] == "no_statement"
    assert s[("charge", "SQ *CAPSOLVER")] == "charge_only"
    assert s[("charge", "TRADER JOES #123")] == "charge_only"
    assert ledger.counts(ledger.rows(con)) == {"matched": 2, "close": 1, "receipt_only": 1, "no_statement": 1,
                                               "charge_only": 2, "all": 7}


def test_close_amounts_need_the_store_name_and_stay_within_a_few_dollars():
    assert ledger.is_close(45, 46.38)
    assert ledger.is_close(200, 211)  # 6% of 200 is 12
    assert not ledger.is_close(45, 49)
    assert not ledger.is_close(45, 45.0)  # exact amounts are the normal matcher's job


def test_confirming_or_rejecting_a_close_amount(con):
    tx = con.execute("SELECT id FROM bank_transactions WHERE description LIKE 'WHOP%'").fetchone()[0]
    eid = con.execute("SELECT id FROM expenses WHERE vendor = 'Restock Room'").fetchone()[0]
    ledger.reject_close(con, tx, "expense", eid)
    s = states(con)
    assert s[("expense", "Restock Room")] == "receipt_only" and s[("charge", "WHOP*RESTOCK ROOM")] == "charge_only"
    con.execute("DELETE FROM ledger_rejects")
    ledger.confirm_close(con, tx, "expense", eid)
    assert states(con)[("expense", "Restock Room")] == "matched"
    assert bank.auto_match(con) == 0  # a later import never undoes it


def test_paid_another_way(con):
    eid = con.execute("SELECT id FROM expenses WHERE vendor = 'Kodai'").fetchone()[0]
    ledger.set_paid_with(con, eid, "PayPal balance")
    row = next(r for r in ledger.rows(con) if r["name"] == "Kodai")
    assert row["state"] == "other_way" and row["expense"]["paid_with"] == "PayPal balance"
    ledger.set_paid_with(con, eid, "")
    assert states(con)[("expense", "Kodai")] == "receipt_only"


def test_ignored_charges_leave_the_ledger(con):
    tx = con.execute("SELECT id FROM bank_transactions WHERE description LIKE 'TRADER%'").fetchone()[0]
    bank.set_match(con, tx, None, how="ignored")
    assert ("charge", "TRADER JOES #123") not in states(con)


def test_month_chart_is_drawn_to_scale(con):
    chart = ledger.month_chart(con, n=3, today=date(2026, 10, 22))
    assert [b["label"] for b in chart["bars"]] == ["Aug", "Sep", "Oct"]
    oct_ = chart["bars"][-1]
    assert oct_["total"] == 165  # 45 + 45 + 75
    top = chart["grid"][-1]["v"]
    assert top == 200 and chart["bars"][1]["total"] == 200  # Sep: AYCD
    plot_h = chart["base"] - chart["grid"][-1]["y"]
    assert abs(sum(s["h"] for s in oct_["segs"]) - 165 / top * plot_h) < 0.5


def test_last_months_cross_the_year():
    assert ledger.last_months(3, date(2026, 2, 1)) == ["2025-12", "2026-01", "2026-02"]


def test_pages_and_hand_edits(tmp_path, con):
    path = str(tmp_path / "l.db")
    client = create_app(path).test_client()
    page = client.get("/expenses").get_data(as_text=True)
    assert "Spend by month" in page and "Needs a look" in page
    page = client.get("/expenses/ledger").get_data(as_text=True)
    assert "Restock Room" in page and "Close amount" in page and "SQ *CAPSOLVER" in page
    page = client.get("/expenses/ledger?show=receipt_only").get_data(as_text=True)
    assert "Kodai" in page and "IPRoyal" not in page

    client.post("/expenses/new", data={"vendor": "Capsolver", "amount": "$25.00", "category": "captcha",
                                       "date": "2026-10-09", "description": "Credits"})
    c = db.connect(path)
    eid = c.execute("SELECT id FROM expenses WHERE vendor = 'Capsolver'").fetchone()[0]
    assert c.execute("SELECT amount, category FROM expenses WHERE id = ?", (eid,)).fetchone()[:] == (25.0, "captcha")
    client.post(f"/expenses/{eid}/edit", data={"vendor": "Capsolver", "amount": "30", "category": "captcha",
                                               "date": "2026-10-09"})
    assert c.execute("SELECT amount FROM expenses WHERE id = ?", (eid,)).fetchone()[0] == 30.0

    kodai = c.execute("SELECT id FROM expenses WHERE vendor = 'Kodai'").fetchone()[0]
    client.post(f"/expenses/{kodai}/paid", data={"method": "__other__", "other": "Venmo"})
    assert c.execute("SELECT paid_with FROM expenses WHERE id = ?", (kodai,)).fetchone()[0] == "Venmo"

    ipr = c.execute("SELECT id FROM expenses WHERE vendor = 'IPRoyal'").fetchone()[0]
    client.post(f"/expenses/{ipr}/delete")
    assert c.execute("SELECT match_kind FROM bank_transactions WHERE description = 'IPROYAL PROXIES'").fetchone()[0] is None


def test_expenses_page_with_blank_amounts(tmp_path):
    from jigstream import db as jdb
    from datetime import date
    client = create_app(str(tmp_path / "k.db")).test_client()
    client.get("/expenses")
    c = jdb.connect(str(tmp_path / "k.db"))
    for vendor, category, amount in (("Whop", "software", None), ("Oxylabs", "proxies", 10.0)):
        c.execute("INSERT INTO expenses (vendor, category, amount, date) VALUES (?, ?, ?, ?)",
                  (vendor, category, amount, date.today().isoformat()))
    c.commit()
    assert client.get("/expenses").status_code == 200
