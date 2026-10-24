import unittest
from email.message import EmailMessage

from jigstream.config import Config
from jigstream.config import parse_accounts
from jigstream.extract import botting_signals, find_tracking, find_tracking_url, looks_like_purchase, normalize
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


def mail(subject, sender="Nike <orders@nike.com>", date="2026-10-01T10:00:00+00:00",
         body="Your order total: $130.00", mid=None, account="me@gmail.com"):
    return Email(mid or f"<{subject}@x>", sender, subject, date, body, account)


class ExtractTests(unittest.TestCase):
    def test_tracking_formats(self):
        self.assertEqual(find_tracking("Track: 1Z999AA10123456784"), ("1Z999AA10123456784", "UPS"))
        self.assertEqual(find_tracking("USPS 9400 1000 0000 0000 0000 00"), ("9400100000000000000000", "USPS"))
        self.assertEqual(find_tracking("FedEx tracking number: 123456789012"), ("123456789012", "FedEx"))
        self.assertEqual(find_tracking("order 123456789012"), (None, None))

    def test_purchase_prepass_and_botting_signals(self):
        self.assertTrue(looks_like_purchase(mail("Receipt", body="Payment of $45.00 received")))
        self.assertTrue(looks_like_purchase(mail("Shipped", body="Tracking 1Z999AA10123456784")))
        self.assertFalse(looks_like_purchase(mail("Weekly newsletter", body="Big news this week")))
        self.assertFalse(looks_like_purchase(mail("Hello", body="see you at 5.00 tomorrow")))
        e = mail("Your receipt from Stripe", body="Residential proxies 10GB plan, $45.00, renewal monthly")
        self.assertIn("residential", botting_signals(e))

    def test_tracking_link_from_email(self):
        body = ("View order [https://click.shop.com/track?u=1] Track package "
                "[https://www.ups.com/track?loc=en_US&tracknum=1Z999AA10123456784]")
        self.assertEqual(find_tracking_url(body, "1Z999AA10123456784"),
                         "https://www.ups.com/track?loc=en_US&tracknum=1Z999AA10123456784")
        self.assertEqual(find_tracking_url("[https://click.shop.com/track?u=1]", "1Z999AA10123456784"), None)
        self.assertEqual(find_tracking_url("[https://narvar.com/tracking/shop/x]", "TBA123456789012"),
                         "https://narvar.com/tracking/shop/x")

    def test_parse_accounts(self):
        self.assertEqual(parse_accounts("a@gmail.com:abcd efgh, b@gmail.com:wxyz ,bad"),
                         [("a@gmail.com", "abcdefgh"), ("b@gmail.com", "wxyz")])

    def test_normalize_order_fills_tracking_from_body(self):
        e = mail("Shipped", body="Your UPS tracking 1Z999AA10123456784")
        r = normalize({"kind": "order", "retailer": "Nike", "order_number": "C001", "total": "$1,210.50",
                       "shipping_status": "bogus", "order_date": "10/01/2026"}, e)
        self.assertEqual(r["total"], 1210.5)
        self.assertEqual(r["tracking_number"], "1Z999AA10123456784")
        self.assertEqual(r["carrier"], "UPS")
        self.assertEqual(r["shipping_status"], "shipped")
        self.assertEqual(r["order_date"], "2026-10-01")

    def test_botting_expense_from_unknown_vendor(self):
        e = mail("Your receipt", sender="Stripe <receipts@stripe.com>")
        r = normalize({"kind": "botting_expense", "vendor": "Brand New Proxies", "expense_category": "proxy",
                       "billing_period": "monthly", "total": "$45.00"}, e)
        self.assertEqual((r["vendor"], r["category"], r["billing_period"], r["amount"]),
                         ("Brand New Proxies", "proxy", "monthly", 45.0))
        bad = normalize({"kind": "botting_expense", "expense_category": "weird", "billing_period": "daily"}, e)
        self.assertEqual((bad["category"], bad["billing_period"], bad["vendor"]), ("other", None, "Stripe"))

    def test_parse_json_with_fences(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('Sure! {"a": 2} hope that helps'), {"a": 2})
        self.assertEqual(parse_json('<think>maybe {x}?</think>\n{"a": 3}'), {"a": 3})


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
            mail("Weekly deals", body="Order now and save, everything $99.00"),
            mail("Account notice", body="We updated our privacy policy"),
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
        self.assertEqual(counts, {"order": 3, "botting_expense": 1, "other": 1, "skipped": 1, "error": 0,
                                  "failed_inboxes": []})

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


