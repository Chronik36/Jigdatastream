import os
import re

import pytest
from werkzeug.security import generate_password_hash

import jigstream.app as appmod
from jigstream import mobile

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


def phone(app, ip="198.51.100.7"):
    """A phone on the internet, through the phone link: cloudflared on the Mac passes its requests to
    127.0.0.1 with the phone's own address in Cf-Connecting-Ip."""
    c = client(app, "127.0.0.1")
    c.environ_base["HTTP_CF_CONNECTING_IP"] = ip
    return c


def pair(c):
    return c.get("/m/pair?k=" + os.environ["PHONE_PAIR_KEY"])


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


def test_blocked_when_off_or_not_through_the_phone_link(app):
    assert phone(app).get("/m/login").status_code == 200
    assert client(app, "203.0.113.9").get("/m/login").status_code == 404
    assert client(app, "100.101.102.103").get("/m/login").status_code == 404  # Tailscale isn't used any more
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
    assert pair(c).status_code == 429  # the QR code waits too
    assert sign_in(phone(app, ip="198.51.100.8")).status_code == 302  # by the phone's address, not the link's
    os.environ["PHONE_LOCKOUT"] = "0"  # the Setup switch for testing...
    assert sign_in(c).status_code == 429  # ...never applies to the internet-facing link
    os.environ["PHONE_ALLOW_LAN"] = "1"
    wifi = client(app, "192.168.1.20")
    for _ in range(mobile.LOCK_AFTER + 1):
        assert sign_in(wifi, "nope").status_code == 401
    assert sign_in(wifi).status_code == 302


def test_saving_setup_lifts_a_lockout_and_can_switch_it_off(app):
    for _ in range(mobile.LOCK_AFTER):
        sign_in(phone(app), "nope")
    assert sign_in(phone(app)).status_code == 429
    mac = app.test_client()
    mac.post("/m/setup", data={"csrf": csrf(mac, "/setup"), "on": "1", "email": "me@example.com",
                               "no_lockout": "1", "allow_lan": "1"})
    assert os.environ["PHONE_LOCKOUT"] == "0" and not mobile.failures
    assert sign_in(phone(app)).status_code == 302
    for _ in range(mobile.LOCK_AFTER + 2):
        assert sign_in(client(app, "192.168.1.20"), "nope").status_code == 401
    assert sign_in(client(app, "192.168.1.20")).status_code == 302


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
    for k in ("PHONE_PASSWORD_HASH", "PHONE_EMAIL", "PHONE_PAIR_KEY", "PHONE_PAIR_TOPIC"):
        os.environ.pop(k, None)
    os.environ["PHONE_ACCESS"] = "0"
    c = app.test_client()
    t = csrf(c, "/setup")
    assert c.post("/m/setup", data={"on": "1"}).status_code == 404
    r = c.post("/m/setup", data={"csrf": t, "on": "1", "email": "me@example.com", "password": "short"}, follow_redirects=True)
    assert "at least 10" in r.get_data(as_text=True)
    c.post("/m/setup", data={"csrf": t, "on": "1"})  # no email or password needed: phones pair by QR code
    env = open(".env").read()
    assert "PHONE_ACCESS=1" in env and "PHONE_SECRET_KEY=" in env and "PHONE_PAIR_KEY=" in env and "PHONE_PAIR_TOPIC=bottrack-" in env
    page = c.get("/setup").get_data(as_text=True)
    assert "Restart BotTrack" not in page and "Starting the phone link" in page  # no restart, the link starts right away
    assert pair(phone(app)).headers["Location"] == "/"
    assert "Sign in" not in phone(app).get("/m/login").get_data(as_text=True)  # no password set
    c.post("/m/setup", data={"csrf": t, "on": "1", "email": "Me@example.com", "password": PASSWORD})
    env = open(".env").read()
    assert "PHONE_EMAIL=me@example.com" in env and PASSWORD not in env
    assert sign_in(phone(app)).status_code == 302
    c.post("/m/setup", data={"csrf": t, "on": "1", "allow_lan": "1"})
    assert "Restart BotTrack to allow Wi-Fi" in c.get("/setup").get_data(as_text=True)


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
    assert phone(app).get("/m/api/changes").status_code == 302  # signed out: no data


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
    from jigstream import mobile
    waiting = c.post("/m/update", data={"csrf": t}).get_data(as_text=True)
    assert "Updating BotTrack" in waiting and mobile.BOOT in waiting  # it waits for a new boot id
    assert c.get("/m/api/changes").get_json()["boot"] == mobile.BOOT
    assert calls == ["install", "restart"]
    paused = []
    monkeypatch.setattr(app, "pause_sync_for_update", lambda: paused.append(1))
    c.post("/m/update", data={"csrf": t})  # a running sync is paused for the update, not a reason to refuse
    assert paused and calls == ["install", "restart", "install", "restart"]


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
    assert "A new version of the BotTrack app is ready" in old  # every page offers it, not just App & updates
    assert 'href="https://example.com/BotTrack-android-3.apk"' in old  # older apps download it in the browser


