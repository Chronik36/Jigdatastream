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