class StatusTests(unittest.TestCase):
    def order(self, store, mid, date, status, tracking=None, url=None):
        store.save(mail(mid, date=date), {
            "kind": "order", "retailer": "Nike", "order_number": "C9", "order_date": None, "items": [],
            "total": None, "currency": "USD", "shipping_address": None, "tracking_number": tracking,
            "tracking_url": url, "carrier": "UPS" if tracking else None, "shipping_status": status,
            "estimated_delivery": None})

    def test_newer_email_cannot_undo_delivered_and_email_link_is_used(self):
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "s.db")
        store = Store(db.connect(path))
        link = "https://www.ups.com/track?loc=en_US&tracknum=1Z999AA10123456784&src=email"
        self.order(store, "a", "2026-10-01T00:00:00", "shipped", "1Z999AA10123456784", link)
        self.order(store, "b", "2026-10-04T00:00:00", "delivered")
        self.order(store, "c", "2026-10-05T00:00:00", "shipped")  # e.g. a re-sent shipping notice
        self.assertEqual(store.orders()[0]["shipping_status"], "delivered")
        page = create_app(path).test_client().get("/orders").get_data(as_text=True)
        self.assertIn("src=email", page)


class InboxTests(unittest.TestCase):
    def setUp(self):
        import jigstream.sync as sync
        self.sync = sync
        self.calls = []
        self.real_fetch = sync.fetch_emails
        boxes = {
            "a@gmail.com": [mail("A order", account="a@gmail.com")],
            "b@gmail.com": [mail("B receipt", sender="Whop <no-reply@whop.com>", account="b@gmail.com")],
        }

        def fake_fetch(config, address, password, days, skip_ids, on_total):
            self.calls.append((address, days))
            if address == "bad@gmail.com":
                raise RuntimeError("Gmail login failed for bad@gmail.com")
            new = [e for e in boxes[address] if e.message_id not in skip_ids]
            on_total(len(new))
            yield from new

        sync.fetch_emails = fake_fetch
        self.llm = FakeLLM({
            "A order": {"kind": "order", "retailer": "Target", "order_number": "9", "total": 130},
            "B receipt": {"kind": "botting_expense", "vendor": "Some Cook Group", "expense_category": "cook_group",
                          "total": 30},
        })

    def tearDown(self):
        self.sync.fetch_emails = self.real_fetch

    def test_inboxes_in_succession_with_incremental_ranges(self):
        store = Store(db.connect(":memory:"))
        config = Config(accounts=[("a@gmail.com", "x"), ("bad@gmail.com", "x"), ("b@gmail.com", "x")])
        counts = self.sync.run_sync(config, store, llm=self.llm, log=lambda *a, **k: None)
        self.assertEqual([c[0] for c in self.calls], ["a@gmail.com", "bad@gmail.com", "b@gmail.com"])
        self.assertTrue(all(days == 365 for _, days in self.calls))  # first sync: one year
        self.assertEqual(counts["failed_inboxes"], ["bad@gmail.com"])
        self.assertEqual(store.orders()[0]["account"], "a@gmail.com")
        self.assertEqual(store.expenses()[0]["account"], "b@gmail.com")
        self.assertIsNone(store.account_state("bad@gmail.com"))

        self.calls.clear()
        self.sync.run_sync(config, store, llm=self.llm, log=lambda *a, **k: None)
        self.assertEqual(dict(self.calls)["a@gmail.com"], 60)  # later syncs: the last two months, seen mail skipped
        self.assertEqual(dict(self.calls)["bad@gmail.com"], 365)  # never synced, still a full year

        self.calls.clear()
        config.full_history = True
        self.sync.run_sync(config, store, llm=self.llm, log=lambda *a, **k: None)
        self.assertIsNone(dict(self.calls)["a@gmail.com"])  # whole mailbox
        self.assertTrue(store.account_state("a@gmail.com")["backfilled_at"])

    def test_stop_keeps_what_was_read_and_the_next_sync_carries_on(self):
        store = Store(db.connect(":memory:"))
        config = Config(accounts=[("a@gmail.com", "x"), ("b@gmail.com", "x")])
        stopping = {"now": False}

        def stop():
            return stopping["now"]

        real_process = self.sync.process

        def process_then_stop(*a, **k):  # Stop pressed while the first email is being read
            real_process(*a, **k)
            stopping["now"] = True

        self.sync.process = process_then_stop
        try:
            counts = self.sync.run_sync(config, store, llm=self.llm, log=lambda *a, **k: None, stop=stop)
        finally:
            self.sync.process = real_process
        self.assertTrue(counts["stopped"])
        self.assertEqual([c[0] for c in self.calls], ["a@gmail.com"])  # b's inbox wasn't started
        self.assertEqual(len(store.orders()), 1)  # the email read before stopping is kept
        self.assertIsNone(store.account_state("a@gmail.com"))  # not marked synced, so its range is redone

        self.calls.clear()
        counts = self.sync.run_sync(config, store, llm=self.llm, log=lambda *a, **k: None)
        self.assertNotIn("stopped", counts)
        self.assertEqual([c[0] for c in self.calls], ["a@gmail.com", "b@gmail.com"])
        self.assertEqual((len(store.orders()), len(store.expenses())), (1, 1))  # nothing read twice