def test_newer_android_apps_update_themselves(app, monkeypatch):
    from jigstream import updater
    monkeypatch.setitem(updater.state, "available", False)
    monkeypatch.setitem(updater.state, "apk_version", 6)
    monkeypatch.setitem(updater.state, "apk_url", "https://github.com/x/y/releases/download/b/BotTrack-android-6.apk")
    c = phone(app)
    pair(c)
    page = c.get("/orders", headers={"User-Agent": "Mozilla/5.0 BotTrackApp/5"}).get_data(as_text=True)
    assert 'href="bottrack://update?url=https%3A%2F%2Fgithub.com%2Fx%2Fy%2Freleases%2Fdownload%2Fb%2FBotTrack-android-6.apk"' in page
    assert "BotTrack app is ready" not in c.get("/orders", headers={"User-Agent": "x BotTrackApp/6"}).get_data(as_text=True)


def test_release_check_finds_the_android_app(monkeypatch):
    from jigstream import updater
    rel = {"tag_name": "build-9-abc", "body": "x", "assets": [
        {"name": "bottrack-code.zip", "browser_download_url": "https://e/code.zip"},
        {"name": "BotTrack-android-4.apk", "browser_download_url": "https://e/BotTrack-android-4.apk"}]}
    monkeypatch.setattr(updater, "_get", lambda *a, **k: __import__("json").dumps(rel).encode())
    updater._latest_release(updater.settings())
    assert updater.state["apk_version"] == 4 and updater.state["apk_url"].endswith("-4.apk")


def test_scanning_the_qr_code_pairs_a_phone(app, monkeypatch):
    from jigstream import tunnel
    monkeypatch.setitem(tunnel.state, "url", "https://quiet-river-words.trycloudflare.com")
    monkeypatch.setitem(tunnel.state, "status", "on")
    mac = app.test_client()
    page = mac.get("/setup").get_data(as_text=True)
    key, topic = os.environ["PHONE_PAIR_KEY"], os.environ["PHONE_PAIR_TOPIC"]
    qr = f"https://quiet-river-words.trycloudflare.com/m/pair?k={key}&amp;t={topic}"
    assert qr in page and "qrcode.js" in page
    assert mac.get("/m/api/link").get_json()["pair_url"] == qr.replace("&amp;", "&")
    c = phone(app)
    assert c.get("/m/pair?k=nope").status_code == 401
    assert pair(c).headers["Location"] == "/"
    assert c.get("/orders").status_code == 200
    assert c.get("/m/api/link").headers["Location"] == "/"  # the QR code itself stays on the Mac
    manifest = c.get("/m/manifest.webmanifest").get_json()
    assert manifest["start_url"] == "/m/pair?k=" + key  # an iPhone home screen app pairs itself on launch
    assert phone(app).get("/m/manifest.webmanifest").get_json()["start_url"] == "/"
    # Forget all phones: a new code, and the paired phone is signed out
    mac.post("/m/setup/forget", data={"csrf": csrf(mac, "/setup")})
    assert os.environ["PHONE_PAIR_KEY"] != key and os.environ["PHONE_PAIR_TOPIC"] != topic
    assert c.get("/orders").status_code == 302
    assert c.get("/m/pair?k=" + key).status_code == 401


