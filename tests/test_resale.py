import json
import unittest

from jigstream import db, resale
from jigstream.config import Config
from jigstream.extract import extract, purchase_proof
from jigstream.mail import Email
from jigstream.orderstore import Store
from jigstream.sync import clean_ad_orders, find_resale, run_sync

WORDS = resale.DEFAULT_WORDS

SAMS = """Sam's Club
Thank you for shopping with us today
Club #4821  Register 7
GV WHOLE MILK             3.48
BANANAS 3LB               1.42
POKEMON TCG 151 BOOSTER BUNDLE   26.98
PAPER TOWELS 12CT         21.98
SUBTOTAL                  53.86
TAX                        2.10
TOTAL                     55.96
VISA ************4421     55.96
"""
PROMO = """Third Eye Comics - New this week!
Pokemon Scarlet & Violet Prismatic Evolutions Booster Box $159.99
One Piece OP-09 Booster Box $109.99
Shop now. Pre-order today, quantities limited.
Unsubscribe"""


def mail(subject, body, sender="Sam's Club <receipts@samsclub.com>", mid=None):
    return Email(mid or f"<{subject}@x>", sender, subject, "2026-10-20T10:00:00+00:00", body, "me@gmail.com")


class Scripted:
    """Answers in turn, recording each prompt."""
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def chat_json(self, system, user):
        self.prompts.append(user)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class WordsTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")

    def test_defaults_add_remove(self):
        self.assertEqual(resale.words(self.c), ["Pokémon", "One Piece", "NeeDoh"])
        resale.remove(self.c, "NeeDoh")
        self.assertEqual(resale.words(self.c), ["Pokémon", "One Piece"])
        self.assertEqual(resale.add(self.c, "needoh"), "NeeDoh")  # a built-in comes back as it was
        resale.add(self.c, "Lorcana")
        self.assertIn("Lorcana", resale.words(self.c))
        self.assertIsNone(resale.add(self.c, "ab"))

    def test_matching_ignores_accents_case_and_spacing(self):
        self.assertTrue(resale.is_resale("POKEMON TCG BOOSTER", WORDS))
        self.assertTrue(resale.is_resale("Nee Doh Nice Cube", WORDS))
        self.assertTrue(resale.is_resale("ONEPIECE OP-09", WORDS))
        self.assertFalse(resale.is_resale("Pokeball plush keychain", WORDS))
        self.assertFalse(resale.is_resale("Paper towels", WORDS))

    def test_recheck_when_words_change(self):
        self.assertTrue(resale.needs_recheck(self.c))
        resale.mark_checked(self.c, resale.words(self.c))
        self.assertFalse(resale.needs_recheck(self.c))
        resale.add(self.c, "Funko")
        self.assertTrue(resale.needs_recheck(self.c))


