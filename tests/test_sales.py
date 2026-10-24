import json
from datetime import date

import pytest

from jigstream import bank, budget, db, inbox, sales, wallet
from jigstream.app import create_app
from jigstream.config import Config
from jigstream.extract import email_prompt, normalize
from jigstream.mail import Email
from jigstream.orderstore import Store
from jigstream.sync import run_sync

TODAY = date(2026, 10, 22)


@pytest.fixture(autouse=True)
def key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setattr(wallet, "_key", None)


def mail(subject, sender="StockX <noreply@stockx.com>", body="You sold it! Payout $171.40", mid=None):
    return Email(mid or subject, sender, subject, "2026-10-20T10:00:00+00:00", body, "me@gmail.com")


def order(c, number, retailer, day, total, items, status="delivered"):
    c.execute("INSERT INTO tracked_orders (merge_key, retailer, order_number, order_date, total, items,"
              " shipping_status) VALUES (?,?,?,?,?,?,?)",
              (f"{retailer}:{number}", retailer, number, day, total, json.dumps(items), status))
    c.commit()


@pytest.fixture
def con(tmp_path):
    c = sales.ensure(db.connect(str(tmp_path / "s.db")))
    order(c, "C01", "Nike", "2026-10-02", 127.20, [{"name": "Nike Dunk Low Retro Panda", "price": 127.20}])
    order(c, "T9", "Target", "2026-10-05", 311.94, [{"name": "Pokemon Scarlet Violet Elite Trainer Box", "price": 51.99,
                                                     "quantity": 6}])
    order(c, "X1", "Nike", "2026-10-06", 99.0, [{"name": "Nike Dunk Low Retro Panda", "price": 99.0}], "cancelled")
    c.execute("INSERT INTO expenses (message_id, vendor, category, amount, date) VALUES ('e1','Kodai','bot',75,'2026-10-10')")
    c.execute("INSERT INTO expenses (message_id, vendor, category, amount, date) VALUES ('e2','Packet','proxy',45,'2026-10-12')")
    c.commit()
    return c


def test_sale_emails_are_read_as_sales():
    r = normalize({"kind": "sale", "platform": "StockX", "items": [{"name": "Dunk Low Panda"}], "size": "10",
                   "sale_price": 190, "fees": 18.60, "order_number": "12345-678"}, mail("You sold"))
    assert (r["kind"], r["platform"], r["item"], r["payout"], r["date"]) == ("sale", "StockX", "Dunk Low Panda",
                                                                              171.40, "2026-10-20")
    r = normalize({"kind": "sale"}, mail("Your payout is on the way", "eBay <ebay@ebay.com>", "Payout total: $612.33"))
    assert r["payout"] == 612.33 and r["item"] is None and r["platform"] == "eBay"


def test_prompt_names_built_in_and_your_own_platforms():
    p = email_prompt(mail("Your item sold", "Depop <hello@depop.com>"), platforms=["Depop"])
    assert "selling platform: Depop" in p
    assert "selling platform: StockX" in email_prompt(mail("You sold"))
    assert "selling platform" not in email_prompt(mail("Hi", "Nike <o@nike.com>"))


def test_gmail_search_gets_your_platforms():
    q = sales.with_terms('(order OR shipped) -in:spam', ["Depop", "Facebook Marketplace", "order"])
    assert q == '(order OR shipped OR Depop OR "Facebook Marketplace") -in:spam'


def test_sold_then_paid_out_is_one_sale_and_links_its_order(con):
    sales.save_email(con, mail("You sold", mid="m1"), {"platform": "StockX", "item": "Nike Dunk Low Panda",
                                                       "size": "10", "sale_price": 190, "order_number": "123-45",
                                                       "date": "2026-10-18"})
    sales.save_email(con, mail("Payout sent", mid="m2"), {"platform": "stockx", "payout": 171.40,
                                                          "order_number": "12345", "date": "2026-10-20"})
    rows = sales.rows(con)
    assert len(rows) == 1
    s = rows[0]
    assert (s["payout"], s["order_ref"], s["cost"], s["profit"]) == (171.40, "C01", 127.20, 44.20)


def test_lump_payouts_are_left_out_when_sales_are_itemized(con):
    sales.save_email(con, mail("Sold", mid="a"), {"platform": "eBay", "item": "Elite Trainer Box", "payout": 60,
                                                  "date": "2026-10-15"})
    sales.save_email(con, mail("Payout", mid="b"), {"platform": "eBay", "payout": 600, "date": "2026-10-16"})
    sales.save_email(con, mail("Payout", mid="c"), {"platform": "Alias", "payout": 200, "date": "2026-10-16"})
    counted = {(r["platform"], r["payout"]): r["counted"] for r in sales.rows(con)}
    assert counted == {("eBay", 60): 1, ("eBay", 600): 0, ("Alias", 200): 1}


