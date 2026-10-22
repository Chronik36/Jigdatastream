import io
import json
import zipfile

from jigstream import db, export, profiles, updater
from jigstream.app import create_app

STELLAR = [{"profileName": "Main VCC", "email": "j@x.com", "phone": "5125550100",
            "shipping": {"firstName": "Jay", "lastName": "B", "address1": "10 Pine St", "address2": "Apt 2",
                         "city": "Austin", "state": "TX", "zip": "78701", "country": "US"},
            "billing": {"firstName": "Jay", "lastName": "B", "address1": "10 Pine St", "address2": "Apt 2",
                        "city": "Austin", "state": "TX", "zip": "78701", "country": "US"},
            "sameBillingAndShippingAddress": True, "id": "abc",
            "payment": {"cardHolder": "Jay B", "cardNumber": "371449635398431", "cardMonth": "07",
                        "cardYear": "2031", "cardCvv": "1234"}}]
VALOR = [{"name": "Other", "first_name": "Al", "last_name": "C", "email": "a@x.com", "phone": "1",
          "shipping_address1": "5 Elm Rd", "shipping_city": "Reno", "shipping_state": "Nevada", "shipping_zip": "89501",
          "card_number": "4111111111111111", "card_cvv": "999", "card_exp": "01/30"}]


def test_card_meta_and_scrub(tmp_path):
    c = db.connect(str(tmp_path / "p.db"))
    profiles.ingest(c, json.dumps(STELLAR), "Stellar")
    r = c.execute("SELECT * FROM profiles").fetchone()
    assert (r["card_brand"], r["card_last4"], r["card_exp"]) == ("Amex", "8431", "07/31")
    raw = json.loads(r["raw"])
    assert raw["payment"]["cardNumber"] == "" and raw["payment"]["cardCvv"] == ""  # keys kept, values gone
    assert "371449635398431" not in r["raw"] and "1234" not in r["raw"]


def test_export_uses_bot_layout_and_carries_cards(tmp_path):
    c = db.connect(str(tmp_path / "e.db"))
    profiles.ingest(c, json.dumps(STELLAR), "Stellar")
    profiles.ingest(c, json.dumps(VALOR), "Valor")
    fmt, template = export.templates(c)["Stellar"]
    rows = c.execute("SELECT * FROM profiles ORDER BY name").fetchall()
    # without the original files, card numbers stay blank
    _, text = export.export(rows, fmt, template)
    out = {p["profileName"]: p for p in json.loads(text)}
    assert out["Other"]["shipping"]["address1"] == "5 Elm Rd" and out["Other"]["shipping"]["state"] == "NV"
    assert out["Other"]["payment"]["cardNumber"] == "" and out["Other"]["sameBillingAndShippingAddress"] is True
    assert out["Other"]["id"] != "abc"
    # with the Valor export uploaded again, its card comes along in Stellar's layout
    _, text = export.export(rows, fmt, template, export.cards_from([json.dumps(VALOR)]))
    other = {p["profileName"]: p for p in json.loads(text)}["Other"]
    assert other["payment"]["cardNumber"] == "4111111111111111" and other["payment"]["cardYear"] == "2030"
    assert other["payment"]["cardMonth"] == "01" and other["payment"]["cardCvv"] == "999"


def test_profile_pages(tmp_path):
    path = str(tmp_path / "w.db")
    profiles.ingest(db.connect(path), json.dumps(STELLAR), "Stellar")
    client = create_app(path).test_client()
    page = client.get("/profiles").get_data(as_text=True)
    assert "Main VCC" in page and "Same as billing" in page and "Amex •••• 8431" in page
    assert client.get("/profiles/Main VCC/edit").status_code == 200
    form = {k: "" for k in ("bot", "first_name", "last_name", "phone", "email", "ship_address2", "ship_country")}
    form.update(name="Main VCC 2", ship_address1="1 New St", ship_city="Austin", ship_state="Texas", ship_zip="78702", same="on")
    assert client.post("/profiles/Main VCC/edit", data=form).status_code == 302
    r = db.connect(path).execute("SELECT * FROM profiles").fetchone()
    assert (r["name"], r["bill_address1"], r["bill_state"]) == ("Main VCC 2", "1 New St", "Texas")
    resp = client.post("/profiles/export", data={"bot": "Stellar"})
    assert json.loads(resp.data)[0]["shipping"]["address1"] == "1 New St"
    client.post("/profiles/Main VCC 2/delete")
    assert db.connect(path).execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0


