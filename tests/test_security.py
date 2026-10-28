import os
import subprocess

import pytest
from cryptography.fernet import Fernet

from jigstream import config, logs, mobile, wallet
from jigstream.config import load_dotenv, plain_text, save_env
from test_mobile import app, pair, phone, client  # noqa: F401  (the app fixture)


def test_secrets_in_env_are_sealed_and_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if not k.startswith(("PHONE", "GMAIL"))})
    env = tmp_path / ".env"
    env.write_text("GMAIL_ACCOUNTS=me@gmail.com:abcdefghijklmnop\nPHONE_PAIR_KEY=pairkey1234567890\nPORT=8765\n")
    load_dotenv(env)
    text = env.read_text()
    assert "abcdefghijklmnop" not in text and "pairkey1234567890" not in text and "PORT=8765" in text
    assert os.environ["GMAIL_ACCOUNTS"] == "me@gmail.com:abcdefghijklmnop"
    assert oct(env.stat().st_mode & 0o777) == "0o600"

    # The next start opens the sealed values again.
    for k in ("GMAIL_ACCOUNTS", "PHONE_PAIR_KEY", "PORT"):
        os.environ.pop(k)
    monkeypatch.setattr(config, "_sealed_checked", set())
    load_dotenv(env)
    assert os.environ["PHONE_PAIR_KEY"] == "pairkey1234567890" and env.read_text() == text

    # A new secret is written sealed; a setting that isn't secret stays readable.
    save_env({"BACKUP_PASSWORD": "correct horse", "UI_THEME": "light"}, env)
    assert "correct horse" not in env.read_text() and "UI_THEME=light" in env.read_text()
    assert "BACKUP_PASSWORD=correct horse" in plain_text(env)


def test_a_card_key_that_does_not_open_them_leaves_the_file_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if not k.startswith(("PHONE", "GMAIL"))})
    env = tmp_path / ".env"
    sealed = "sealed:" + Fernet(Fernet.generate_key()).encrypt(b"me@gmail.com:pw").decode()
    env.write_text(f"GMAIL_ACCOUNTS={sealed}\n")
    load_dotenv(env)
    assert "GMAIL_ACCOUNTS" not in os.environ and env.read_text() == f"GMAIL_ACCOUNTS={sealed}\n"


def test_logs_hide_pairing_code_channels_and_link(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ, PHONE_PAIR_KEY="Zx9_pairing-key-0123456789abcd",
                                            PHONE_PAIR_TOPIC="bottrack-0123456789abcdef0123456789abcdef",
                                            BOTTRACK_SIGNING_KEY="c2lnbmluZy1rZXktc2VjcmV0LXZhbHVlLTEyMzQ1"))
    text = logs.scrub("posted to ntfy.sh/bottrack-0123456789abcdef0123456789abcdef-alerts "
                      "link https://quiet-river-words.trycloudflare.com/m/pair?k=Zx9_pairing-key-0123456789abcd&t=x "
                      "key c2lnbmluZy1rZXktc2VjcmV0LXZhbHVlLTEyMzQ1 and GET /m/pair?k=AnotherKeyFromBefore123")
    assert "0123456789abcdef" not in text and "trycloudflare" not in text and "Zx9_" not in text
    assert "c2lnbmlu" not in text and "AnotherKeyFromBefore" not in text and "[phone link]" in text


class FakeKeychain:
    def __init__(self, find_code, stored=""):
        self.find_code, self.stored, self.added = find_code, stored, []

    def __call__(self, *args):
        if args[0] == "find-generic-password":
            return subprocess.CompletedProcess(args, self.find_code, self.stored, "")
        self.added.append(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")


def _on_a_mac(tmp_path, monkeypatch, keychain):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOTTRACK_CARD_KEY_FILE")
    monkeypatch.setattr(wallet.sys, "platform", "darwin")
    monkeypatch.setattr(wallet, "_keychain", keychain)


def test_a_keychain_that_does_not_answer_never_replaces_the_card_key(tmp_path, monkeypatch):
    keychain = FakeKeychain(51)  # locked or refused, not "no such item"
    _on_a_mac(tmp_path, monkeypatch, keychain)
    with pytest.raises(RuntimeError, match="Keychain"):
        wallet.key()
    assert keychain.added == [] and not (tmp_path / ".card-key").exists()


def test_a_key_file_from_before_moves_into_the_keychain(tmp_path, monkeypatch):
    keychain = FakeKeychain(wallet.KEYCHAIN_NOT_FOUND)
    _on_a_mac(tmp_path, monkeypatch, keychain)
    old = Fernet.generate_key()
    (tmp_path / ".card-key").write_bytes(old)
    assert wallet.key() == old and keychain.added == [old.decode()]


def test_phone_cookie_is_https_only_through_the_link(app):  # noqa: F811
    c = phone(app)
    r = pair(c)
    assert "Secure" in r.headers.get("Set-Cookie", "")
    local = client(app, "127.0.0.1")
    r = local.get("/")
    assert "Secure" not in (r.headers.get("Set-Cookie") or "")
    assert isinstance(app.session_interface, mobile.LinkSessions)
