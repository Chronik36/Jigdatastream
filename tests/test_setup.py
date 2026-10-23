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


def test_release_updates_need_no_token(tmp_path, monkeypatch):
    import io
    import zipfile
    from jigstream import updater
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    app_dir = tmp_path / "app"
    (app_dir / "jigstream").mkdir(parents=True)
    (app_dir / "VERSION").write_text("build-7-aaaaaaa\n")
    release = lambda s: ("build-9-bbbbbbb", "https://example.invalid/bottrack-code.zip", ["New stuff"])  # noqa: E731
    st = updater.check_release(app_dir, latest_release=release)
    assert st["available"] and st["latest"] == "build-9-bbbbbbb" and st["message"] == "New stuff"
    (app_dir / "VERSION").write_text("build-12-ccccccc\n")
    assert not updater.check_release(app_dir, latest_release=release)["available"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bottrack/jigstream/app.py", "# new")
        z.writestr("bottrack/run.py", "")
        z.writestr("bottrack/requirements.txt", "flask\n")
    updater.install(app_dir, sha="build-9-bbbbbbb", blob=buf.getvalue(), pip=False)
    assert (app_dir / "jigstream/app.py").read_text() == "# new"
    assert updater.installed_version(app_dir) == "build-9-bbbbbbb"
    assert updater.build_number("0123abc") == 0
    # Patch notes: the update remembers the version it replaced until they've been seen.
    assert updater.whats_new_pending(app_dir) is None  # 12 -> 9 here is a downgrade: nothing new to show
    (app_dir / "VERSION").write_text("build-14-ddddddd\n")
    assert updater.whats_new_pending(app_dir) == "build-12-ccccccc"
    rels = [{"tag_name": "build-15-e", "body": "- Later"}, {"tag_name": "build-14-d", "published_at": "2026-10-24T01:00:00Z",
             "body": "- Stop sync button\n- Phone fits the screen"}, {"tag_name": "build-13-c", "body": "Drops addresses"},
            {"tag_name": "build-12-c", "body": "- Old"}]
    notes = updater.release_notes(12, 14, fetch=lambda: rels)
    assert notes == [{"build": 14, "date": "2026-10-24", "notes": ["Stop sync button", "Phone fits the screen"]},
                     {"build": 13, "date": "", "notes": ["Drops addresses"]}]
    updater.seen_whats_new(app_dir)
    assert updater.whats_new_pending(app_dir) is None


def test_update_shows_the_loading_screen(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(appmod.updater, "can_self_update", lambda: True)
    monkeypatch.setattr(appmod.updater, "install", lambda: None)
    monkeypatch.setattr(appmod.updater, "restart_soon", lambda: None)
    monkeypatch.setattr(appmod.updater, "backups", lambda: ["app-1"])
    app = appmod.create_app(str(tmp_path / "s.db"))
    c = app.test_client()

    setup = c.get("/setup").get_data(as_text=True)
    assert 'id="loader"' in setup and 'data-loader="Installing the update"' in setup
    page = c.post("/update/install").get_data(as_text=True)
    assert 'class="loader on"' in page and "Updated" in page and "/static/loader/lines.png" in page
    for name in ("logo.png", "lines.png"):
        assert c.get(f"/static/loader/{name}").status_code == 200


def test_stop_sync_and_update_pauses_a_running_sync(tmp_path, monkeypatch):
    import threading
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GMAIL_ACCOUNTS", "a@gmail.com:x")
    monkeypatch.setattr(appmod.updater, "can_self_update", lambda: True)
    monkeypatch.setattr(appmod.updater, "restart_soon", lambda: None)
    reading, release = threading.Event(), threading.Event()

    def slow_sync(config, store, log=None, progress=None, stop=None):
        reading.set()
        release.wait(5)  # the email being read when Stop is pressed
        return {"order": 0, "botting_expense": 0, "other": 0, "skipped": 0, "error": 0,
                "failed_inboxes": [], "stopped": stop()}

    monkeypatch.setattr(appmod, "run_sync", slow_sync)
    app = appmod.create_app(str(tmp_path / "s.db"))
    c = app.test_client()
    sync = app.config["SYNC_STATE"]

    assert app.start_sync() and reading.wait(5)
    assert "Stop sync" in c.get("/orders").get_data(as_text=True)
    c.post("/sync/stop")
    assert sync["stop"].is_set() and "Stopping..." in c.get("/orders").get_data(as_text=True)
    release.set()
    for _ in range(50):
        if not sync["running"]:
            break
        threading.Event().wait(0.1)
    assert not sync["running"] and sync["last"].startswith("Sync stopped.")
    assert "Stop sync" not in c.get("/orders").get_data(as_text=True)

    # Update now while a backfill runs: the sync stops, the update installs, and the backfill resumes after.
    installed = []
    monkeypatch.setattr(appmod.updater, "install", lambda: installed.append(sync["running"]))
    reading.clear(), release.clear()
    assert app.start_sync(full=True) and reading.wait(5)
    threading.Timer(0.3, release.set).start()
    assert "Updated" in c.post("/update/install").get_data(as_text=True)
    assert installed == [False]  # it waited for the sync to stop
    assert (tmp_path / "resume-sync").read_text() == "full"
    reading.clear()
    assert app.start_sync() and sync["full"] and not (tmp_path / "resume-sync").exists()
    release.set()


def test_window_splash_is_self_contained():
    from jigstream.window import splash

    page = splash("Starting BotTrack", "Getting things <ready>.")
    assert page.count("data:image/png;base64,") == 2 and "/static/" not in page
    assert "Getting things &lt;ready&gt;." in page and 'class="loader on"' in page


def test_dashboard_shows_the_year_and_imports_have_their_own_page(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = appmod.create_app(str(tmp_path / "s.db"))
    c = app.test_client()
    page = c.get("/").get_data(as_text=True)
    assert "Spent in" in page and "Successful checkouts" in page and "/upload/profiles" not in page
    imports = c.get("/imports").get_data(as_text=True)
    assert 'action="/upload/profiles"' in imports and 'action="/upload/webhooks"' in imports
    assert c.post("/upload/webhooks").headers["Location"].endswith("/imports")
    assert 'action="/sync/all"' in page  # Sync everything is on every page


def test_sync_everything_starts_each_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = appmod.create_app(str(tmp_path / "s.db"))
    started = []
    app.start_sync = lambda full=False: started.append("email") or True
    app.config["SYNC_ALL"] = [("linked banks", lambda: started.append("banks") or True)]
    monkeypatch.setattr(appmod.carriers, "available", lambda env: False)
    c = app.test_client()
    c.post("/sync/all")
    assert started == ["email", "banks"]
    assert "Syncing everything: email and Discord, linked banks." in c.get("/").get_data(as_text=True)


def test_patch_notes_show_once_after_an_update(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pending = {"v": "build-60-a"}
    monkeypatch.setattr(appmod.updater, "whats_new_pending", lambda: pending["v"])
    monkeypatch.setattr(appmod.updater, "installed_version", lambda: "build-62-b")
    monkeypatch.setattr(appmod.updater, "release_notes", lambda a, b: [{"build": b, "date": "", "notes": [f"from {a}"]}])
    monkeypatch.setattr(appmod.updater, "seen_whats_new", lambda: pending.update(v=None))
    c = appmod.create_app(str(tmp_path / "s.db")).test_client()
    assert 'id="wnew"' in c.get("/orders").get_data(as_text=True)
    assert c.get("/whats-new").get_json()["releases"] == [{"build": 62, "date": "", "notes": ["from 60"]}]
    c.post("/whats-new/seen")
    assert 'id="wnew"' not in c.get("/orders").get_data(as_text=True)
