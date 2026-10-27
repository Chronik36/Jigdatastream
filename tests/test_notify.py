import json
from datetime import datetime, timezone

from jigstream import db, notify
from jigstream.orderstore import Store

from test_orders import mail as email


def result(status, **extra):
    return dict({"kind": "order", "retailer": "Target", "order_number": "T1", "order_date": None,
                 "items": [{"name": "Pokemon ETB", "quantity": 1}], "total": 54.99, "currency": "USD",
                 "shipping_address": "1 Elm St", "tracking_number": "1Z999AA10123456784", "tracking_url": None,
                 "carrier": "UPS", "shipping_status": status, "estimated_delivery": "2026-10-30"}, **extra)


def test_shipping_email_alerts_the_phone(monkeypatch):
    monkeypatch.setenv("PHONE_ACCESS", "1")
    monkeypatch.setenv("PHONE_PAIR_TOPIC", "bottrack-abc")
    sent = []
    monkeypatch.setattr(notify.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda s: target()})())
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout: sent.append(json.loads(req.data)) or
                        type("R", (), {"close": lambda s: None})())
    now = datetime.now(timezone.utc).isoformat()
    store = Store(db.connect(":memory:"))
    store.save(email("Order placed", date=now, mid="<1@x>"), result("ordered"))
    assert sent == []  # a new order isn't news
    store.save(email("Shipped", date=now, mid="<2@x>"), result("shipped"))
    (alert,) = sent
    assert alert["topic"] == "bottrack-abc-alerts" and alert["title"] == "Shipped: Target"
    assert alert["message"] == "Pokemon ETB · arrives 2026-10-30" and alert["tags"][1].startswith("order-")
    assert "1Z999" not in json.dumps(alert) and "Elm" not in json.dumps(alert)  # nothing personal goes out
    store.save(email("Delivered", date="2025-01-01T10:00:00+00:00", mid="<3@x>"), result("delivered"))
    assert len(sent) == 1  # old mail read by a backfill doesn't buzz the phone


def test_no_alerts_without_phone_access(monkeypatch):
    monkeypatch.delenv("PHONE_ACCESS", raising=False)
    monkeypatch.setenv("PHONE_PAIR_TOPIC", "bottrack-abc")
    assert not notify.order_moved({"id": 1, "retailer": "Target"}, "ordered", "delivered",
                                  datetime.now(timezone.utc).isoformat(), post=lambda *a, **k: None)
