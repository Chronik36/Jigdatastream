from email.message import EmailMessage

import jigstream.mail as mail
from jigstream.config import Config


def raw(mid, subject):
    e = EmailMessage()
    e["Message-ID"], e["Subject"], e["From"] = mid, subject, "shop@example.com"
    e["Date"] = "Wed, 01 Oct 2026 10:00:00 -0400"
    e.set_content("Total $5.00")
    return e.as_bytes()


# folder -> {uid: (message_id, subject)}
FOLDERS = {
    '"[Gmail]/All Mail"': {b"101": ("<a>", "old"), b"102": ("<b>", "new1"), b"103": ("<c>", "new2")},
    '"[Gmail]/Spam"': {b"7": ("<s>", "forwarded receipt"), b"8": ("<b>", "duplicate of new1")},
}


class FakeIMAP:
    searches = []

    def __init__(self, host, timeout=None):
        self.folder = None

    def login(self, user, password):
        pass

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
                      b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
                      b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']

    def select(self, folder, readonly):
        assert readonly
        self.folder = folder
        return ("OK", [b"1"]) if folder in FOLDERS else ("NO", [b"no such folder"])

    def uid(self, cmd, *args):
        box = FOLDERS[self.folder]
        if cmd == "SEARCH":
            self.searches.append((self.folder, args[1]))
            return "OK", [b" ".join(sorted(box))]
        uids, what = args
        if "HEADER.FIELDS" in what:
            out = []
            for i, u in enumerate(uids.split(b",")):
                hdr = b"Message-ID: " + box[u][0].encode() + b"\r\n\r\n"
                if u == b"103":  # UID after the literal, as some servers send it
                    out += [(b"%d (BODY[HEADER.FIELDS (MESSAGE-ID)] {%d}" % (i + 1, len(hdr)), hdr), b" UID 103)"]
                else:
                    out += [(b"%d (UID %s BODY[HEADER.FIELDS (MESSAGE-ID)] {%d}" % (i + 1, u, len(hdr)), hdr), b")"]
            return "OK", out
        assert "BODY.PEEK[]" in what and b"," not in uids
        return "OK", [(b"1 (BODY[] {9}", raw(*box[uids])), b")"]

    def logout(self):
        pass


def test_reads_all_mail_then_spam_without_repeats(monkeypatch):
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    FakeIMAP.searches = []
    totals = []
    got = list(mail.fetch_emails(Config(), "me@gmail.com", "pw", days=365, skip_ids={"<a>"},
                                 on_total=totals.append))
    assert [(e.message_id, e.account) for e in got] == [
        ("<c>", "me@gmail.com"), ("<b>", "me@gmail.com"), ("<s>", "me@gmail.com")]
    assert totals == [3]
    (box1, q1), (box2, q2) = FakeIMAP.searches
    assert box1 == '"[Gmail]/All Mail"' and "-in:spam" in q1 and "newer_than:365d" in q1
    assert box2 == '"[Gmail]/Spam"' and "-in:spam" not in q2 and "in:spam" in q2 and "-in:trash" in q2


def test_localized_spam_name_and_missing_folder(monkeypatch):
    class Localized(FakeIMAP):
        def list(self):
            return "OK", [b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Pourriel"']

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", Localized)
    assert mail.spam_folder(Localized("x")) == '"[Gmail]/Pourriel"'
    # The folder isn't selectable here, so only All Mail is read.
    got = list(mail.fetch_emails(Config(), "me@gmail.com", "pw", skip_ids={"<a>", "<b>"}))
    assert [e.message_id for e in got] == ["<c>"]


def test_search_headers_pages_through_all_mail_then_spam(monkeypatch):
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    page1 = mail.search_headers(Config(), "me@gmail.com", "pw", "", limit=2)
    assert [(r["message_id"], r["f"], r["uid"]) for r in page1] == [("<c>", 0, 103), ("<b>", 0, 102)]
    page2 = mail.search_headers(Config(), "me@gmail.com", "pw", "", limit=2, after={"f": 0, "u": 102})
    assert [(r["message_id"], r["f"], r["spam"]) for r in page2] == [("<a>", 0, False), ("<b>", 1, True)]
    page3 = mail.search_headers(Config(), "me@gmail.com", "pw", "", limit=2, after={"f": 1, "u": 8})
    assert [r["message_id"] for r in page3] == ["<s>"]
