import json

from jigstream import app as appmod, db, inbox
from jigstream.mail import Email
from jigstream.orderstore import Store

ROW = {"account": "me@gmail.com", "message_id": "<c1@nike>", "sender": "Nike <n@nike.com>",
       "subject": "Your order was cancelled", "date": "2026-10-20T10:00:00+00:00"}
MAIL = Email("<c1@nike>", "Nike <n@nike.com>", "Your order was cancelled", "2026-10-20T10:00:00+00:00",
             "Order C123 has been cancelled.", "me@gmail.com")


class FakeLLM:
    def chat_json(self, system, user, examples=()):
        return {"kind": "order", "retailer": "Nike", "order_number": "C123", "shipping_status": "ordered"}


def test_label_becomes_training_example_and_applies(tmp_path, monkeypatch):
    store = Store(db.connect(str(tmp_path / "i.db")))
    monkeypatch.setattr(inbox, "open_email", lambda config, account, mid: MAIL)
    config = type("C", (), {"max_body_chars": 5000, "accounts": [("me@gmail.com", "pw")]})()
    inbox.label_emails(config, store, FakeLLM(), [ROW], "cancelled")
    ex = store.examples()[0]
    assert ex["status"] == "cancelled" and json.loads(ex["corrected"])["shipping_status"] == "cancelled"
    assert ex["model_was_right"] == 0 and ex["model_status"] == "ordered"
    assert store.orders()[0]["shipping_status"] == "cancelled"
    assert store.inbox_labels()["<c1@nike>"]["state"].startswith("saved")
    # relabelling replaces the example instead of adding another
    inbox.label_emails(config, store, FakeLLM(), [ROW], "other")
    assert [json.loads(e["corrected"]) for e in store.examples()] == [{"kind": "other"}]


def test_expense_label(tmp_path, monkeypatch):
    store = Store(db.connect(str(tmp_path / "e.db")))
    monkeypatch.setattr(inbox, "open_email", lambda *a: MAIL)
    config = type("C", (), {"max_body_chars": 5000, "accounts": []})()
    inbox.label_emails(config, store, FakeLLM(), [ROW], "expense")
    assert store.expenses()[0]["message_id"] == "<c1@nike>"


def test_inbox_page_and_label_route(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@gmail.com:pw")
    monkeypatch.setattr(inbox, "search", lambda config, q, limit=50: ([dict(ROW, spam=False)], []))
    monkeypatch.setattr(inbox, "open_email", lambda *a: MAIL)
    started = []
    monkeypatch.setattr(appmod.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: started.append(args)})())
    c = appmod.create_app(str(tmp_path / "w.db")).test_client()
    page = c.get("/inbox").get_data(as_text=True)
    assert "Label and train" in page and "Loading your inboxes" in page  # the list loads after the page
    assert "Your order was cancelled" in c.get("/inbox/rows").get_data(as_text=True)
    assert c.get("/inbox/view?account=me@gmail.com&id=<c1@nike>").get_json()["body"].startswith("Order C123")
    r = c.post("/inbox/label", data={"label": "cancelled", "pick": json.dumps(ROW)})
    assert r.status_code == 302 and started and started[0][4] == "cancelled"


def test_fill_missing_amounts_reads_totals_and_drops_reminders(tmp_path, monkeypatch):
    store = Store(db.connect(str(tmp_path / "f.db")))
    for mid, subject in (("<p@x>", "Your receipt"), ("<r@x>", "Invoice Payment Reminder")):
        store.db.execute("INSERT INTO emails (message_id, account, subject) VALUES (?, 'me@gmail.com', ?)", (mid, subject))
        store.db.execute("INSERT INTO expenses (message_id, vendor, account) VALUES (?, 'Alpine', 'me@gmail.com')", (mid,))
    store.db.commit()
    paid = Email("<p@x>", "Alpine <a@x.com>", "Your receipt", "2026-10-18", "Plan | $40.00\nTotal | $43.20", "me@gmail.com")
    monkeypatch.setattr(inbox, "open_email", lambda *a: paid)
    assert inbox.fill_missing_amounts(object(), store) == (1, 1)
    assert [(e["message_id"], e["amount"]) for e in store.expenses()] == [("<p@x>", 43.2)]
