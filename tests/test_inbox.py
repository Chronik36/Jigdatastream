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
    monkeypatch.setattr(inbox, "search", lambda config, q, limit=50, after=None: ([dict(ROW, spam=False)], [], None))
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


def test_ai_skipped_filter_lists_skipped_emails_not_yet_labelled(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@gmail.com:pw")
    monkeypatch.setattr(inbox, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Gmail search")))
    path = str(tmp_path / "s.db")
    store = Store(db.connect(path))
    for mid, kind, subject in (("<a@x>", "other", "Your Nike receipt"), ("<b@x>", "skipped", "Newsletter"),
                               ("<c@x>", "order", "Shipped"), ("<d@x>", "other", "Already fixed")):
        store.db.execute("INSERT INTO emails (message_id, account, sender, subject, date, kind) VALUES (?,?,?,?,?,?)",
                         (mid, "me@gmail.com", "S <s@x.com>", subject, "2026-10-2" + mid[1], kind))
    store.db.commit()
    store.set_inbox_label({"account": "me@gmail.com", "message_id": "<d@x>", "sender": "", "subject": "", "date": ""},
                          "ordered", "saved")
    html = appmod.create_app(path).test_client().get("/inbox/rows?f=AI+skipped").get_data(as_text=True)
    assert "Your Nike receipt" in html and "Newsletter" in html and "AI: not relevant" in html
    assert "Shipped" not in html and "Already fixed" not in html


def test_find_total_reads_stripe_style_table_receipts():
    from jigstream.extract import find_total
    from jigstream.mail import html_to_text
    html = ("<table><tr><td>&nbsp;</td><td><table><tr><td>Receipt from STELLA CLOUD LLC</td></tr><tr><td>$70.00</td></tr>"
            "<tr><td>Paid September 29, 2026</td></tr></table></td></tr><tr><td><table><tr><td>EPYC Server</td><td>Qty 1</td>"
            "<td>$70.00</td></tr><tr><td>Amount paid</td><td>&nbsp;</td></tr><tr><td>&nbsp;</td></tr><tr><td>&nbsp;</td></tr>"
            "<tr><td>$70.00</td></tr></table></td></tr><tr><td>Get $5 off your next order</td></tr></table>")
    assert find_total(html_to_text(html)) == 70.0
    assert find_total("Payment amount: 25 USD") == 25.0
    assert find_total("Total: 110,00 €") == 110.0
    assert find_total("Plan $70.00\nTotal $70.00\nRefer a friend for $5 off") == 70.0


def test_fill_missing_amounts_retries_when_email_cant_be_opened(tmp_path, monkeypatch):
    from jigstream import db
    from jigstream.orderstore import Store
    store = Store(db.connect(tmp_path / "t.db"))
    store.db.execute("INSERT INTO expenses (message_id, vendor, account) VALUES ('<a@x>', 'Alpine', 'me@x')")
    store.db.commit()

    def down(*a):
        raise OSError("offline")
    monkeypatch.setattr(inbox, "open_email", down)
    assert inbox.fill_missing_amounts(object(), store) == (0, 0)
    monkeypatch.setattr(inbox, "open_email", lambda *a: Email("<a@x>", "s", "Receipt", "", "Total\n | \n | \n$30.00"))
    assert inbox.fill_missing_amounts(object(), store) == (1, 0)
    assert store.expenses()[0]["amount"] == 30.0


def test_search_pages_merge_inboxes_without_gaps_or_repeats(monkeypatch):
    # Two inboxes of 7 and 4 emails, interleaved by date; read 3 at a time.
    boxes = {"a@x": [{"message_id": f"<a{u}>", "date": f"2026-10-{u * 2:02d}", "uid": u, "f": 0} for u in range(7, 0, -1)],
             "b@x": [{"message_id": f"<b{u}>", "date": f"2026-10-{u * 2 + 1:02d}", "uid": u, "f": 0} for u in range(4, 0, -1)]}

    def fake(config, address, password, query, limit, after=None):
        rows = [r for r in boxes[address] if not after or r["uid"] < after["u"]]
        return [dict(r, account=address) for r in rows[:limit]]
    monkeypatch.setattr(inbox, "search_headers", fake)
    config = type("C", (), {"accounts": [("a@x", "pw"), ("b@x", "pw")]})()
    seen, after, pages = [], None, 0
    while True:
        rows, errors, after = inbox.search(config, "", limit=3, after=after)
        seen += [r["message_id"] for r in rows]
        pages += 1
        if after is None:
            break
        after = json.loads(json.dumps(after))  # it travels through the page as JSON
    assert errors == [] and pages == 4
    everything = sorted(boxes["a@x"] + boxes["b@x"], key=lambda r: r["date"], reverse=True)
    assert seen == [r["message_id"] for r in everything]


def test_rows_route_passes_cursor_and_skipped_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@gmail.com:pw")
    calls = []

    def fake(config, q, limit=50, after=None):
        calls.append(after)
        return ([dict(ROW, spam=False)], [], {"me@gmail.com": {"f": 0, "u": 5}}) if after is None else ([], [], None)
    monkeypatch.setattr(inbox, "search", fake)
    path = str(tmp_path / "p.db")
    store = Store(db.connect(path))
    for n in range(60):
        store.db.execute("INSERT INTO emails (message_id, account, sender, subject, date, kind) VALUES (?,?,?,?,?,?)",
                         (f"<n{n}>", "me@gmail.com", "S", f"Skipped {n:02d}", f"2026-09-{n % 28 + 1:02d}T{n:02d}", "other"))
    store.db.commit()
    c = appmod.create_app(path).test_client()
    html = c.get("/inbox/rows").get_data(as_text=True)
    assert "Your order was cancelled" in html and 'class="mrow more"' in html
    nxt = html.split('data-next="')[1].split('"')[0].replace("&#34;", '"')
    assert c.get("/inbox/rows?after=" + nxt).status_code == 200 and calls[1] == {"me@gmail.com": {"f": 0, "u": 5}}
    first = c.get("/inbox/rows?f=AI+skipped").get_data(as_text=True)
    assert first.count('data-id=') == 50 and 'data-next="50"' in first
    rest = c.get("/inbox/rows?f=AI+skipped&after=50").get_data(as_text=True)
    assert rest.count('data-id=') == 10 and "mrow more" not in rest and "That's everything" in rest
