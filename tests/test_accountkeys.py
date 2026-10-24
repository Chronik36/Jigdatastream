import os

from jigstream import accountkeys
from jigstream.app import create_app


def keys(tmp_path, monkeypatch, public):
    f = tmp_path / "pub.txt"
    f.write_text(public)
    monkeypatch.setattr(accountkeys, "PUBLIC_FILE", f)
    monkeypatch.setattr(accountkeys, "revoked_ids", lambda *a: {"dead01"})


def test_keys_are_signed_for_one_email(tmp_path, monkeypatch):
    private, public = accountkeys.new_signing_key()
    keys(tmp_path, monkeypatch, public)
    key, body = accountkeys.issue(private, "Friend@Gmail.com", days=30, note="Mike")
    assert accountkeys.read(key)["e"] == "friend@gmail.com"
    assert accountkeys.check(key, ["friend@gmail.com"]) is None
    assert "is for friend@gmail.com" in accountkeys.check(key, ["someone@gmail.com"])
    assert "ended" in accountkeys.check(key, ["friend@gmail.com"], today="2099-01-01")
    assert "taken back" in accountkeys.check(key, ["friend@gmail.com"], revoked={body["i"]})
    other, _ = accountkeys.new_signing_key()
    forged, _ = accountkeys.issue(other, "friend@gmail.com")
    assert "isn't valid" in accountkeys.check(forged, ["friend@gmail.com"])
    tampered = key[:12] + ("A" if key[12] != "A" else "B") + key[13:]
    assert accountkeys.check(tampered, ["friend@gmail.com"])


def test_the_app_stays_locked_without_a_key(tmp_path, monkeypatch):
    private, public = accountkeys.new_signing_key()
    keys(tmp_path, monkeypatch, public)
    for k in ("BOTTRACK_KEY", "BOTTRACK_KEY_GRACE", "BOTTRACK_SIGNING_KEY", "GMAIL_ACCOUNTS", "GMAIL_ADDRESS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)  # .env lands here
    app = create_app(str(tmp_path / "k.db"))
    c = app.test_client()
    r = c.get("/orders")
    assert r.status_code == 302 and r.headers["Location"].endswith("/activate")
    assert c.get("/activate").status_code == 200
    key, _ = accountkeys.issue(private, "me@gmail.com")
    monkeypatch.setattr("jigstream.mail.check_login", lambda *a: None)
    r = c.post("/activate", data={"key": key, "address": "me@gmail.com", "password": "abcdabcdabcdabcd"})
    assert r.headers["Location"].endswith("/orders")
    assert c.get("/orders").status_code == 200
    assert os.environ["BOTTRACK_KEY"] == key


def test_a_copy_already_in_use_gets_a_week(tmp_path, monkeypatch):
    _, public = accountkeys.new_signing_key()
    keys(tmp_path, monkeypatch, public)
    for k in ("BOTTRACK_KEY", "BOTTRACK_KEY_GRACE", "BOTTRACK_SIGNING_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "old.db").write_bytes(b"x" * 10)  # data from before keys
    accountkeys.start_grace(True)
    s = accountkeys.status([])
    assert s["open"] and s["grace_until"] and s["why"]


def test_keys_off_without_a_public_key(tmp_path, monkeypatch):
    keys(tmp_path, monkeypatch, "")
    assert accountkeys.status([])["open"]
