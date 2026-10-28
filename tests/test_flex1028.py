"""Expenses, sales and shipping can each be fixed and relabelled by hand, and every fix teaches the AI."""
from jigstream import db, expensepages, inbox, ordersheet, recurring, sales
from jigstream.app import create_app
from jigstream.orderstore import Store

from test_round4 import order


class Now:
    """Runs background work right away, so a test sees what it did."""
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.run = lambda: target(*args, **(kwargs or {}))

    def start(self):
        self.run()


def setup(tmp_path, monkeypatch):
    path = str(tmp_path / "f.db")
    store = Store(db.connect(path))
    app = create_app(path)
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@gmail.com:pw")
    monkeypatch.setattr(expensepages, "llm_for", lambda config: None)
    monkeypatch.setattr(expensepages.threading, "Thread", Now)
    labelled, taught = [], []
    monkeypatch.setattr(inbox, "label_emails", lambda config, st, llm, picks, label, sale=None, what=None:
                        labelled.append((picks[0]["message_id"], label, what)))
    monkeypatch.setattr(expensepages, "teach_expense", lambda config, eid, path=None: taught.append(("expense", eid)))
    monkeypatch.setattr(expensepages, "teach_sale", lambda config, sid, path=None: taught.append(("sale", sid)))
    return path, store, app.test_client(), labelled, taught


def from_email(store, mid, vendor, amount, category="proxy"):
    store.db.execute("INSERT INTO emails (message_id, account, sender, subject, date) VALUES (?, 'me@gmail.com', ?, ?, ?)",
                     (mid, f"hi@{vendor.lower()}.com", f"{vendor} receipt", "2026-10-20"))
    eid = store.db.execute("INSERT INTO expenses (message_id, account, vendor, amount, category, date)"
                           " VALUES (?, 'me@gmail.com', ?, ?, ?, '2026-10-20')", (mid, vendor, amount, category)).lastrowid
    store.db.commit()
    return eid


def test_an_expense_that_is_really_a_sale_or_an_ad_moves_and_teaches(tmp_path, monkeypatch):
    path, store, c, labelled, _ = setup(tmp_path, monkeypatch)
    sale = from_email(store, "<s@x>", "StockX", 120.0)
    ad = from_email(store, "<a@x>", "Whop", 0.0)
    odd = from_email(store, "<o@x>", "Discord", 5.0)
    typed = Store(db.connect(path)).add_expense({"vendor": "Gas", "amount": 20.0, "category": "gas"})
    for page in ("/expenses/review", "/expenses/ledger", "/expenses"):
        html = c.get(page).get_data(as_text=True)
        assert "Not an expense? It's…" in html and "Something else (type it)" in html, page
    assert "Read the email" in c.get("/expenses/ledger").get_data(as_text=True)
    c.post(f"/expenses/{sale}/is", data={"label": "sale"})
    c.post(f"/expenses/{ad}/is", data={"label": "ad"})
    c.post(f"/expenses/{odd}/is", data={"label": "custom", "what": "Discord Nitro gift"})
    r = c.post(f"/expenses/{typed}/is", data={"label": "custom", "what": ""})  # no words: nothing happens
    assert r.status_code == 302
    assert labelled == [("<s@x>", "sale", None), ("<a@x>", "ad", None), ("<o@x>", "other", "Discord Nitro gift")]
    left = [r[0] for r in db.connect(path).execute("SELECT vendor FROM expenses")]
    assert left == ["Gas"]
    c.post(f"/expenses/{typed}/is", data={"label": "other"})  # typed in by hand: just removed, no email to read
    assert db.connect(path).execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0 and len(labelled) == 3


