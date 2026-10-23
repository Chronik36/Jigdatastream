from datetime import date

import pytest

from jigstream import budget, db, recurring, wallet
from jigstream.app import create_app

TODAY = date(2026, 10, 22)


@pytest.fixture(autouse=True)
def key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setattr(wallet, "_key", None)


def expense(c, mid, vendor, amount, day, category):
    c.execute("INSERT INTO expenses (message_id, vendor, category, amount, date) VALUES (?,?,?,?,?)",
              (mid, vendor, category, amount, day))
    c.commit()


@pytest.fixture
def con(tmp_path):
    c = budget.ensure(db.connect(str(tmp_path / "b.db")))
    expense(c, "1", "Packet", 100, "2026-09-28", "proxy")
    expense(c, "2", "Packet", 520, "2026-10-03", "proxy")
    expense(c, "3", "Kodai", 75, "2026-09-24", "bot")
    expense(c, "4", "Gmail Farm", 296, "2026-10-10", "account")
    expense(c, "5", "Old", 900, "2026-08-10", "proxy")
    return c


def test_budgets_apply_from_the_month_they_were_set(con):
    budget.set_budgets(con, {"proxy": 500, "bot": 100}, date(2026, 8, 15))
    budget.set_budgets(con, {"proxy": 600}, TODAY)
    assert budget.budgets(con, "2026-08") == {"proxy": 500, "bot": 100}
    assert budget.budgets(con, "2026-09") == {"proxy": 500, "bot": 100}
    assert budget.budgets(con, "2026-10") == {"proxy": 600, "bot": 100}
    budget.set_budgets(con, {"bot": None}, TODAY)  # cleared: no budget from now on
    assert budget.budgets(con, "2026-10") == {"proxy": 600}
    assert budget.budgets(con, "2026-07") == {}


def test_current_month_counts_renewals_still_due(con):
    budget.set_budgets(con, {"proxy": 600, "bot": 100, "account": 300}, TODAY)
    recurring.track(con, "Kodai", "monthly", category="bot")  # paid Sep 24, renews Oct 24
    m = budget.month_view(con, "2026-10", TODAY)
    rows = {r["category"]: r for r in m["rows"]}
    assert rows["bot"]["spent"] == 0 and rows["bot"]["to_renew"] == 75 and rows["bot"]["state"] == "ok"
    assert rows["proxy"]["state"] == "ok" and rows["account"]["state"] == "near"
    assert (m["budget"], m["spent"], m["to_renew"], m["projected"]) == (1000, 816, 75, 891)
    assert m["left"] == 109 and m["current"]
    past = budget.month_view(con, "2026-09", TODAY)
    assert not past["current"] and past["to_renew"] == 0 and past["spent"] == 175


def test_history_and_chart_scale(con):
    budget.set_budgets(con, {"proxy": 500}, date(2026, 8, 1))
    h = budget.history(con, 3, TODAY)
    assert [(r["label"], r["spent"], r["budget"]) for r in h["rows"]] == [
        ("Aug", 900, 500), ("Sep", 175, 500), ("Oct", 816, 500)]
    assert h["rows"][0]["diff"] == 400 and h["rows"][0]["color"] == "#ff6b7a"
    assert h["grid"][-1]["v"] == 1000
    assert abs(h["rows"][0]["h"] - (h["grid"][0]["y"] - h["grid"][-1]["y"]) * .9) < .5


def test_months_shift_across_years():
    assert budget.shift("2026-01", -1) == "2025-12" and budget.shift("2026-12", 1) == "2027-01"


def test_budget_page_save_and_export(tmp_path, con):
    path = str(tmp_path / "b.db")
    client = create_app(path).test_client()
    client.post("/expenses/budget", data={"b_proxy": "$600", "b_bot": "", "b_account": "300"})
    c = db.connect(path)
    month = budget.month_of(date.today())
    assert budget.budgets(c, month) == {"proxy": 600, "account": 300}
    page = client.get("/expenses/budget").get_data(as_text=True)
    assert "Edit budgets" in page and "Projected month end" in page
    page = client.get("/expenses/budget?month=2026-08").get_data(as_text=True)
    assert "August 2026" in page and "Over budget" not in page
    csv = client.get("/expenses/export.csv?month=2026-10").get_data(as_text=True)
    assert csv.splitlines()[0].startswith("Date,Vendor") and "Gmail Farm" in csv and "Old" not in csv
    assert "of your" in client.get("/expenses").get_data(as_text=True)  # the budget meter on Overview
