import unittest
from email.message import EmailMessage

from jigstream.config import Config
from jigstream.extract import botting_hint, find_tracking, normalize
from jigstream.llm import parse_json
from jigstream.mail import Email, html_to_text, parse_message
from jigstream.orderstore import Store
from jigstream.sync import run_sync
from jigstream import db
from jigstream.app import create_app


class FakeLLM:
    def __init__(self, replies):
        self.replies = replies  # subject -> dict

    def chat_json(self, system, user):
        subject = user.split("\n")[1].removeprefix("Subject: ")
        return self.replies[subject]


def mail(subject, sender="Nike <orders@nike.com>", date="2026-10-01T10:00:00+00:00", body="", mid=None):
    return Email(mid or f"<{subject}@x>", sender, subject, date, body)


class ExtractTests(unittest.TestCase):
    def test_tracking_formats(self):
        self.assertEqual(find_tracking("Track: 1Z999AA10123456784"), ("1Z999AA10123456784", "UPS"))
        self.assertEqual(find_tracking("USPS 9400 1000 0000 0000 0000 00"), ("9400100000000000000000", "USPS"))
        self.assertEqual(find_tracking("FedEx tracking number: 123456789012"), ("123456789012", "FedEx"))
        self.assertEqual(find_tracking("order 123456789012"), (None, None))

    def test_botting_hint(self):
        self.assertEqual(botting_hint("Whop <no-reply@whop.com>", "Your receipt"), "cook_group")
        self.assertEqual(botting_hint("billing@iproyal.com", "Invoice"), "proxy")
        self.assertEqual(botting_hint("Cybersole", "Renewal"), "bot")
        self.assertIsNone(botting_hint("Nike <orders@nike.com>", "Your order"))

    def test_normalize_order_fills_tracking_from_body(self):
        e = mail("Shipped", body="Your UPS tracking 1Z999AA10123456784")
        r = normalize({"kind": "order", "retailer": "Nike", "order_number": "C001", "total": "$1,210.50",
                       "shipping_status": "bogus", "order_date": "10/01/2026"}, e)
        self.assertEqual(r["total"], 1210.5)
        self.assertEqual(r["tracking_number"], "1Z999AA10123456784")
        self.assertEqual(r["carrier"], "UPS")
        self.assertEqual(r["shipping_status"], "shipped")
        self.assertEqual(r["order_date"], "2026-10-01")

    def test_hint_overrides_order_kind(self):
        e = mail("Payment received", sender="Whop <no-reply@whop.com>")
        r = normalize({"kind": "order", "retailer": "Whop", "total": 45}, e)
        self.assertEqual(r["kind"], "botting_expense")
        self.assertEqual(r["category"], "cook_group")
        self.assertEqual(r["amount"], 45.0)

    def test_parse_json_with_fences(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('Sure! {"a": 2} hope that helps'), {"a": 2})


class MailTests(unittest.TestCase):
    def test_html_email(self):
        msg = EmailMessage()
        msg["From"] = "Foot Locker <x@footlocker.com>"
        msg["Subject"] = "Order confirmed"
        msg["Date"] = "Wed, 01 Oct 2026 10:00:00 -0400"
        msg["Message-ID"] = "<abc@fl>"
        msg.set_content("plain version")
        msg.add_alternative("<html><style>.x{}</style><body><p>Order #123</p><table><tr><td>Dunk</td>"
                            "<td>$120</td></tr></table></body></html>", subtype="html")
        e = parse_message(msg.as_bytes())
        self.assertEqual(e.message_id, "<abc@fl>")
        self.assertTrue(e.date.startswith("2026-10-01T10:00:00"))
        self.assertIn("Order #123", e.body)
        self.assertIn("Dunk | $120", e.body)
        self.assertNotIn(".x{}", e.body)

    def test_html_to_text_collapses_space(self):
        self.assertEqual(html_to_text("<div>a&nbsp;&nbsp;b</div><div></div><div>c</div>"), "a b\nc")