def test_editing_an_expense_teaches_and_can_track_it_monthly(tmp_path, monkeypatch):
    path, store, c, _, taught = setup(tmp_path, monkeypatch)
    eid = from_email(store, "<w@x>", "Webshare", 30.0)
    c.post(f"/expenses/{eid}/edit", data={"vendor": "Webshare", "amount": "30", "category": "proxy"})
    assert taught == []  # nothing changed
    c.post(f"/expenses/{eid}/edit", data={"vendor": "Webshare", "amount": "32.50", "category": "proxy",
                                          "repeats": "monthly"})
    e = Store(db.connect(path)).expense(eid)
    assert (e["amount"], e["billing_period"]) == (32.5, "monthly") and e["subscription_id"]
    assert taught == [("expense", eid)]
    subs = recurring.subscriptions(db.connect(path))
    assert [(s["vendor"], s["period"], s["amount"]) for s in subs] == [("Webshare", "monthly", 32.5)]
    c.post(f"/expenses/{eid}/edit", data={"vendor": "Webshare", "amount": "32.50", "category": "proxy",
                                          "repeats": "quarterly"})
    assert len(recurring.subscriptions(db.connect(path))) == 1  # already tracked


def test_fixing_a_sale_teaches_and_a_sale_can_be_something_else(tmp_path, monkeypatch):
    path, store, c, labelled, taught = setup(tmp_path, monkeypatch)
    con = sales.ensure(store.db)
    con.execute("INSERT INTO sales (message_id, platform, item, sale_price, payout, account, source, date)"
                " VALUES ('<e@x>', 'eBay', 'ETB', 80, 70, 'me@gmail.com', 'email', '2026-10-20')")
    con.execute("INSERT INTO sales (message_id, platform, item, payout, account, source, date)"
                " VALUES ('<t@x>', 'Tradepost', 'Cards', 40, 'me@gmail.com', 'email', '2026-10-21')")
    con.execute("INSERT INTO emails (message_id, account, sender, subject, date)"
                " VALUES ('<t@x>', 'me@gmail.com', 'tp@x.com', 'Your order', '2026-10-21')")
    con.commit()
    ebay, tp = [r[0] for r in con.execute("SELECT id FROM sales ORDER BY id")]
    html = c.get("/expenses/profit?month=2026-10").get_data(as_text=True)
    assert "Not a sale? It's…" in html and "Read the email" in html
    c.post(f"/expenses/sales/{ebay}/edit", data={"platform": "eBay", "item": "ETB", "sale_price": "85", "payout": "74"})
    assert taught == [("sale", ebay)]
    c.post(f"/expenses/sales/{tp}/is", data={"label": "ordered"})
    assert labelled == [("<t@x>", "ordered", None)]
    assert [r[0] for r in db.connect(path).execute("SELECT platform FROM sales")] == ["eBay"]


def test_shipping_fixes_to_dates_and_names_teach_too(tmp_path, monkeypatch):
    path = str(tmp_path / "s.db")
    store = Store(db.connect(path))
    order(store, 1, "in_transit", tracking_number="1Z999AA10123456784", carrier="UPS")
    oid = store.orders()[0]["id"]
    app = create_app(path)
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@gmail.com:pw")
    monkeypatch.setattr(ordersheet.threading, "Thread", Now)
    taught = []
    monkeypatch.setattr(ordersheet, "teach", lambda config, order_id, changed, path=None: taught.append(changed))
    c = app.test_client()
    sheet = c.get(f"/orders/{oid}/sheet").get_data(as_text=True)
    assert "Wrong carrier, tracking number or delivery date?" in sheet
    c.post(f"/orders/{oid}/edit", data={"estimated_delivery": "2026-10-30", "carrier": "USPS", "back": "1"})
    assert taught == [{"estimated_delivery": "2026-10-30", "carrier": "USPS"}]
    assert ordersheet._shows("Arriving Thursday, Oct 30", "estimated_delivery", "2026-10-30")
    assert ordersheet._shows("Estimated delivery: October 30", "estimated_delivery", "2026-10-30")
    assert ordersheet._shows("Delivery by 10/30", "estimated_delivery", "2026-10-30")
    assert not ordersheet._shows("Arriving Oct 3", "estimated_delivery", "2026-10-30")
