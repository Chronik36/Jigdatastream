import os
import re

import pytest
from werkzeug.security import generate_password_hash

import jigstream.app as appmod
from jigstream import mobile

PHONE = {"REMOTE_ADDR": "100.101.102.103"}  # a Tailscale address
PASSWORD = "correct horse battery"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if not k.startswith(("PHONE", "GMAIL"))})
    os.environ.update(PHONE_ACCESS="1", PHONE_EMAIL="me@example.com",
                      PHONE_PASSWORD_HASH=generate_password_hash(PASSWORD))
    monkeypatch.setattr(appmod, "installed_models", lambda url: [])
    mobile.failures.clear()
    return appmod.create_app(str(tmp_path / "t.db"))


def client(app, addr):
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = addr
    return c


def phone(app):
    return client(app, PHONE["REMOTE_ADDR"])


def sign_in(c, password=PASSWORD):
    return c.post("/m/login", data={"email": "Me@Example.com ", "password": password})


def csrf(c, path):
    page = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" (?:value|content)="([^"]+)"', page).group(1)


def test_phone_must_sign_in_and_never_reaches_mac_only_pages(app):
    c = phone(app)
    assert c.get("/orders").headers["Location"].endswith("/m/login")
    assert c.get("/setup").headers["Location"].endswith("/m/login")
    assert sign_in(c, "wrong password").status_code == 401
    assert sign_in(c).headers["Location"] == "/"
    for path in ("/", "/orders", "/expenses", "/expenses/ledger", "/profiles", "/stock", "/training", "/products",
                 "/inbox", "/keywords", "/cards", "/checkouts", "/manual", "/m/app"):
        assert c.get(path).status_code == 200, path
    for path in ("/setup", "/logs", "/logs/download", "/bank/plaid"):
        assert c.get(path).headers["Location"] == "/", path
    page = c.get("/", follow_redirects=True).get_data(as_text=True)
    t = csrf(c, "/orders")
    assert c.post("/update/rollback", data={"csrf": t}).status_code == 403
    assert c.post("/profiles/export", data={"csrf": t}).status_code == 403
    assert c.get("/cards/1/reveal").status_code == 403
    assert c.post("/m/setup", data={"on": "1", "csrf": t}).status_code == 404
    page = c.get("/orders").get_data(as_text=True)
    assert 'href="/setup" title="Setup"' not in page and 'href="/logs" title' not in page and 'href="/m/app"' in page


def test_the_mac_window_needs_no_sign_in(app):
    c = app.test_client()
    assert c.get("/setup").status_code == 200
    page = c.get("/orders").get_data(as_text=True)
    assert 'href="/setup" title="Setup"' in page and "ptabs" not in page  # the Mac keeps its own layout
    # a proxy on this Mac (like tailscale serve) is treated as a phone
    assert c.get("/orders", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 302
    assert c.get("/setup", base_url="http://bottrack.tail1234.ts.net").status_code == 302


def test_blocked_when_off_or_from_outside_the_tailnet(app):
    assert client(app, "203.0.113.9").get("/m/login").status_code == 404
    assert client(app, "192.168.1.20").get("/m/login").status_code == 404
    os.environ["PHONE_ALLOW_LAN"] = "1"
    assert client(app, "192.168.1.20").get("/m/login").status_code == 200
    os.environ["PHONE_ACCESS"] = "0"
    assert phone(app).get("/m/login").status_code == 404


def test_sign_in_locks_after_repeated_failures(app):
    c = phone(app)
    for _ in range(mobile.LOCK_AFTER):
        assert sign_in(c, "nope").status_code == 401
    assert sign_in(c).status_code == 429
    os.environ["PHONE_LOCKOUT"] = "0"  # the Setup switch for testing
    assert sign_in(c).status_code == 302


def test_saving_setup_lifts_a_lockout_and_can_switch_it_off(app):
    for _ in range(mobile.LOCK_AFTER):
        sign_in(phone(app), "nope")
    mac = app.test_client()
    mac.post("/m/setup", data={"csrf": csrf(mac, "/setup"), "on": "1", "email": "me@example.com", "no_lockout": "1"})
    assert os.environ["PHONE_LOCKOUT"] == "0" and not mobile.failures
    for _ in range(mobile.LOCK_AFTER + 2):
        assert sign_in(phone(app), "nope").status_code == 401
    assert sign_in(phone(app)).status_code == 302


def test_changing_the_password_signs_phones_out(app):
    c = phone(app)
    sign_in(c)
    assert c.get("/orders").status_code == 200
    os.environ["PHONE_PASSWORD_HASH"] = generate_password_hash("a brand new password")
    assert c.get("/orders").status_code == 302


def test_posts_need_the_page_token(app):
    c = phone(app)
    sign_in(c)
    page = c.get("/orders").get_data(as_text=True)
    assert "ptabs" in page and 'name="csrf"' in page  # phone layout, and forms get the token
    assert c.post("/expenses/new", data={"vendor": "X", "amount": "5"}).status_code == 400
    t = csrf(c, "/orders")
    assert c.post("/expenses/new", data={"csrf": t, "vendor": "X", "amount": "5"}).status_code in (200, 302)
    assert c.post("/expenses/new", data={"vendor": "Y", "amount": "5"}, headers={"X-CSRF-Token": t}).status_code in (200, 302)


def test_old_phone_pages_open_the_real_ones(app):
    c = phone(app)
    sign_in(c)
    assert c.get("/m/orders").headers["Location"] == "/orders"
    assert c.get("/m/profiles").headers["Location"] == "/profiles"
    assert c.get("/m/nope").status_code == 404


def test_setup_turns_phone_access_on(app):
    os.environ.pop("PHONE_PASSWORD_HASH")
    os.environ["PHONE_ACCESS"] = "0"
    c = app.test_client()
    t = csrf(c, "/setup")
    assert c.post("/m/setup", data={"on": "1", "email": "me@example.com", "password": "x"}).status_code == 404
    r = c.post("/m/setup", data={"csrf": t, "on": "1", "email": "me@example.com", "password": "short"}, follow_redirects=True)
    assert "at least 10" in r.get_data(as_text=True)
    c.post("/m/setup", data={"csrf": t, "on": "1", "email": "Me@example.com", "password": PASSWORD})
    env = open(".env").read()
    assert "PHONE_ACCESS=1" in env and "PHONE_EMAIL=me@example.com" in env and PASSWORD not in env
    assert "PHONE_SECRET_KEY=" in env
    assert "Restart BotTrack to turn phone access on" in c.get("/setup").get_data(as_text=True)
    assert sign_in(phone(app)).status_code == 302


def test_pages_see_changes_from_anywhere(app):
    c = phone(app)
    sign_in(c)
    n = c.get("/m/api/changes").get_json()["n"]
    con = appmod.db.connect(app.config["DB_PATH"])
    con.execute("INSERT INTO profiles (name) VALUES ('From the Mac')")  # e.g. the Mac's own pages or a sync
    con.commit()
    assert c.get("/m/api/changes").get_json()["n"] == n + 1
    appmod.Store(con).add_expense({"vendor": "Proxies", "amount": 5.0})
    assert c.get("/m/api/changes").get_json()["n"] == n + 2
    assert client(app, "100.64.0.9").get("/m/api/changes").status_code == 302  # signed out: no data


def test_update_from_the_phone(app, monkeypatch):
    from jigstream import updater
    c = phone(app)
    sign_in(c)
    monkeypatch.setitem(updater.state, "available", True)
    page = c.get("/orders").get_data(as_text=True)
    assert "A new version of BotTrack is ready" in page and 'action="/m/update"' in page
    calls = []
    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    monkeypatch.setattr(updater, "install", lambda: calls.append("install"))
    monkeypatch.setattr(updater, "restart_soon", lambda: calls.append("restart"))
    t = csrf(c, "/m/app")
    assert c.post("/m/update").status_code == 400  # needs the page token
    assert "Updating BotTrack" in c.post("/m/update", data={"csrf": t}).get_data(as_text=True)
    assert calls == ["install", "restart"]
    app.sync_state["running"] = True
    c.post("/m/update", data={"csrf": t})
    assert calls == ["install", "restart"]


def test_android_app_is_offered_its_update(app, monkeypatch):
    from jigstream import updater
    monkeypatch.setitem(updater.state, "available", False)
    monkeypatch.setitem(updater.state, "apk_version", 3)
    monkeypatch.setitem(updater.state, "apk_url", "https://example.com/BotTrack-android-3.apk")
    c = phone(app)
    sign_in(c)
    assert "Android app" not in c.get("/m/app").get_data(as_text=True)  # a browser, not the app
    old = c.get("/m/app", headers={"User-Agent": "Mozilla/5.0 BotTrackApp/2"}).get_data(as_text=True)
    assert "Version 3 is ready" in old and "BotTrack-android-3.apk" in old
    assert "Version 3 is ready" not in c.get("/m/app", headers={"User-Agent": "x BotTrackApp/3"}).get_data(as_text=True)


def test_release_check_finds_the_android_app(monkeypatch):
    from jigstream import updater
    rel = {"tag_name": "build-9-abc", "body": "x", "assets": [
        {"name": "bottrack-code.zip", "browser_download_url": "https://e/code.zip"},
        {"name": "BotTrack-android-4.apk", "browser_download_url": "https://e/BotTrack-android-4.apk"}]}
    monkeypatch.setattr(updater, "_get", lambda *a, **k: __import__("json").dumps(rel).encode())
    updater._latest_release(updater.settings())
    assert updater.state["apk_version"] == 4 and updater.state["apk_url"].endswith("-4.apk")
