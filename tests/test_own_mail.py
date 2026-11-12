import json
import logging

from jigstream import db, sync
from jigstream.config import Config
from jigstream.mail import bottrack_own
from jigstream.orderstore import Store, _forget_own_mail
from tests.test_orders import FakeLLM, mail

LOG_BODY = ("What went wrong: add a category\nBotTrack: build-163\n"
            "order in 3.0s: Kollect Korner | Order #35661 confirmed -> {'retailer': 'Kollect Korner', "
            "'order_number': '35661', 'total': 38.6, 'shipping_address': '1 Main St, La Puente, CA'}\n"
            "Your order total: $38.60")


def test_bottrack_own_subjects():
    assert bottrack_own("BotTrack logs (build-163-b255e5b)")
    assert bottrack_own("Fwd: BotTrack logs (build-163)")
    assert bottrack_own("BotTrack access request: friend@gmail.com")
    assert bottrack_own("Your BotTrack key")
    assert not bottrack_own("Your Kollect Korner order #35661 is confirmed")


def test_a_friends_logs_email_is_never_read_as_an_order():
    store = Store(db.connect(":memory:"))
    counts = {"skipped": 0}
    # The model is never asked (an empty FakeLLM would fail if it were).
    sync.process(mail("BotTrack logs (build-163-b255e5b)", sender="friend@gmail.com", body=LOG_BODY),
                 store, FakeLLM({}), Config(accounts=[]), counts, lambda *a, **k: None)
    assert counts["skipped"] == 1
    assert store.orders() == []


def test_log_line_leaves_out_names_and_addresses(caplog):
    store = Store(db.connect(":memory:"))
    llm = FakeLLM({"A order": {"kind": "order", "retailer": "Target", "order_number": "9", "total": 130,
                               "shipping_address": "1 Main St, La Puente, CA", "shipping_name": "Pat Doe"}})
    with caplog.at_level(logging.INFO, logger="jigstream.sync"):
        sync.process(mail("A order"), store, llm, Config(accounts=[]), {"skipped": 0}, lambda *a, **k: None)
    assert "Target" in caplog.text and "Main St" not in caplog.text and "Pat Doe" not in caplog.text


def test_orders_already_read_from_bottrack_mail_are_taken_back():
    con = db.connect(":memory:")
    Store(con)
    con.executemany("INSERT INTO emails (message_id, subject, kind) VALUES (?, ?, 'order')",
                    [("<log@x>", "BotTrack logs (build-163)"), ("<real@x>", "Your order is confirmed")])
    con.execute("INSERT INTO tracked_orders (retailer, order_number, message_ids) VALUES ('Kollect Korner', '35661', ?)",
                (json.dumps(["<log@x>"]),))
    con.execute("INSERT INTO tracked_orders (retailer, order_number, message_ids) VALUES ('Target', '9', ?)",
                (json.dumps(["<real@x>", "<log@x>"]),))
    con.execute("INSERT INTO expenses (message_id, vendor, amount) VALUES ('<log@x>', 'Kollect Korner', 38.6)")
    con.execute("DELETE FROM store_fixes WHERE name = 'forget_own_mail'")
    _forget_own_mail(con)
    rows = con.execute("SELECT retailer, message_ids FROM tracked_orders").fetchall()
    assert [(r[0], json.loads(r[1])) for r in rows] == [("Target", ["<real@x>"])]
    assert con.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0
    assert con.execute("SELECT kind FROM emails WHERE message_id = '<log@x>'").fetchone()[0] == "skipped"
