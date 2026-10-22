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