def _zipball(version_text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Chronik36-Jigdatastream-abc1234/", "")
        z.writestr("Chronik36-Jigdatastream-abc1234/README.md", "x")
        z.writestr("Chronik36-Jigdatastream-abc1234/run.py", "print('run')")
        z.writestr("Chronik36-Jigdatastream-abc1234/requirements.txt", "flask>=3.0\n")
        z.writestr("Chronik36-Jigdatastream-abc1234/jigstream/app.py", version_text)
    return buf.getvalue()


def test_update_install_and_rollback(tmp_path):
    home = tmp_path / "OrderTracker"
    app_dir = home / "app"
    (app_dir / "jigstream").mkdir(parents=True)
    (app_dir / "jigstream" / "app.py").write_text("old")
    (app_dir / "VERSION").write_text("old111\n")
    (home / "data").mkdir()
    (home / "data" / "jigstream.db").write_text("keep me")
    updater.install(app_dir, sha="new222", blob=_zipball("new"), pip=False)
    assert (app_dir / "jigstream" / "app.py").read_text() == "new"
    assert updater.installed_version(app_dir) == "new222" and not (app_dir / "README.md").exists()
    assert (home / "data" / "jigstream.db").read_text() == "keep me"
    assert len(updater.backups(app_dir)) == 1
    updater.rollback(app_dir)
    assert (app_dir / "jigstream" / "app.py").read_text() == "old" and updater.installed_version(app_dir) == "old111"


def test_bad_download_changes_nothing(tmp_path):
    app_dir = tmp_path / "app"
    (app_dir / "jigstream").mkdir(parents=True)
    (app_dir / "jigstream" / "app.py").write_text("old")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/README.md", "not the app")
    try:
        updater.install(app_dir, sha="bad", blob=buf.getvalue(), pip=False)
    except RuntimeError:
        pass
    assert (app_dir / "jigstream" / "app.py").read_text() == "old"


def test_check_compares_commits(tmp_path):
    (tmp_path / "VERSION").write_text("aaa\n")
    head = {"sha": "bbb", "commit": {"message": "Better profiles\n\nbody", "committer": {"date": "2026-10-23T00:00:00Z"}}}
    base = {"commit": {"committer": {"date": "2026-10-22T00:00:00Z"}}}
    calls = {"commits/main": head, "compare/aaa...bbb": {"status": "ahead", "base_commit": base}}
    s = updater.check(tmp_path, api=calls.get)
    assert s["available"] and s["message"] == "Better profiles" and not s["error"]
    calls["compare/aaa...bbb"] = {"status": "behind", "base_commit": base}
    assert not updater.check(tmp_path, api=calls.get)["available"]  # main is older than this build
    (tmp_path / "VERSION").write_text("bbb\n")
    assert not updater.check(tmp_path, api=calls.get)["available"]

    def private(path):
        raise RuntimeError("Can't see the update source.")
    assert "Can't see" in updater.check(tmp_path, api=private)["error"]


def test_mac_app_moves_old_install(tmp_path, monkeypatch):
    from jigstream import macapp
    monkeypatch.setattr(macapp, "HOME", tmp_path)
    old, new = tmp_path / "OrderTracker", tmp_path / "BotTrack"
    (old / "data").mkdir(parents=True)
    (old / "data" / "jigstream.db").write_text("orders")
    (old / ".env").write_text("GMAIL_ACCOUNTS=a@b.com:pw\nLLM_MODEL=llama3.1:8b\nPORT=8765\n")
    (tmp_path / "Applications" / "Order Tracker.app").mkdir(parents=True)
    calls = []
    assert macapp.migrate_old_install(new, old, run=lambda *a, **k: calls.append(a))
    assert (new / "data" / "jigstream.db").read_text() == "orders"
    assert "GMAIL_ACCOUNTS" in (new / ".env").read_text() and "llama3.1" not in (new / ".env").read_text()
    assert not old.exists() and not (tmp_path / "Applications" / "Order Tracker.app").exists()
    assert "bootout" in calls[0][0]
    assert not macapp.migrate_old_install(new, old)  # only once


def test_mac_app_reuses_model_from_ollama(tmp_path):
    from jigstream import macapp
    src, dst = tmp_path / "ollama", tmp_path / "models"
    m = src / "manifests/registry.ollama.ai/huihui_ai/qwen3-abliterated/14b"
    m.parent.mkdir(parents=True)
    m.write_text(json.dumps({"config": {"digest": "sha256:aa"}, "layers": [{"digest": "sha256:bb"}]}))
    (src / "blobs").mkdir()
    (src / "blobs" / "sha256-aa").write_text("cfg")
    (src / "blobs" / "sha256-bb").write_text("weights")
    assert macapp.copy_model_from_ollama("huihui_ai/qwen3-abliterated:14b", dst, src)
    assert (dst / "blobs" / "sha256-bb").read_text() == "weights"
    assert (dst / "manifests/registry.ollama.ai/huihui_ai/qwen3-abliterated/14b").exists()
    assert not macapp.copy_model_from_ollama("qwen3:8b", dst, src)


def test_first_update_and_undo_in_mac_app(tmp_path, monkeypatch):
    code = tmp_path / "BotTrack" / "app"  # nothing there yet: the app runs the code inside its bundle
    monkeypatch.setenv("BOTTRACK_CODE_DIR", str(code))
    updater.install(code, sha="new333", blob=_zipball("new"), pip=False)
    assert (code / "jigstream" / "app.py").read_text() == "new"
    updater.rollback(code)  # back to the bundled code
    assert not code.exists()


def test_ai_progress_banner(tmp_path):
    app = create_app(str(tmp_path / "a.db"))
    app.config["AI_STATE"] = {"ready": False, "message": "Downloading the AI model", "percent": 42, "error": None}
    c = app.test_client()
    assert 'id="aibar"' in c.get("/orders").get_data(as_text=True) and c.get("/api/ai").get_json()["percent"] == 42
    app.config["AI_STATE"].update(ready=True)
    assert 'id="aibar"' not in c.get("/orders").get_data(as_text=True)


def test_cleanup_keeps_user_data(tmp_path):
    import os, time as _t
    from jigstream import macapp
    d = tmp_path / "BotTrack"
    for name in ("app-20260101-000000-aaa", "app-20260201-000000-bbb", "app-20260301-000000-ccc"):
        (d / "backups" / name).mkdir(parents=True)
    (d / "pydeps" / "flask").mkdir(parents=True)
    (d / "data").mkdir()
    (d / "data" / "jigstream.db").write_text("orders")
    (d / ".env").write_text("GMAIL_ACCOUNTS=x")
    (d / "logs").mkdir()
    old_log = d / "logs" / "install.log"
    old_log.write_text("old")
    os.utime(old_log, (_t.time() - 40 * 86400,) * 2)
    big = d / "logs" / "ai-engine.log"
    big.write_bytes(b"x" * (macapp.LOG_KEEP_BYTES + 10))
    (tmp_path / "Applications" / "Order Tracker.app").mkdir(parents=True)
    macapp.cleanup(d, tmp_path, running_bundled_code=True)
    assert [p.name for p in (d / "backups").iterdir()] == ["app-20260301-000000-ccc"]
    assert not (d / "pydeps").exists() and not old_log.exists() and big.stat().st_size < macapp.LOG_KEEP_BYTES
    assert (d / "data" / "jigstream.db").read_text() == "orders" and (d / ".env").exists()
    assert not (tmp_path / "Applications" / "Order Tracker.app").exists()
