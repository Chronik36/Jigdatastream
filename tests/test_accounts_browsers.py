"""Store accounts (passwords sealed) and Browsers (Chrome through a proxy, a saved or fresh session each)."""
import socket
import threading

import pytest

from jigstream import browsers, db, mobile, proxies, storeaccounts, wallet
from jigstream.app import create_app


@pytest.fixture(autouse=True)
def key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setattr(wallet, "_key", None)


def test_pasted_accounts_in_any_order():
    got, problems = storeaccounts.parse_lines("a@b.com:pw1\nc@d.com:pw2:user2\nuser3:e@f.com:pw3\nnope:x\ng@h.com", "Riot")
    assert [(a["email"], a["password"], a["username"]) for a in got] == [
        ("a@b.com", "pw1", None), ("c@d.com", "pw2", "user2"), ("e@f.com", "pw3", "user3")]
    assert problems == ["line 4: no email", "line 5: no password"]
    assert storeaccounts.site_name("https://www.riot.com/") == "Riot" and storeaccounts.site_name("pokemon-center.com") == "Pokemon Center"
    assert storeaccounts.needs_username("Riot") and not storeaccounts.needs_username("Target")


def test_accounts_page_keeps_passwords_sealed(tmp_path):
    path = str(tmp_path / "a.db")
    app = create_app(path)
    c = app.test_client()
    con = db.connect(path)
    con.execute("INSERT INTO profiles (name, email) VALUES ('Jig 1', 'a@b.com')")
    con.commit()
    c.post("/accounts/add", data={"site": "target.com", "email": "a@b.com", "password": "hunter2", "status": "active"})
    c.post("/accounts/add", data={"site": "Riot", "lines": "c@d.com:pw2\nriotjake:e@f.com:pw3"})
    c.post("/accounts/add", data={"site": "Target", "email": "A@b.com", "password": "newpw"})  # same account: updated
    rows = storeaccounts.rows(con)
    assert [(r["site"], r["email"], r["username"], r["profile"]) for r in rows] == [
        ("Riot", "e@f.com", "riotjake", None), ("Target", "a@b.com", None, "Jig 1")]
    raw = con.execute("SELECT password FROM store_accounts WHERE site = 'Target'").fetchone()[0]
    assert "newpw" not in raw and storeaccounts.password(con, rows[1]["id"]) == "newpw"
    page = c.get("/accounts").get_data(as_text=True)
    assert "newpw" not in page and "Jig 1" in page and "riotjake" in page and 'href="/accounts?site=Riot"' in page
    assert c.get(f"/accounts/{rows[1]['id']}/password").get_json() == {"password": "newpw"}
    assert "storeaccounts.reveal" in mobile.PHONE_BLOCKED_ENDPOINTS
    c.post(f"/accounts/{rows[0]['id']}/edit", data={"site": "Riot", "email": "e@f.com", "username": ""})
    assert storeaccounts.rows(con)[0]["username"] == "riotjake"  # Riot can't lose its username
    c.post("/accounts/bulk", data={"ids": [str(rows[0]["id"])], "action": "banned"})
    assert storeaccounts.rows(con, status="banned")[0]["email"] == "e@f.com"


class FakeChrome:
    def __init__(self, args, **kw):
        self.args, self.code = args, None
        launched.append(self)

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        while self.code is None:
            done.wait(timeout or 5)
            self.code = 0
        return 0

    terminate = kill = lambda self: done.set()


launched, done = [], threading.Event()


def test_browsers_spread_over_a_group_and_keep_or_drop_sessions(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    create_app(path)
    con = db.connect(path)
    proxies.add_list(con, "1.1.1.1:8000:u1:p1\n2.2.2.2:8000:u2:p2", "Acme", "Resi")
    fake = tmp_path / "chrome"
    fake.write_text("")
    browsers.ensure(con)
    con.execute("INSERT INTO browser_settings VALUES ('app', ?)", (str(fake),))
    con.commit()
    ids = browsers.add(con, 3, "Target", group="Resi", keep=True)
    assert [r["proxy_id"] for r in browsers.rows(con)] == [1, 2, 1]
    con.execute("UPDATE browsers SET keep_session = 0 WHERE id = ?", (ids[1],))
    con.commit()
    launched.clear()
    done.clear()
    assert browsers.launch(con, ids[0], path, popen=FakeChrome) is None
    assert browsers.launch(con, ids[1], path, popen=FakeChrome) is None
    kept, fresh = launched
    assert f"--user-data-dir={browsers.sessions_dir(path) / str(ids[0])}" in kept.args
    assert any(a.startswith("--proxy-server=http://127.0.0.1:") for a in kept.args)
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in kept.args
    fresh_dir = next(a.split("=", 1)[1] for a in fresh.args if a.startswith("--user-data-dir="))
    assert browsers.is_running(ids[0]) and browsers.is_running(ids[1])
    browsers.close(ids[1])
    browsers.close(ids[0])
    import os
    assert not os.path.exists(fresh_dir) and (browsers.sessions_dir(path) / str(ids[0])).exists()
    page = create_app(path).test_client().get("/browsers").get_data(as_text=True)
    assert "Save session" in page and "Fresh each time" in page and "Open all" in page


def test_relay_adds_the_proxy_login():
    up = socket.socket()
    up.bind(("127.0.0.1", 0))
    up.listen(1)
    got = []

    def upstream():
        s, _ = up.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            data += s.recv(4096)
        got.append(data)
        s.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        s.close()
    threading.Thread(target=upstream, daemon=True).start()
    relay = browsers.Relay({"host": "127.0.0.1", "port": up.getsockname()[1], "user": "bob", "password": "pw"})
    c = socket.create_connection(("127.0.0.1", relay.port))
    c.sendall(b"CONNECT www.target.com:443 HTTP/1.1\r\nHost: www.target.com:443\r\n\r\n")
    assert c.recv(100).startswith(b"HTTP/1.1 200")
    relay.close()
    assert got[0].startswith(b"CONNECT www.target.com:443 HTTP/1.1\r\nProxy-Authorization: Basic Ym9iOnB3\r\n")