class SyncTests(unittest.TestCase):
    def test_lifecycle_merges_and_renders(self):
        store = Store(db.connect(":memory:"))
        emails = [
            mail("Order confirmed", date="2026-10-01T10:00:00+00:00"),
            mail("Your order shipped", date="2026-10-03T10:00:00+00:00"),
            mail("Delivered", date="2026-10-06T10:00:00+00:00"),
            mail("Whop receipt", sender="Whop <no-reply@whop.com>"),
            mail("Weekly deals"),
        ]
        llm = FakeLLM({
            "Order confirmed": {"kind": "order", "retailer": "Nike", "order_number": "C001", "total": 130,
                                "items": [{"name": "Dunk Low", "quantity": 1, "price": 120}],
                                "shipping_address": "1 Main St, Austin TX", "shipping_status": "ordered"},
            "Your order shipped": {"kind": "order", "retailer": "Nike.com", "order_number": "C001",
                                   "tracking_number": "1Z999AA10123456784", "shipping_status": "shipped"},
            "Delivered": {"kind": "order", "retailer": "Nike", "tracking_number": "1Z999AA10123456784",
                          "shipping_status": "delivered"},
            "Whop receipt": {"kind": "botting_expense", "vendor": "Whop", "expense_category": "cook_group",
                             "total": 50, "description": "Cook group monthly"},
            "Weekly deals": {"kind": "other"},
        })
        counts = run_sync(Config(), store, emails=emails, llm=llm, log=lambda *a, **k: None)
        self.assertEqual(counts, {"order": 3, "botting_expense": 1, "other": 1, "error": 0})

        orders = store.orders()
        self.assertEqual(len(orders), 1)
        o = orders[0]
        self.assertEqual(o["shipping_status"], "delivered")
        self.assertEqual(o["carrier"], "UPS")
        self.assertEqual(o["shipping_address"], "1 Main St, Austin TX")
        self.assertEqual(len(o["message_ids"]), 3)
        self.assertEqual(store.expenses()[0]["amount"], 50)

        # An older email arriving late must not roll the status back.
        late = mail("Shipped again", date="2026-10-02T10:00:00+00:00")
        llm.replies["Shipped again"] = {"kind": "order", "retailer": "Nike", "order_number": "C001",
                                        "shipping_status": "shipped"}
        run_sync(Config(), store, emails=[late] + emails, llm=llm, log=lambda *a, **k: None)
        self.assertEqual(store.orders()[0]["shipping_status"], "delivered")

        # A bot "success" whose order the retailer later cancelled shows up on the dashboard.
        cancel = mail("Order cancelled", date="2026-10-07T10:00:00+00:00")
        llm.replies["Order cancelled"] = {"kind": "order", "retailer": "Nike", "order_number": "C001",
                                          "shipping_status": "cancelled"}
        run_sync(Config(), store, emails=[cancel], llm=llm, log=lambda *a, **k: None)
        store.db.execute("INSERT INTO checkouts (fingerprint, status, site, order_number) VALUES ('f', 'success', 'nike', 'C001')")
        from jigstream import analysis
        self.assertEqual(analysis.cancelled_after_success(store.db)[0]["order_number"], "C001")

    def test_pages_render(self):
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "w.db")
        store = Store(db.connect(path))
        store.save(mail("x", date="2026-10-03T00:00:00"), {
            "kind": "order", "retailer": "Nike", "order_number": "C001", "order_date": "2026-10-01",
            "items": [{"name": "Dunk Low", "quantity": 2, "price": 120}], "total": 262.4, "currency": "USD",
            "shipping_address": "1 Main St", "tracking_number": "1Z999AA10123456784", "carrier": "UPS",
            "shipping_status": "in_transit", "estimated_delivery": "2026-10-07"})
        store.save(mail("y", sender="Whop"), {"kind": "botting_expense", "vendor": "Whop", "category": "cook_group",
                   "description": "Cook group monthly", "amount": 50.0, "currency": "USD", "date": "2026-10-01",
                   "order_number": None})
        client = create_app(path).test_client()
        page = client.get("/orders").get_data(as_text=True)
        self.assertIn("Dunk Low x2", page)
        self.assertIn("ups.com/track?tracknum=1Z999AA10123456784", page)
        self.assertIn("$262.40", page)
        self.assertIn("Cook group monthly", client.get("/expenses").get_data(as_text=True))
        self.assertEqual(client.get("/api/expenses").get_json()[0]["amount"], 50.0)

    def test_errors_are_retried(self):
        from jigstream.llm import LLMError

        class Broken:
            def chat_json(self, s, u):
                raise LLMError("Model did not return JSON")

        store = Store(db.connect(":memory:"))
        counts = run_sync(Config(), store, emails=[mail("X")], llm=Broken(), log=lambda *a, **k: None)
        self.assertEqual(counts["error"], 1)
        self.assertEqual(store.seen_ids(), set())


if __name__ == "__main__":
    unittest.main()
