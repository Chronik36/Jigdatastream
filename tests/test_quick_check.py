import unittest
from unittest import mock

from jigstream import db, mail
from jigstream.config import Config
from jigstream.orderstore import Store
from jigstream.sync import quick_check
from tests.test_orders import FakeLLM, mail as email


def raw(mid, subject):
    return (f"Message-ID: {mid}\r\nFrom: Nike <orders@nike.com>\r\nSubject: {subject}\r\n"
            f"Date: Sat, 24 Oct 2026 10:00:00 +0000\r\n\r\nYour order total: $130.00\r\n").encode()


class FakeIMAP:
    """All Mail holds UIDs 1-8 (8 newest); Spam is empty."""
    def __init__(self, host):
        self.folder, self.fetched = None, []

    def login(self, a, p):
        return "OK", []

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']

    def select(self, folder, readonly=True):
        self.folder = folder
        return "OK", []

    def uid(self, cmd, *args):
        spam = "Spam" in self.folder
        if cmd == "SEARCH":
            return "OK", [b"" if spam else b"1 2 3 4 5 6 7 8"]
        uids, what = args
        if "MESSAGE-ID" in what:
            data = []
            for u in uids.split(b","):
                data += [(b"%s (UID %s BODY[HEADER] {40}" % (u, u), b"Message-ID: <m%s@x>\r\n\r\n" % u), b")"]
            return "OK", data
        self.fetched.append(uids)
        return "OK", [(b"1 (BODY[] {100}", raw(f"<m{uids.decode()}@x>", f"Order {uids.decode()}"))]

    def logout(self):
        pass


class QuickCheckTests(unittest.TestCase):
    def test_reads_only_new_ones_among_the_newest(self):
        store = Store(db.connect(":memory:"))
        for u in (8, 6):  # already read
            store.record_email(email(f"Order {u}", mid=f"<m{u}@x>"), "order")
        store.db.commit()
        fake = {}
        with mock.patch.object(mail.imaplib, "IMAP4_SSL", lambda host: fake.setdefault("imap", FakeIMAP(host))):
            got = list(mail.newest_emails(Config(), "me@gmail.com", "pw", 3,
                                          lambda ids: {i for i in ids if i in ("<m8@x>", "<m6@x>")}))
        self.assertEqual([e.message_id for e in got], ["<m7@x>"])  # 8, 7, 6 are the newest three
        self.assertEqual(fake["imap"].fetched, [b"7"])

    def test_quick_check_saves_new_orders_and_skips_known(self):
        store = Store(db.connect(":memory:"))
        store.record_email(email("Old order", mid="<old@x>"), "order")
        store.db.commit()
        asked = []

        def fetch(config, address, password, n, known):
            asked.append((address, n))
            for e in [email("Old order", mid="<old@x>"), email("Order confirmed", mid="<new@x>")]:
                if e.message_id not in known([e.message_id]):
                    yield e

        llm = FakeLLM({"Order confirmed": {"kind": "order", "retailer": "Nike", "order_number": "Q1", "total": 130,
                                           "shipping_status": "ordered"}})
        counts = quick_check(Config(accounts=[("me@gmail.com", "pw")], use_examples=False), store, llm=llm,
                             fetch=fetch)
        self.assertEqual(counts["order"], 1)
        self.assertEqual(asked, [("me@gmail.com", 5)])
        self.assertEqual([o["order_number"] for o in store.orders()], ["Q1"])

    def test_stops_when_a_full_sync_starts(self):
        store = Store(db.connect(":memory:"))
        fetch = mock.Mock(return_value=iter([]))
        quick_check(Config(accounts=[("a@gmail.com", "pw")]), store, llm=FakeLLM({}), stop=lambda: True, fetch=fetch)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