def test_setup_starts_and_stops_the_phone_link(app, monkeypatch):
    from jigstream import tunnel
    calls = []
    monkeypatch.setattr(tunnel, "ensure", lambda port: calls.append(("ensure", port)))
    monkeypatch.setattr(tunnel, "stop", lambda: calls.append("stop"))
    mac = app.test_client()
    mac.post("/m/setup", data={"csrf": csrf(mac, "/setup"), "on": "1"})
    assert calls == []  # only the Mac app runs the link
    app.config.update(PHONE_LINK=True, PORT=8765)
    mac.post("/m/setup", data={"csrf": csrf(mac, "/setup"), "on": "1"})
    mac.post("/m/setup", data={"csrf": csrf(mac, "/setup")})
    assert calls == [("ensure", 8765), "stop"]


def test_phone_link_reads_its_address_and_checks_its_download(tmp_path, monkeypatch):
    import hashlib
    import io
    import tarfile
    from jigstream import tunnel
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    tunnel.LOG.write_text("INF Requesting new quick Tunnel on trycloudflare.com...\n"
                          "ERR failed to reach https://api.trycloudflare.com/tunnel\n"
                          "INF |  https://quiet-river-words.trycloudflare.com  |\n")
    assert tunnel.url_in_log() == "https://quiet-river-words.trycloudflare.com"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("cloudflared")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"exe!"))
    data = buf.getvalue()
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)
    get = lambda url, timeout: io.BytesIO(data)
    with pytest.raises(RuntimeError, match="checksum"):
        tunnel.binary(get)
    assert not tunnel.BIN.exists()
    monkeypatch.setitem(tunnel.SHA256, tunnel._arch(), hashlib.sha256(data).hexdigest())
    assert tunnel.binary(get) == str(tunnel.BIN) and tunnel.BIN.read_bytes() == b"exe!"


def test_phone_link_address_goes_to_the_private_topic(monkeypatch):
    from jigstream import tunnel
    sent = []

    class Done:
        def close(self):
            pass
    monkeypatch.setenv("PHONE_PAIR_TOPIC", "bottrack-abc123")
    tunnel.announce("https://a-b.trycloudflare.com", post=lambda req, timeout: sent.append(req) or Done())
    assert sent[0].full_url == "https://ntfy.sh/bottrack-abc123" and sent[0].data == b"https://a-b.trycloudflare.com"


def test_phone_can_send_logs_but_not_read_them(app, monkeypatch):
    from jigstream import logs
    sent = []
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@gmail.com:abcd efgh ijkl mnop")
    monkeypatch.setattr(logs, "developer_email", lambda: "dev@example.com")
    monkeypatch.setattr(logs, "send_logs", lambda sender, pw, note, about: sent.append((sender, note)) or "dev@example.com")
    c = phone(app)
    pair(c)
    assert 'href="/logs/send"' in c.get("/m/app").get_data(as_text=True)
    page = c.get("/logs/send").get_data(as_text=True)
    assert "What went wrong?" in page and 'href="/logs?file=' not in page
    for path in ("/logs", "/logs/download"):
        assert c.get(path).headers["Location"] == "/"
    t = csrf(c, "/logs/send")
    r = c.post("/logs/send", data={"csrf": t, "note": "The orders page froze after I pressed sync on my phone",
                                   "consent": "yes", "sender": "me@gmail.com"}, headers={"User-Agent": "x BotTrackApp/5"})
    assert r.headers["Location"].endswith("/m/app")
    assert sent[0][0] == "me@gmail.com" and "Sent from a phone: x BotTrackApp/5" in sent[0][1]


def test_other_websites_cannot_post_to_bottrack_on_the_mac(tmp_path, monkeypatch):
    import os
    import stat
    from jigstream.app import create_app
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "s.db"
    app = create_app(str(path))
    c = app.test_client()
    assert c.post("/profiles/clear", data={"confirm": "0"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/sync/stop", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/sync/stop", headers={"Origin": "http://127.0.0.1:8765"}).status_code in (200, 302)
    assert c.post("/sync/stop").status_code in (200, 302)  # a local program
    r = c.get("/")
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
