import os

import jigstream.app as appmod
from jigstream.config import Config, save_env


def test_save_env_updates_in_place(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nLLM_MODEL=old\nOTHER=1\n")
    monkeypatch.setattr(os, "environ", dict(os.environ))
    save_env({"LLM_MODEL": "qwen3:8b", "GMAIL_ACCOUNTS": "a@gmail.com:pw"}, path=env)
    assert env.read_text() == "# comment\nLLM_MODEL=qwen3:8b\nOTHER=1\nGMAIL_ACCOUNTS=a@gmail.com:pw\n"
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert os.environ["LLM_MODEL"] == "qwen3:8b"


def test_setup_adds_and_removes_inbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items()
                                        if not k.startswith(("GMAIL", "LLM"))})
    monkeypatch.setattr(appmod, "installed_models", lambda url: ["huihui_ai/qwen3-abliterated:14b"])
    logins = []

    def fake_login(config, address, password):
        logins.append((address, password))
        return None if password == "goodpassword1234" else "Gmail rejected that address and app password."

    monkeypatch.setattr(appmod, "check_login", fake_login)
    app = appmod.create_app(str(tmp_path / "s.db"))
    started = []
    app.start_sync = lambda full=False: started.append(full)  # route uses the closure; just avoid real sync
    c = app.test_client()

    page = c.get("/setup").get_data(as_text=True)
    assert "No inboxes yet" in page and "Using huihui_ai/qwen3-abliterated:14b" in page
    assert "No Gmail inbox connected yet" in c.get("/orders").get_data(as_text=True)

    r = c.post("/setup/inbox", data={"address": "me@gmail.com", "password": "wrong"}, follow_redirects=True)
    assert "rejected" in r.get_data(as_text=True) and not (tmp_path / ".env").exists()

    monkeypatch.setattr(appmod, "run_sync", lambda *a, **k: {"order": 0, "botting_expense": 0, "other": 0,
                                                              "skipped": 0, "error": 0, "failed_inboxes": []})
    c.post("/setup/inbox", data={"address": "me@gmail.com", "password": "good password 1234"})
    assert "GMAIL_ACCOUNTS=me@gmail.com:goodpassword1234" in (tmp_path / ".env").read_text()
    assert Config.from_env().accounts == [("me@gmail.com", "goodpassword1234")]
    assert "me@gmail.com" in c.get("/setup").get_data(as_text=True)

    c.post("/setup/inbox/remove", data={"address": "me@gmail.com"})
    assert Config.from_env().accounts == []


def test_qwen3_gets_no_think(monkeypatch):
    import io, json as _json
    from jigstream.llm import LLMClient
    sent = {}

    def fake(req, timeout):
        sent.update(_json.loads(req.data))
        return io.BytesIO(_json.dumps({"choices": [{"message": {"content": '<think></think>{"kind": "other"}'}}]}).encode())
    monkeypatch.setattr("urllib.request.urlopen", fake)
    c = LLMClient("http://x/v1", "huihui_ai/qwen3-abliterated:14b", "k", 5)
    assert c.chat_json("sys", "email")["kind"] == "other"
    assert sent["messages"][-1]["content"].endswith("/no_think")