def test_month_profit_break_even_and_goal(con):
    sales.add(con, {"platform": "StockX", "item": "Nike Dunk Low Retro Panda", "payout": 171.40, "date": "2026-10-18"})
    sales.add(con, {"platform": "Local", "item": "Pokemon Elite Trainer Box x6", "payout": 420, "cost": 311.94,
                    "date": "2026-10-19"})
    sales.set_setting(con, "monthly_goal", "300")
    p = sales.month_numbers(con, "2026-10", TODAY)
    assert p["income"] == pytest.approx(591.40) and p["costs"] == pytest.approx(439.14)
    assert p["flips"] == pytest.approx(152.26) and p["expenses"] == 120 and p["net"] == pytest.approx(32.26)
    assert p["target"] == 420 and p["need"] == pytest.approx(267.74)
    assert p["avg"] == pytest.approx(76.13) and p["more"] == 4
    h = sales.history(con, 3, TODAY)
    assert [round(r["net"], 2) for r in h["rows"]] == [0, 0, 32.26]


def test_cancelled_orders_and_claimed_orders_are_not_reused(con):
    sales.add(con, {"platform": "GOAT", "item": "Dunk Low Retro Panda", "payout": 150, "date": "2026-10-20"})
    sales.add(con, {"platform": "GOAT", "item": "Dunk Low Retro Panda", "payout": 150, "date": "2026-10-21"})
    linked = [r["order_ref"] for r in sales.rows(con)]
    assert sorted(linked, key=str) == ["C01", None]  # one pair bought, the cancelled one never counts
    assert [o["order_number"] for o in sales.unsold(con)] == ["T9"]


def test_payout_deposits_from_statements(con):
    rows = "Date,Description,Amount\n10/15/2026,STOCKX PAYOUT 8821,250.00\n10/16/2026,ZELLE FROM MOM,40.00\n"
    bank.import_file(con, "chk.csv", rows.encode(), "Checking", "negative")
    found = sales.deposits(con)
    assert [(d["platform"], d["amount"]) for d in found] == [("StockX", -250.0)]
    sales.add_deposit(con, found[0]["id"])
    assert sales.deposits(con) == [] and sales.rows(con)[0]["payout"] == 250


def test_inbox_sale_label_teaches_the_model():
    assert inbox.corrected_answer({"kind": "other"}, "sale") == {"kind": "sale"}
    assert inbox.LABELS["sale"][0] == "Sale / payout"


def test_sync_saves_sales(tmp_path):
    class LLM:
        def chat_json(self, system, user):
            assert '"sale"' in system
            return {"kind": "sale", "platform": "GOAT", "items": [{"name": "Jordan 4"}], "payout": 210}
    store = Store(db.connect(str(tmp_path / "x.db")))
    counts = run_sync(Config(), store, emails=[mail("Your item sold", "GOAT <hi@goat.com>")], llm=LLM(),
                      log=lambda *a, **k: None)
    assert counts["sale"] == 1
    assert [(r["platform"], r["payout"]) for r in sales.rows(store.db)] == [("GOAT", 210)]


def test_profit_page_and_forms(tmp_path, con):
    path = str(tmp_path / "s.db")
    client = create_app(path).test_client()
    client.post("/expenses/platforms", data={"name": "Depop"})
    client.post("/expenses/profit/goal", data={"goal": "$500"})
    c = db.connect(path)
    assert "Depop" in sales.platforms(c) and sales.goal(c) == 500
    client.post("/expenses/sales/new", data={"platform": "Depop", "item": "Pokemon ETB", "payout": "420",
                                             "order_id": str(c.execute("SELECT id FROM tracked_orders WHERE"
                                                                       " order_number = 'T9'").fetchone()[0]),
                                             "date": date.today().isoformat()})
    s = sales.rows(c)[0]
    assert s["cost"] == 311.94 and s["profit"] == pytest.approx(108.06)
    page = client.get("/expenses/profit").get_data(as_text=True)
    assert "Profit on flips" in page and "Pokemon ETB" in page and "Depop" in page
    client.post(f"/expenses/sales/{s['id']}/count", data={"counted": "0"})
    assert sales.rows(c)[0]["counted"] == 0
    client.post(f"/expenses/sales/{s['id']}/delete")
    assert sales.rows(c) == []
    assert "Profit in" in client.get("/expenses").get_data(as_text=True)
    assert budget.month_of(TODAY) == "2026-10"


def test_a_deposit_for_a_payout_email_is_tied_to_it_and_the_dashboard_shows_money_in(con, tmp_path):
    sales.add(con, {"platform": "eBay", "item": "ETB", "payout": 88.5, "date": "2026-10-12"}, "email")
    rows = ("Date,Description,Amount\n10/15/2026,EBAY COM PAYOUT X1,88.50\n10/16/2026,MERCARI INC PAYOUT,60.00\n"
            "10/17/2026,EBAY COM PAYOUT X2,40.00\n")
    bank.import_file(con, "chk.csv", rows.encode(), "Checking", "negative")
    assert sorted((d["platform"], d["amount"]) for d in sales.deposits(con)) == [("Mercari", -60.0), ("eBay", -40.0)]
    assert len(sales.rows(con)) == 1  # the eBay payout isn't counted twice
    from jigstream import overview
    assert overview.summary(con, "2026")["income"] == 88.5
    page = create_app(str(tmp_path / "s.db")).test_client().get("/?year=2026").get_data(as_text=True)
    assert "Sold in 2026" in page and "$88.50" in page
