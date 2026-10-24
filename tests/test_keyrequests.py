import os

from jigstream import accountkeys, db, keyrequests
from jigstream.app import create_app


class FakeSMTP:
    sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class Config:
    imap_host = "imap.gmail.com"

    def __init__(self, accounts):
        self.accounts = accounts


def _body(msg):
    return msg.get_content()


def test_ask_allow_and_the_key_arrives(tmp_path, monkeypatch):
    private, public = accountkeys.new_signing_key()
    pub = tmp_path / "pub.txt"
    pub.write_text(public)
    monkeypatch.setattr(accountkeys, "PUBLIC_FILE", pub)
    monkeypatch.setattr(accountkeys, "revoked_ids", lambda *a: set())
    monkeypatch.setenv("BOTTRACK_SIGNING_KEY", private)
    monkeypatch.chdir(tmp_path)
    FakeSMTP.sent = []

    # the friend asks from their Gmail
    keyrequests.ask("Friend@gmail.com", "pw", "Mike", "from the Discord", "120", to="dev@gmail.com", smtp=FakeSMTP())
    ask = FakeSMTP.sent[-1]
    assert ask["To"] == "dev@gmail.com" and keyrequests.ASK_SUBJECT in ask["Subject"]

    # jacob's computer finds it (twice: still one request)
    inbox = [("friend@gmail.com", ask["Subject"], _body(ask))]
    con = db.connect(str(tmp_path / "d.db"))
    dev = Config([("dev@gmail.com", "devpw")])
    assert keyrequests.note_requests(con, dev, search_fn=lambda *a, **k: inbox) == 1
    assert keyrequests.note_requests(con, dev, search_fn=lambda *a, **k: inbox) == 0
    (row,) = keyrequests.waiting(con)
    assert row["name"] == "Mike" and row["note"] == "from the Discord" and row["inbox"] == "dev@gmail.com"

    # Allow emails the key from jacob's Gmail
    key = keyrequests.allow(con, dev, "friend@gmail.com", smtp=FakeSMTP())
    sent = FakeSMTP.sent[-1]
    assert sent["To"] == "friend@gmail.com" and sent["From"] == "dev@gmail.com" and key in _body(sent)
    assert not keyrequests.waiting(con)
    assert con.execute("SELECT email FROM license_keys").fetchone()[0] == "friend@gmail.com"

    # the friend's copy picks it up; a key for someone else, or a forged one, doesn't open it
    monkeypatch.delenv("BOTTRACK_SIGNING_KEY")
    other, _ = accountkeys.issue(private, "someone@gmail.com")
    forged, _ = accountkeys.issue(accountkeys.new_signing_key()[0], "friend@gmail.com")
    friend = Config([("friend@gmail.com", "pw")])
    assert keyrequests.look_for_key(friend, search_fn=lambda *a, **k: [("x@y", "", other + "\n" + forged)]) is None
    mail = [("dev@gmail.com", keyrequests.KEY_SUBJECT, _body(sent))]
    assert keyrequests.look_for_key(friend, search_fn=lambda *a, **k: mail) == key
    assert os.environ["BOTTRACK_KEY"] == key
    assert accountkeys.status(["friend@gmail.com"])["open"]

    # asking again later (a lost key) puts them back on the list
    again = inbox[0][2].replace(row["asked_at"], "2999-01-01T00:00:00+00:00")
    assert keyrequests.note_requests(con, dev, search_fn=lambda *a, **k: [("friend@gmail.com", "", again)]) == 1
    assert keyrequests.waiting(con)


def test_the_key_screen_offers_a_request(tmp_path, monkeypatch):
    private, public = accountkeys.new_signing_key()
    pub = tmp_path / "pub.txt"
    pub.write_text(public)
    monkeypatch.setattr(accountkeys, "PUBLIC_FILE", pub)
    monkeypatch.setattr(accountkeys, "revoked_ids", lambda *a: set())
    for k in ("BOTTRACK_KEY", "BOTTRACK_KEY_GRACE", "BOTTRACK_SIGNING_KEY", "BOTTRACK_KEY_ASKED", "GMAIL_ADDRESS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@gmail.com:abcdabcdabcdabcd")
    monkeypatch.setenv("BOTTRACK_LOGS_TO", "dev@gmail.com")
    monkeypatch.chdir(tmp_path)
    asked = []
    monkeypatch.setattr(keyrequests, "ask", lambda *a, **k: asked.append(a))
    c = create_app(str(tmp_path / "a.db")).test_client()
    assert "Request access" in c.get("/activate").get_data(as_text=True)
    c.post("/activate/request", data={"address": "me@gmail.com", "name": "Me"})
    assert asked and asked[0][0] == "me@gmail.com"
    html = c.get("/activate").get_data(as_text=True)
    assert "Waiting for your key" in html
    monkeypatch.setattr(keyrequests, "look_for_key", lambda config: None)
    assert c.get("/activate/check?now=1").get_json() == {"open": False}
    assert c.get("/orders").status_code == 302


def test_requests_show_on_the_keys_page(tmp_path, monkeypatch):
    private, _ = accountkeys.new_signing_key()
    monkeypatch.setenv("BOTTRACK_SIGNING_KEY", private)
    monkeypatch.chdir(tmp_path)
    app = create_app(str(tmp_path / "k.db"))
    con = db.connect(str(tmp_path / "k.db"))
    con.executescript(keyrequests.SCHEMA)
    con.execute("INSERT INTO key_requests (email, name, asked_at, inbox) VALUES ('pal@gmail.com', 'Pal', '2026-10-24T10:00:00', 'dev@gmail.com')")
    con.commit()
    c = app.test_client()
    assert "1 person is asking" in c.get("/setup").get_data(as_text=True)
    html = c.get("/keys").get_data(as_text=True)
    assert "pal@gmail.com" in html and "Allow" in html
    c.post("/keys/requests/ignore", data={"email": "pal@gmail.com"})
    assert not keyrequests.waiting(con)
