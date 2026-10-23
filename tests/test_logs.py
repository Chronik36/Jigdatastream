import io
import logging
import os
import zipfile

from jigstream import logs
from jigstream.app import create_app


def test_passwords_never_reach_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ, GMAIL_ACCOUNTS="me@gmail.com:abcdefghijklmnop"))
    monkeypatch.setattr(logs, "LOG_DIR", tmp_path)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        logs.setup_logging(tmp_path)
        logging.getLogger("jigstream.test").warning("login failed with abcd efgh ijkl mnop for me@gmail.com")
        for h in root.handlers:
            h.flush()
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    text = (tmp_path / "app.log").read_text()
    assert "me@gmail.com" in text and "abcd" not in text and "********" in text

    (tmp_path / "install.log").write_text("pw=abcdefghijklmnop\n")
    z = zipfile.ZipFile(io.BytesIO(logs.zip_logs(tmp_path)))
    assert sorted(z.namelist()) == ["app.log", "install.log"]
    assert "abcdefghijklmnop" not in z.read("install.log").decode()

    client = create_app(str(tmp_path / "w.db")).test_client()
    page = client.get("/logs?file=install.log").get_data(as_text=True)
    assert "pw=********" in page
    assert client.get("/logs?file=../../etc/passwd").status_code == 200  # unknown names fall back
    assert client.get("/logs/download").mimetype == "application/zip"


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def login(self, user, password):
        self.login_as = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def test_scrub_masks_cards_and_tokens_but_not_tracking():
    text = logs.scrub("card 4111-1111-1111-1111 tracking 1234567890123 access-production-1a2b3c4d-5e6f bearer abcdefghijklmnopqr")
    assert "4111" not in text and "[card]" in text
    assert "1234567890123" in text  # not a valid card number, kept for debugging
    assert "access-production" not in text and "abcdefghijklmnopqr" not in text


def test_send_logs_emails_scrubbed_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ, GMAIL_ACCOUNTS="me@gmail.com:abcdefghijklmnop"))
    (tmp_path / "app.log").write_text("paid with 4111 1111 1111 1111 using abcdefghijklmnop\n")
    smtp = FakeSMTP()
    to = logs.send_logs("me@gmail.com", "abcdefghijklmnop", "it crashed when I pressed Sync now", {"BotTrack": "build-9-abc"},
                        log_dir=tmp_path, to="dev@example.com", smtp=smtp)
    assert to == "dev@example.com" and smtp.login_as == ("me@gmail.com", "abcdefghijklmnop")
    msg = smtp.sent[0]
    assert msg["To"] == "dev@example.com" and "build-9-abc" in msg["Subject"]
    assert "it crashed when I pressed Sync now" in msg.get_body(("plain",)).get_content()
    att = next(msg.iter_attachments())
    z = zipfile.ZipFile(io.BytesIO(att.get_payload(decode=True)))
    assert sorted(z.namelist()) == ["about.txt", "app.log", "problem.txt"]
    assert "pressed Sync now" in z.read("problem.txt").decode()
    log = z.read("app.log").decode()
    assert "4111" not in log and "abcdefghijklmnop" not in log


def test_send_logs_page_needs_consent(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ, GMAIL_ACCOUNTS="me@gmail.com:abcdefghijklmnop",
                                            BOTTRACK_LOGS_TO="dev@example.com"))
    monkeypatch.setattr(logs, "LOG_DIR", tmp_path)
    (tmp_path / "app.log").write_text("hello\n")
    sent = []
    monkeypatch.setattr(logs, "send_logs", lambda *a, **k: sent.append(a) or "dev@example.com")
    client = create_app(str(tmp_path / "w.db")).test_client()
    page = client.get("/logs/send").get_data(as_text=True)
    assert "dev@example.com" in page and "app.log" in page and "me@gmail.com" in page
    long_note = "Target orders never show up after Sync now"
    client.post("/logs/send", data={"note": long_note, "sender": "me@gmail.com"})
    assert not sent  # no consent
    page = client.post("/logs/send", data={"note": "broken", "consent": "yes", "sender": "me@gmail.com"}).get_data(as_text=True)
    assert not sent and "at least 20 characters" in page and ">broken</textarea>" in page  # kept what they typed
    client.post("/logs/send", data={"note": long_note, "consent": "yes", "sender": "me@gmail.com"})
    assert sent and sent[0][:3] == ("me@gmail.com", "abcdefghijklmnop", long_note)


def test_send_logs_page_without_address(tmp_path, monkeypatch):
    env = dict(os.environ)
    env.pop("BOTTRACK_LOGS_TO", None)
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(logs, "DEVELOPER_EMAIL", "")
    page = create_app(str(tmp_path / "w.db")).test_client().get("/logs/send").get_data(as_text=True)
    assert "No developer address" in page


def test_send_logs_requires_a_note(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        logs.send_logs("me@gmail.com", "pw", "   short   ", log_dir=tmp_path, to="dev@example.com", smtp=FakeSMTP())