def test_spend_counts_only_orders_that_stuck():
    store = Store(db.connect(":memory:"))
    orders = [{"total": 100, "shipping_status": "delivered"}, {"total": 250, "shipping_status": "cancelled"},
              {"total": 40, "shipping_status": "refunded"}, {"total": 60, "shipping_status": None},
              {"total": 999, "shipping_status": "not_an_order"}]
    st = store.stats(orders=orders, expenses=[])
    assert st["order_total"] == 160 and st["not_spent"] == 3 and st["order_count"] == 5

if __name__ == "__main__":
    unittest.main()


def test_ad_senders_skip_the_model_after_three_ads():
    store = Store(db.connect(":memory:"))
    asked = []

    class LLM:
        def chat_json(self, system, user):
            asked.append(user.split("\n")[1])
            return {"kind": "other"}
    ads = [mail(f"Deals week {i} - save $20 on your order", sender="Shop <news@shop.com>", mid=f"<ad{i}@x>") for i in range(3)]
    run_sync(Config(), store, emails=ads, llm=LLM(), log=lambda *a, **k: None)
    assert len(asked) == 3
    later = [mail("Flash deals inside, $10 off", sender="Shop <News@shop.com>", mid="<ad9@x>"),
             mail("Your order has shipped", sender="Shop <news@shop.com>", mid="<ship@x>")]
    counts = run_sync(Config(), store, emails=later, llm=LLM(), log=lambda *a, **k: None)
    assert asked[-1] == "Subject: Your order has shipped" and len(asked) == 4 and counts["skipped"] == 1


def test_long_emails_keep_their_start_and_end():
    from jigstream.extract import trim
    text = "Order #123 items\n" + "x" * 20000 + "\nTotal $99.00"
    out = trim(text, 8000)
    assert len(out) <= 8000 and out.startswith("Order #123") and out.endswith("Total $99.00")
