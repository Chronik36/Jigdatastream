from datetime import date

import pytest

from jigstream import bank, db, recurring, wallet
from jigstream.app import create_app

TODAY = date(2026, 10, 22)


@pytest.fixture(autouse=True)
def key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setattr(wallet, "_key", None)


def expense(c, mid, vendor, amount, day, category="proxy", billing=None):
    c.execute("INSERT INTO expenses (message_id, vendor, category, amount, date, billing_period, account)"
              " VALUES (?,?,?,?,?,?, 'me@gmail.com')", (mid, vendor, category, amount, day, billing))
    c.commit()


@pytest.fixture
def con(tmp_path):
    c = recurring.ensure(db.connect(str(tmp_path / "r.db")))
    for i, d in enumerate(["2026-08-05", "2026-09-05", "2026-10-05"]):
        expense(c, f"v{i}", "Vultr", 48, d, "server")
    for i, d in enumerate(["2026-02-02", "2026-05-02", "2026-08-02"]):
        expense(c, f"r{i}", "Restock Room", 135, d, "cook_group")
    for i, d in enumerate(["2026-07-01", "2026-08-15", "2026-09-29"]):
        expense(c, f"k{i}", "Valor", 90, d, "bot")  # every 45 days
    expense(c, "a1", "Packet Proxies", 100, "2026-09-28", billing="monthly")
    expense(c, "x1", "IPRoyal", 60, "2026-03-03")
    expense(c, "x2", "IPRoyal", 60, "2026-10-11")  # two buys months apart: not a schedule
    return c


def test_dates_move_by_calendar_months():
    assert recurring.advance(date(2026, 1, 31), "monthly") == date(2026, 2, 28)
    assert recurring.advance(date(2026, 11, 30), "quarterly") == date(2027, 2, 28)
    assert recurring.advance(date(2026, 10, 5), "yearly") == date(2027, 10, 5)
    assert recurring.advance(date(2026, 10, 5), "custom", 45) == date(2026, 11, 19)


def test_vendor_names_and_card_descriptions_share_a_key():
    assert recurring.vkey("Packet Proxies") == recurring.vkey("PACKETPROXIES.COM 8885551234") == "packetproxies"


def test_suggests_schedules_from_repeat_receipts(con):
    ideas = {i["name"]: (i["period"], i["every_days"]) for i in recurring.suggestions(con, TODAY)}
    assert ideas["Vultr"] == ("monthly", None)
    assert ideas["Restock Room"] == ("quarterly", None)
    assert ideas["Valor"] == ("custom", 45)
    assert ideas["Packet Proxies"] == ("monthly", None)  # one receipt that says it renews monthly
    assert "IPRoyal" not in ideas


def test_suggests_from_repeat_card_charges(con):
    rows = "Date,Description,Amount\n08/12/2026,CAPSOLVER.COM 555,25.00\n09/12/2026,CAPSOLVER.COM 556,25.00\n" \
           "10/12/2026,CAPSOLVER.COM 557,25.00\n"
    bank.import_file(con, "c.csv", rows.encode(), "Amex", "positive")
    idea = next(i for i in recurring.suggestions(con, TODAY) if i["kind"] == "charge")
    assert idea["period"] == "monthly" and idea["amount"] == 25
    sid = recurring.track(con, "Capsolver", "monthly", category="captcha", tx_id=idea["id"])
    sub = next(s for s in recurring.subscriptions(con, TODAY) if s["id"] == sid)
    assert sub["payments"] == 3 and sub["next_due"] == "2026-11-12"
    assert con.execute("SELECT COUNT(*) FROM bank_transactions WHERE match_kind = 'expense'").fetchone()[0] == 3


