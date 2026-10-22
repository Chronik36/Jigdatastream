import json
import os

import jigstream.app as appmod
from jigstream import db, training
from jigstream.config import Config
from jigstream.mail import Email
from jigstream.orderstore import Store


def order(store, status="shipped", number="C777", tracking="1Z999AA10123456784"):
    store.save(Email("<orig>", "Nike <n@nike.com>", "Order confirmed", "2026-10-01T00:00:00", "", "me@gmail.com"), {
        "kind": "order", "retailer": "Nike", "order_number": number, "order_date": "2026-10-01", "items": [],
        "total": 100.0, "currency": "USD", "shipping_address": None, "tracking_number": tracking,
        "carrier": "UPS", "shipping_status": status, "estimated_delivery": None})
    return store.orders()[0]


class LLM:
    def __init__(self, status):
        self.status = status
        self.examples_seen = []

    def chat_json(self, system, user, examples=()):
        self.examples_seen.append(list(examples))
        return {"kind": "order", "retailer": "Nike", "order_number": "C777", "shipping_status": self.status}


def test_manual_status_sticks_and_records_label():
    store = Store(db.connect(":memory:"))
    o = order(store)
    label = store.set_status_manually(o["id"], "cancelled")
    assert store.order(o["id"])["shipping_status"] == "cancelled" and store.order(o["id"])["status_manual"] == 1
    # A later "delivered" email doesn't override what you set.
    store.save(Email("<late>", "Nike", "Delivered", "2026-10-09T00:00:00", "", ""), {
        "kind": "order", "retailer": "Nike", "order_number": "C777", "order_date": None, "items": [],
        "total": None, "currency": "USD", "shipping_address": None, "tracking_number": None, "carrier": None,
        "shipping_status": "delivered", "estimated_delivery": None})
    assert store.order(o["id"])["shipping_status"] == "cancelled"
    assert store.labels()[o["id"]]["id"] == label and store.labels()[o["id"]]["previous_status"] == "shipped"


def test_rescan_finds_emails_and_saves_examples(monkeypatch):
    store = Store(db.connect(":memory:"))
    o = order(store)
    label = store.set_status_manually(o["id"], "cancelled")
    queries = []

    def fake_fetch(config, address, password, days=None, skip_ids=(), on_total=None):
        queries.append((address, config.search_query, days))
        yield Email("<c1>", "Nike", "Your order C777 has been cancelled", "2026-10-03T00:00:00",
                    "We cancelled order C777.", address)
        yield Email("<s1>", "Nike", "Your order shipped", "2026-10-02T00:00:00", "Tracking 1Z999AA10123456784", address)

    monkeypatch.setattr(training, "fetch_emails", fake_fetch)
    config = Config(accounts=[("me@gmail.com", "x"), ("two@gmail.com", "y")])
    text = training.rescan(config, store, LLM("shipped"), label, o)
    assert queries[0] == ("me@gmail.com", '("C777" OR "1Z999AA10123456784") -in:trash', None)
    assert len(queries) == 2 and "2 about \"cancelled\"" in text  # one cancellation email per inbox
    used = store.examples()
    assert {e["message_id"] for e in used} == {"<c1>"}
    assert json.loads(used[0]["corrected"])["shipping_status"] == "cancelled" and used[0]["model_was_right"] == 0
    assert len(store.examples(only_corrected=False)) == 2  # the shipped email is kept, unused

    # The correction now goes into prompts, and exports as fine-tuning data.
    shots = training.few_shot(store)
    assert len(shots) == 1 and "cancelled" in shots[0][0] and '"cancelled"' in shots[0][1]
    line = json.loads(training.export_jsonl(store).splitlines()[0])
    assert [m["role"] for m in line["messages"]] == ["system", "user", "assistant"]


def test_orders_page_dropdown_only_in_training_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items()
                                        if not k.startswith(("GMAIL", "TRAINING", "USE_TRAINING"))})
    path = str(tmp_path / "t.db")
    o = order(Store(db.connect(path)))
    app = appmod.create_app(path)
    c = app.test_client()
    assert "correct status" not in c.get("/orders").get_data(as_text=True)
    c.post("/setup/training", data={"training": "on", "use_examples": "on"})
    page = c.get("/orders").get_data(as_text=True)
    assert "correct status" in page and 'href="/training"' in page

    started = []
    monkeypatch.setattr(appmod.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: started.append(args)})())
    c.post(f"/orders/{o['id']}/label", data={"status": "delivered"})
    assert Store(db.connect(path)).order(o["id"])["shipping_status"] == "delivered"
    assert started and started[0][1]["order_number"] == "C777"
    assert "Searching..." in c.get("/orders").get_data(as_text=True)
    assert c.get("/training").status_code == 200
