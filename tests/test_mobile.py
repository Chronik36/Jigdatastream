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
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).get_data(as_text=True)).group(1)


def test_phone_must_sign_in_and_never_reaches_desktop_pages(app):
    c = phone(app)
    assert c.get("/m/orders").headers["Location"].endswith("/m/login")
    assert c.get("/setup").headers["Location"].endswith("/m/login")
    assert sign_in(c, "wrong password").status_code == 401
    assert sign_in(c).headers["Location"].endswith("/m/orders")
    assert c.get("/m/orders").status_code == 200
    for path in ("/setup", "/logs", "/logs/download", "/api/orders"):
        assert c.get(path).headers["Location"].endswith("/m/orders"), path
    assert c.post("/update/install").status_code in (302, 400)  # never runs the desktop route
    assert c.post("/m/setup", data={"on": "1", "csrf": csrf(c, "/m/app")}).status_code == 404


def test_the_mac_window_needs_no_sign_in(app):
    c = app.test_client()
    assert c.get("/setup").status_code == 200
    assert c.get("/m/orders").status_code == 200
    # a proxy on this Mac (like tailscale serve) is treated as a phone
    assert c.get("/m/orders", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 302
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


def test_changing_the_password_signs_phones_out(app):
    c = phone(app)
    sign_in(c)
    assert c.get("/m/orders").status_code == 200
    os.environ["PHONE_PASSWORD_HASH"] = generate_password_hash("a brand new password")
    assert c.get("/m/orders").status_code == 302


def test_posts_need_the_page_token(app):
    c = phone(app)
    sign_in(c)
    assert c.post("/m/orders/new", data={"retailer": "Nike"}).status_code == 400
    token = csrf(c, "/m/orders/new")
    assert c.post("/m/orders/new", data={"retailer": "Nike", "csrf": token}).status_code == 302


def test_add_edit_delete_from_a_phone(app):
    c = phone(app)
    sign_in(c)
    t = csrf(c, "/m/orders/new")
    c.post("/m/orders/new", data={"csrf": t, "retailer": "Nike", "order_number": "C123", "total": "$1,210.50",
                                  "items": "Dunk Low x2\nSocks", "shipping_status": "ordered"})
    page = c.get("/m/orders").get_data(as_text=True)
    assert "Nike" in page and "Dunk Low x2" in page and "$1,210.50" in page
    oid = int(re.search(r"/m/orders/(\d+)", page).group(1))
    c.post(f"/m/orders/{oid}", data={"csrf": t, "retailer": "Nike", "order_number": "C123",
                                     "tracking_number": "1Z999AA10123456784", "shipping_status": "delivered"})
    o = appmod.Store(appmod.db.connect(app.config["DB_PATH"])).order(oid)
    assert o["shipping_status"] == "delivered" and o["status_manual"] == 1 and o["tracking_number"].startswith("1Z")
    assert "ups.com" in c.get(f"/m/orders/{oid}").get_data(as_text=True)
    assert c.get("/m/orders?status=delivered").get_data(as_text=True).count("/m/orders/") >= 1
    c.post(f"/m/orders/{oid}/delete", data={"csrf": t})
    assert "No orders yet" in c.get("/m/orders").get_data(as_text=True)

    c.post("/m/expenses/new", data={"csrf": t, "vendor": "Some Proxies", "amount": "45", "category": "proxy",
                                    "billing_period": "monthly"})
    page = c.get("/m/expenses").get_data(as_text=True)
    assert "Some Proxies" in page and "$45.00" in page
    eid = int(re.search(r"/m/expenses/(\d+)", page).group(1))
    c.post(f"/m/expenses/{eid}", data={"csrf": t, "vendor": "Some Proxies", "amount": "50", "category": "proxy"})
    assert "$50.00" in c.get("/m/expenses").get_data(as_text=True)

    c.post("/m/profiles/new", data={"csrf": t, "name": "Main", "first_name": "Jo", "bill_city": "Austin", "same": "on"})
    assert "Austin" in c.get("/m/profiles/Main/edit").get_data(as_text=True)
    c.post("/m/profiles/Main/edit", data={"csrf": t, "name": "Main", "ship_city": "Dallas", "bill_city": "Austin"})
    row = appmod.db.connect(app.config["DB_PATH"]).execute("SELECT * FROM profiles").fetchone()
    assert (row["ship_city"], row["bill_city"]) == ("Dallas", "Austin")
    c.post("/m/profiles/Main/delete", data={"csrf": t})
    assert "No profiles yet" in c.get("/m/profiles").get_data(as_text=True)


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
    page = c.get("/m/orders").get_data(as_text=True)
    assert "A new version of BotTrack is ready" in page
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
    assert "Android app is ready" not in c.get("/m/orders").get_data(as_text=True)  # a browser, not the app
    old = c.get("/m/orders", headers={"User-Agent": "Mozilla/5.0 BotTrackApp/2"}).get_data(as_text=True)
    assert "Android app is ready" in old and "BotTrack-android-3.apk" in old
    assert "Android app is ready" not in c.get("/m/orders", headers={"User-Agent": "x BotTrackApp/3"}).get_data(as_text=True)


def test_release_check_finds_the_android_app(monkeypatch):
    from jigstream import updater
    rel = {"tag_name": "build-9-abc", "body": "x", "assets": [
        {"name": "bottrack-code.zip", "browser_download_url": "https://e/code.zip"},
        {"name": "BotTrack-android-4.apk", "browser_download_url": "https://e/BotTrack-android-4.apk"}]}
    monkeypatch.setattr(updater, "_get", lambda *a, **k: __import__("json").dumps(rel).encode())
    updater._latest_release(updater.settings())
    assert updater.state["apk_version"] == 4 and updater.state["apk_url"].endswith("-4.apk")
