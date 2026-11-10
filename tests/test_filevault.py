import os

import pytest

import jigstream.app as appmod
from jigstream import filevault


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if not k.startswith(("FILEVAULT", "PHONE"))})
    monkeypatch.setattr(appmod, "installed_models", lambda url: [])
    monkeypatch.setattr(filevault.sys, "platform", "darwin")
    return appmod.create_app(str(tmp_path / "t.db"))


def mac(app):
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    return c


def test_filevault_off_blocks_the_app_until_it_is_on(app, monkeypatch):
    state = {"on": False}
    monkeypatch.setattr(filevault, "_ask", lambda: state["on"])
    c = mac(app)
    r = c.get("/orders")
    assert r.status_code == 302 and r.headers["Location"].startswith("/filevault")
    page = c.get("/filevault?next=/orders").get_data(as_text=True)
    assert "FileVault is strongly recommended" in page and "Remind me tomorrow" not in page
    assert c.post("/filevault/later", data={"next": "/orders"}).headers["Location"].endswith("/filevault")
    assert c.get("/filevault/status").get_json() == {"on": False}
    state["on"] = True
    assert c.get("/filevault/status").get_json() == {"on": True}
    assert c.get("/orders").status_code == 200


def test_unknown_status_or_other_systems_never_lock_anyone_out(app, monkeypatch):
    monkeypatch.setattr(filevault, "_ask", lambda: None)
    filevault._cache["at"] = 0
    assert mac(app).get("/orders").status_code == 200
    monkeypatch.setattr(filevault.sys, "platform", "win32")
    assert filevault.is_on(fresh=True) is None and not filevault.needs_screen()


def test_reading_fdesetup(monkeypatch):
    import subprocess
    for out, want in (("FileVault is On.\n", True), ("FileVault is Off.\n", False),
                      ("Encryption in progress: Percent completed = 12\n", True), ("", None)):
        monkeypatch.setattr(filevault.subprocess, "run",
                            lambda *a, out=out, **k: subprocess.CompletedProcess(a, 0, out, ""))
        assert filevault._ask() is want


def test_i_understand_lets_a_headless_mac_run_without_filevault(app, monkeypatch):
    monkeypatch.setattr(filevault, "_ask", lambda: False)
    filevault._cache["at"] = 0
    c = mac(app)
    page = c.get("/filevault?next=/orders").get_data(as_text=True)
    assert "Use BotTrack without FileVault" in page and "fdesetup authrestart" in page
    # the box has to be ticked
    assert c.post("/filevault/accept", data={"next": "/orders"}).headers["Location"].startswith("/filevault")
    assert c.get("/orders").status_code == 302
    r = c.post("/filevault/accept", data={"next": "/orders", "understand": "yes"})
    assert r.headers["Location"].endswith("/orders")
    assert os.environ.get("FILEVAULT_ACCEPTED") and "FILEVAULT_ACCEPTED=" in open(".env").read()
    orders = c.get("/orders")
    assert orders.status_code == 200 and "FileVault is off on this Mac" not in orders.get_data(as_text=True)
    assert "running without FileVault" in c.get("/filevault").get_data(as_text=True)
    c.post("/filevault/ask-again")
    assert not filevault.accepted() and c.get("/orders").status_code == 302


def test_a_phone_cannot_choose_to_skip_filevault(app, monkeypatch):
    monkeypatch.setattr(filevault, "_ask", lambda: False)
    filevault._cache["at"] = 0
    phone = app.test_client()
    phone.environ_base["REMOTE_ADDR"] = "192.168.1.50"
    assert "Use BotTrack without FileVault" not in phone.get("/filevault").get_data(as_text=True)
    phone.post("/filevault/accept", data={"understand": "yes"})
    assert not filevault.accepted()