class ExtractTests(unittest.TestCase):
    def test_grocery_receipt_called_other_becomes_order_with_only_resale_items(self):
        llm = Scripted({"kind": "other"}, {"kind": "order", "retailer": "Sam's Club", "total": 55.96, "items": [
            {"name": "POKEMON TCG 151 BOOSTER BUNDLE", "quantity": 1, "price": 26.98},
            {"name": "GV WHOLE MILK", "quantity": 1, "price": 3.48}]})
        r = extract(llm, mail("Your Sam's Club receipt", SAMS), resale=WORDS)
        self.assertEqual(r["kind"], "order")
        self.assertEqual([i["name"] for i in r["items"]], ["POKEMON TCG 151 BOOSTER BUNDLE"])
        self.assertEqual(r["total"], 26.98)  # groceries don't count as spend
        self.assertIn("Resale products named: Pokémon", llm.prompts[0])

    def test_model_says_other_twice_reads_receipt_lines(self):
        llm = Scripted({"kind": "other"})
        r = extract(llm, mail("Your Aldi receipt", SAMS, sender="ALDI <ereceipt@aldi.us>"), resale=WORDS)
        self.assertEqual(r["kind"], "order")
        self.assertEqual(r["items"], [{"name": "POKEMON TCG 151 BOOSTER BUNDLE", "quantity": 1, "price": 26.98}])
        self.assertEqual(r["total"], 26.98)
        self.assertEqual(r["shipping_status"], "delivered")  # bought in store

    def test_supplies_expense_with_resale_items_is_re_asked(self):
        llm = Scripted({"kind": "botting_expense", "expense_category": "supplies", "total": 55.96},
                       {"kind": "order", "items": [{"name": "Pokemon booster bundle", "price": 26.98}], "total": 55.96})
        self.assertEqual(extract(llm, mail("Receipt", SAMS), resale=WORDS)["kind"], "order")

    def test_cook_group_receipt_stays_an_expense(self):
        body = "Receipt from Whop\nPokemon Center cook group - Discord membership\nAmount paid $40.00"
        llm = Scripted({"kind": "botting_expense", "expense_category": "cook_group", "total": 40})
        self.assertEqual(extract(llm, mail("Your receipt", body), resale=WORDS)["kind"], "botting_expense")
        self.assertEqual(len(llm.prompts), 1)

    def test_online_order_stays_whole(self):
        body = ("Thanks for your order! Order #112233445\nPokemon Elite Trainer Box $49.99\nUSB-C charger $19.99\n"
                "Ship to: 1 Main St, Austin TX\nOrder total $69.98")
        llm = Scripted({"kind": "order", "retailer": "Target", "order_number": "112233445", "total": 69.98,
                        "shipping_address": "1 Main St, Austin TX",
                        "items": [{"name": "Pokemon Elite Trainer Box", "price": 49.99},
                                  {"name": "USB-C charger", "price": 19.99}]})
        r = extract(llm, mail("Order confirmation", body, sender="Target <orders@target.com>"), resale=WORDS)
        self.assertEqual(len(r["items"]), 2)
        self.assertEqual(r["total"], 69.98)

    def test_promo_listing_products_is_never_an_order(self):
        self.assertFalse(purchase_proof(mail("New arrivals!", PROMO)))
        llm = Scripted({"kind": "order", "retailer": "Third Eye Comics", "total": 269.98,
                        "items": [{"name": "Prismatic Evolutions Booster Box", "price": 159.99}]})
        r = extract(llm, mail("New arrivals!", PROMO, sender="Third Eye Comics <news@thirdeyecomics.com>"),
                    resale=WORDS)
        self.assertEqual(r["kind"], "other")
        self.assertEqual(len(llm.prompts), 1)  # a promo naming Pokemon isn't re-asked

    def test_real_orders_have_proof(self):
        for body in ("Thank you for your order", "Order #A1234567 confirmed", "Your package was delivered",
                     "Track it: 1Z999AA10123456784", "Your order has shipped", "Paid with Visa ending in 4421",
                     "Your order total: $130.00"):
            self.assertTrue(purchase_proof(mail("Hi", body)), body)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(db.connect(":memory:"))

    def test_sync_saves_grocery_receipt_and_drops_promo(self):
        replies = {"Your receipt": {"kind": "other"},
                   "New arrivals!": {"kind": "order", "retailer": "Third Eye", "total": 269.98}}

        class LLM:
            def chat_json(self, system, user):
                return replies[user.split("\n")[1].removeprefix("Subject: ")]
        emails = [mail("Your receipt", SAMS), mail("New arrivals!", PROMO, sender="Third Eye <a@b.com>")]
        run_sync(Config(), self.store, emails=emails, llm=LLM(), log=lambda *a, **k: None)
        orders = self.store.orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["total"], 26.98)

    def test_recheck_reads_skipped_resale_receipts(self):
        receipt, promo = mail("Your receipt", SAMS), mail("New arrivals!", PROMO)
        self.store.record_email(receipt, "other")
        self.store.record_email(promo, "other")
        self.store.db.execute("INSERT INTO expenses (message_id, vendor, category, amount) VALUES (?, 'Sam', 'supplies', 55.96)",
                              (receipt.message_id,))
        llm = Scripted({"kind": "order", "items": [{"name": "Pokemon bundle", "price": 26.98}], "total": 55.96})
        config = Config()
        config.accounts = [("me@gmail.com", "pw")]
        seen = {}

        def fetch(search, address, password, days, done):
            seen["q"] = search.search_query
            return [receipt, promo]
        counts = find_resale(config, self.store, llm=llm, fetch=fetch)
        self.assertIn("pokemon", seen["q"])
        self.assertIn('"one piece"', seen["q"])
        self.assertEqual(counts["order"], 1)
        self.assertEqual(counts["skipped"], 1)  # the promo, without asking the model
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0], 0)
        self.assertFalse(resale.needs_recheck(self.store.db))

    def test_clean_ad_orders_takes_out_promos_and_put_back(self):
        promo, real = mail("New arrivals!", PROMO), mail("Order", "Thank you for your order! $10.00")
        for m, retailer in ((promo, "Third Eye"), (real, "Nike")):
            self.store.save(m, {"kind": "order", "retailer": retailer, "order_number": None, "order_date": None,
                                "items": [{"name": "Box", "price": 10}], "total": 10.0, "currency": "USD",
                                "shipping_address": None, "shipping_name": None, "recipient_email": None,
                                "tracking_number": None, "tracking_url": None, "carrier": None,
                                "shipping_status": "ordered", "estimated_delivery": None})
        by_id = {promo.message_id: promo, real.message_id: real}
        n = clean_ad_orders(Config(), self.store, opener=lambda account, mid: by_id[mid])
        self.assertEqual(n, 1)
        self.assertEqual([o["retailer"] for o in self.store.orders()], ["Nike"])
        ads = resale.ad_orders(self.store.db)
        self.assertEqual(ads[0]["retailer"], "Third Eye")
        self.assertEqual(clean_ad_orders(Config(), self.store, opener=lambda a, m: by_id[m]), 0)  # once
        self.assertTrue(resale.put_back(self.store.db, ads[0]["id"]))
        self.assertEqual(len(self.store.orders()), 2)
        self.assertEqual(resale.ad_orders(self.store.db), [])


class PageTests(unittest.TestCase):
    def test_page_add_and_remove(self):
        import os
        import tempfile
        from jigstream.app import create_app
        d = tempfile.mkdtemp()
        app = create_app(os.path.join(d, "t.db"))
        app.config["TESTING"] = True
        client = app.test_client()
        r = client.get("/training/resale")
        self.assertEqual(r.status_code, 200)
        self.assertIn("NeeDoh", r.get_data(as_text=True))
        client.post("/training/resale/words", data={"word": "Lorcana"})
        self.assertIn("Lorcana", client.get("/training/resale").get_data(as_text=True))
        client.post("/training/resale/words", data={"remove": "Lorcana"})
        self.assertNotIn("Lorcana</span>", client.get("/training/resale").get_data(as_text=True))
        self.assertIn("Resale products", client.get("/training").get_data(as_text=True))
