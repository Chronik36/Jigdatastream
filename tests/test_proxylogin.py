import io
import json
import urllib.error

import pytest

from jigstream import db, proxyaccounts, proxylogin
from jigstream.app import create_app


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


REQ = {"method": "POST", "url": "https://dash.prov.com/api/gen", "status": 200, "page": "https://dash.prov.com/gen",
       "headers": {"Authorization": "Bearer T1", "Content-Type": "application/json", "Cookie": "x"}, "body": '{"qty": 2}',
       "text": json.dumps({"data": {"proxies": ["1.2.3.4:8000:u:p", "1.2.3.5:8000:u:p"]}})}


def test_learns_the_request_that_made_the_list():
    noise = {"method": "GET", "url": "https://dash.prov.com/api/me", "status": 200, "text": '{"name": "jo"}'}
    recipe, lines = proxylogin.recipe_from([noise, REQ], [], "")
    assert lines == ["1.2.3.4:8000:u:p", "1.2.3.5:8000:u:p"]
    assert recipe["kind"] == "request" and recipe["url"] == REQ["url"] and recipe["body"] == '{"qty": 2}'
    assert "Cookie" not in recipe["headers"] and recipe["headers"]["Authorization"] == "Bearer T1"


def test_learns_the_button_when_the_page_makes_the_list():
    clicks = [{"tag": "BUTTON", "text": "Generate", "index": 2, "page": "https://dash.prov.com/gen"}]
    shown = "Your proxies\ngw.prov.com:7000:user-sid1:pw\ngw.prov.com:7000:user-sid2:pw\nold.prov.com:1:a:b"
    recipe, lines = proxylogin.recipe_from([], clicks, shown, before=["old.prov.com:1:a:b"])
    assert recipe == {"kind": "click", "page": "https://dash.prov.com/gen", "button": {"tag": "BUTTON", "text": "Generate", "index": 2}}
    assert lines == ["gw.prov.com:7000:user-sid1:pw", "gw.prov.com:7000:user-sid2:pw"]
    assert proxylogin.recipe_from([], [], "nothing here") == (None, [])


def test_refresh_pool_repeats_the_request(tmp_path):
    con = proxyaccounts.ensure(db.connect(str(tmp_path / "a.db")))
    recipe, _ = proxylogin.recipe_from([REQ], [], "")
    con.execute("INSERT INTO proxy_accounts (name, auth, site_url, recipe, site_cookie) VALUES ('Hype', 'site', ?, ?, 'sid=1')",
                (REQ["page"], json.dumps(recipe)))
    sent = []

    def opener(req, timeout):
        sent.append(req)
        return Answer(REQ["text"].encode())
    acct = proxyaccounts._acct(con, 1)
    assert proxyaccounts.refresh_pool(con, acct, opener) == 2
    req = sent[0]
    assert req.get_method() == "POST" and req.get_header("Cookie") == "sid=1" and req.get_header("Authorization") == "Bearer T1"
    assert json.loads(req.data) == {"qty": 2}
    a = proxyaccounts.accounts(con)[0]
    assert a["signed_in"] and "recipe" not in a and "site_cookie" not in a and a["pool_size"] == 2

    def signed_out(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    with pytest.raises(proxylogin.SignIn):
        proxyaccounts.refresh_pool(con, acct, signed_out)
    assert "sign in again" in proxyaccounts.accounts(con)[0]["pool_error"]


class FakeWindow:
    def __init__(self):
        self.hidden = False

    def hide(self):
        self.hidden = True


def test_use_this_saves_the_recipe_and_fills_the_group(tmp_path, monkeypatch):
    path = str(tmp_path / "a.db")
    app = create_app(path)
    s = proxylogin.Session(webview=None)
    monkeypatch.setattr(proxylogin, "_webview", object())
    monkeypatch.setattr(proxylogin, "_session", s)
    con = proxyaccounts.ensure(db.connect(path))
    con.execute("INSERT INTO proxy_accounts (name, auth, site_url) VALUES ('Hype', 'site', 'https://dash.prov.com')")
    s.windows[1] = FakeWindow()
    recipe, lines = proxylogin.recipe_from([REQ], [], "")
    s.seen[1] = {"lines": lines, "recipe": recipe, "before": [], "cookie": "sid=9", "watching": True}
    c = app.test_client()
    assert c.get("/proxies/login/status?id=1").get_json()["found"] == 2
    c.post("/proxies/login/save", data={"id": 1})
    row = con.execute("SELECT recipe, site_cookie, pool_size FROM proxy_accounts WHERE id=1").fetchone()
    assert json.loads(row["recipe"])["url"] == REQ["url"] and row["site_cookie"] == "sid=9" and row["pool_size"] == 2
    assert s.windows[1].hidden and not s.seen[1]["watching"]
    assert con.execute("SELECT COUNT(*) FROM proxies WHERE grp='Hype'").fetchone()[0] == 2
    html = c.get("/proxies").get_data(as_text=True)
    assert "Sign in again" in html and "Signed-in dashboard" in html


def test_suggest_a_provider_emails_the_developer():
    sent = []

    class Smtp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, m):
            sent.append(m)
    to = proxylogin.send_suggestion({"name": "Hype", "site": "hypeproxies.com", "docs": "", "login": "Discord", "notes": "resi"},
                                    "me@example.com", "pw", "1.2", to="dev@example.com", smtp=Smtp())
    assert to == "dev@example.com"
    body = sent[0].get_content()
    assert sent[0]["Subject"].endswith("Hype") and "login: Discord" in body and "docs:" not in body


class Headers:
    def __init__(self, cookies):
        self.cookies = cookies

    def get_all(self, name):
        return self.cookies if name == "Set-Cookie" else None


def test_saved_signin_is_sent_and_renewed(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setattr("jigstream.wallet._key", None)
    con = proxyaccounts.ensure(db.connect(str(tmp_path / "a.db")))
    recipe, _ = proxylogin.recipe_from([REQ], [], "")
    saved = {"cookies": [{"name": "sess", "value": "old", "domain": ".prov.com", "path": "/"},
                         {"name": "other", "value": "x", "domain": "elsewhere.com", "path": "/"}], "storage": {"tok": "T1"}}
    sealed = proxylogin.seal(saved)
    assert "old" not in sealed and proxylogin.unseal(sealed) == saved
    con.execute("INSERT INTO proxy_accounts (name, auth, site_url, recipe, site_session) VALUES ('Hype', 'site', ?, ?, ?)",
                (REQ["page"], json.dumps(recipe), sealed))
    sent = []

    def opener(req, timeout):
        sent.append(req)
        a = Answer(REQ["text"].encode())
        a.headers = Headers(["sess=new; Path=/; Domain=.prov.com; HttpOnly"])
        return a
    acct = proxyaccounts._acct(con, 1)
    assert proxyaccounts.refresh_pool(con, acct, opener) == 2
    assert sent[0].get_header("Cookie") == "sess=old"  # only the provider's own cookies
    kept = proxylogin.unseal(con.execute("SELECT site_session FROM proxy_accounts").fetchone()[0])
    assert [c["value"] for c in kept["cookies"] if c["name"] == "sess"] == ["new"]
    assert proxyaccounts.accounts(con)[0]["remembered"]
    assert proxylogin.unseal("not sealed") == {}