def test_tracking_links_receipts_and_finds_next_renewal(con):
    vid = con.execute("SELECT id FROM expenses WHERE vendor = 'Vultr' ORDER BY date DESC").fetchone()[0]
    sid = recurring.track(con, None, "monthly", expense_id=vid)
    subs = recurring.subscriptions(con, TODAY)
    s = next(s for s in subs if s["id"] == sid)
    assert (s["vendor"], s["payments"], s["last_paid"], s["next_due"]) == ("Vultr", 3, "2026-10-05", "2026-11-05")
    assert s["state"] == "ok" and s["per_month"] == 48
    assert "Vultr" not in {i["name"] for i in recurring.suggestions(con, TODAY)}
    expense(con, "v9", "Vultr", 48, "2026-11-05", "server")  # the next receipt arrives
    recurring.refresh(con)
    assert next(s for s in recurring.subscriptions(con, TODAY) if s["id"] == sid)["next_due"] == "2026-12-05"


def test_price_changes_missed_renewals_and_charges_after_cancelling(con):
    sid = recurring.track(con, "Restock Room", "quarterly", category="cook_group")
    s = next(s for s in recurring.subscriptions(con, TODAY) if s["id"] == sid)
    assert s["next_due"] == "2026-11-02" and round(s["per_month"], 2) == 45
    expense(con, "r9", "Restock Room", 150, "2026-11-02", "cook_group")
    recurring.refresh(con)
    s = next(s for s in recurring.subscriptions(con, date(2026, 11, 3)) if s["id"] == sid)
    assert s["state_text"] == "Price went up $15.00"
    s = next(s for s in recurring.subscriptions(con, date(2027, 2, 20)) if s["id"] == sid)
    assert s["state"] == "bad" and s["state_text"].startswith("Missed")
    recurring.cancel(con, sid, date(2026, 11, 10))
    assert next(s for s in recurring.subscriptions(con, TODAY) if s["id"] == sid)["state"] == "off"
    expense(con, "r10", "Restock Room", 150, "2027-02-02", "cook_group")
    recurring.refresh(con)
    s = next(s for s in recurring.subscriptions(con, TODAY) if s["id"] == sid)
    assert s["state"] == "bad" and s["state_text"].startswith("Charged after you cancelled")


def test_upcoming_and_totals(con):
    recurring.track(con, "Vultr", "monthly")
    recurring.track(con, "Valor", "custom", 45)
    subs = recurring.subscriptions(con, TODAY)
    soon = recurring.upcoming(subs, TODAY, 30)
    assert [(d.isoformat(), s["vendor"]) for d, s in soon] == [("2026-11-05", "Vultr"), ("2026-11-13", "Valor")]
    t = recurring.totals(subs, TODAY)
    assert t["next30"] == 138 and round(t["per_month"], 2) == round(48 + 90 * 30.44 / 45, 2)
    weeks = recurring.calendar_weeks(subs, TODAY)
    assert weeks[0][0]["day"] == date(2026, 10, 18) and len(weeks) == 5


def test_dismissed_suggestions_stay_hidden(con):
    recurring.dismiss(con, recurring.vkey("Vultr"))
    assert "Vultr" not in {i["name"] for i in recurring.suggestions(con, TODAY)}


def test_recurring_pages(tmp_path, con):
    path = str(tmp_path / "r.db")
    client = create_app(path).test_client()
    page = client.get("/expenses/recurring").get_data(as_text=True)
    assert "Looks like every 3 months" in page and "Restock Room" in page
    client.post("/expenses/recurring/track", data={"vendor": "Valor", "period": "custom", "every_days": "45",
                                                   "category": "bot", "amount": "90"})
    page = client.get("/expenses/recurring").get_data(as_text=True)
    assert "Every 45 days" in page
    c = db.connect(path)
    sid = c.execute("SELECT id FROM subscriptions WHERE vendor = 'Valor'").fetchone()[0]
    client.post(f"/expenses/recurring/{sid}/edit", data={"vendor": "Valor AIO", "period": "monthly", "amount": "95"})
    assert c.execute("SELECT vendor, period, every_days FROM subscriptions WHERE id = ?", (sid,)).fetchone()[:] \
        == ("Valor AIO", "monthly", None)
    client.post(f"/expenses/recurring/{sid}/delete")
    assert c.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
    assert "Coming up" in client.get("/expenses").get_data(as_text=True)
    assert "Repeats" in client.get("/expenses/ledger").get_data(as_text=True)
