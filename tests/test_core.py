import json

from jigstream import analysis, db, emails, parsers, profiles
from jigstream.app import create_app

HOOK_OK = {"username": "Valor AIO", "embeds": [{"title": "Successful Checkout", "timestamp": "2026-01-01T00:00:00Z",
    "fields": [{"name": "Product", "value": "Dunk Low"}, {"name": "Site", "value": "footlocker.com"},
               {"name": "Profile", "value": "||prof1||"}, {"name": "Order #", "value": "ABC12345"}]}]}
HOOK_BAD = {"username": "Stellar", "embeds": [{"title": "Card Declined", "timestamp": "2026-01-02T00:00:00Z",
    "description": "AVS mismatch", "fields": [{"name": "Product", "value": "Dunk Low"},
    {"name": "Site", "value": "footlocker.com"}, {"name": "Profile", "value": "prof2"}]}]}
PROFILES = [{"name": "prof1", "shipping": {"address1": "1 Main Street", "city": "Austin", "state": "TX", "zip": "78701"}},
            {"name": "prof2", "shipping": {"address1": "22 Oak Avenue Apt 4", "city": "Austin", "state": "Texas", "zip": "7870"},
             "billing": {"address1": "23 Oak Ave", "city": "Austin", "state": "TX", "zip": "78701"}}]


def test_pipeline(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    assert parsers.ingest(c, json.dumps([HOOK_OK, HOOK_BAD])) == 2
    assert parsers.ingest(c, json.dumps([HOOK_OK])) == 0  # dedupe
    assert profiles.ingest(c, json.dumps(PROFILES), "Valor") == 2
    rows = {r["profile"]: r for r in c.execute("SELECT * FROM checkouts")}
    assert rows["prof1"]["status"] == "success" and rows["prof1"]["bot"] == "Valor"
    assert rows["prof2"]["status"] == "decline"
    report, unmatched = analysis.failure_report(c)
    assert report[0]["profile"] == "prof2" and report[0]["suggested"]["address2"] == "APT 4"
    assert any("ZIP" in i for i in report[0]["issues"])


def test_profiles_csv():
    csv_text = "Profile Name,First Name,Address 1,City,State,Zip\nA,Bob,5 Elm St,Reno,NV,89501\n"
    assert profiles.parse_profiles(csv_text)[0]["ship_zip"] == "89501"


def test_email_classify():
    assert emails.classify_kind("Your order has been cancelled") == "cancelled"
    assert emails.retailer_for("Target <orders@oe1.target.com>") == "Target"
    assert emails.retailer_for("Some Store <store+123@shopifyemail.com>") == "Shopify"


def test_web(tmp_path):
    app = create_app(str(tmp_path / "w.db"))
    assert app.test_client().get("/").status_code == 200
    for p in ("/checkouts", "/orders", "/profiles", "/tracker", "/import", "/static/app.css", "/static/app.js"):
        assert app.test_client().get(p).status_code == 200


def test_card_data_not_stored(tmp_path):
    c = db.connect(str(tmp_path / "c.db"))
    prof = {"name": "p", "shipping": {"address1": "1 A St", "city": "X", "state": "TX", "zip": "78701"},
            "card": {"number": "4111111111111111", "cvv": "123", "exp": "12/30"}}
    profiles.ingest(c, json.dumps([prof]), "Valor")
    row = c.execute("SELECT * FROM profiles").fetchone()
    assert row["card_last4"] == "1111"
    assert "4111111111111111" not in "".join(str(v) for v in tuple(row)) and '"123"' not in row["raw"]


def test_aycd_style(tmp_path):
    c = db.connect(str(tmp_path / "a.db"))
    data = [{"name": "Group1 #1", "shipping": {"name": "Bob Smith", "email": "b@x.com", "phone": "5125550100",
             "line1": "10 Pine Street", "line2": "Apt 2", "postCode": "78701", "city": "Austin", "state": "TX", "country": "US"},
             "billing": {"name": "Bob Smith", "line1": "10 Pine Street", "postCode": "78701", "city": "Austin", "state": "TX"},
             "payment": {"cardNumber": "4111 1111 1111 9999", "cardCvv": "321", "cardMonth": "01", "cardYear": "30"}}]
    assert profiles.ingest(c, json.dumps(data), "Valor") == 1
    r = c.execute("SELECT * FROM profiles").fetchone()
    assert (r["ship_address1"], r["ship_zip"], r["card_last4"]) == ("10 Pine Street", "78701", "9999")
    assert "4111" not in r["raw"] and "321" not in r["raw"]


def test_tracker(tmp_path):
    from jigstream import tracker
    c = db.connect(str(tmp_path / "t2.db"))
    hooks = [{"username": "Valor", "embeds": [{"title": "Successful Checkout", "timestamp": "2026-02-01T10:00:00Z",
              "fields": [{"name": "Product", "value": "Pokemon Prismatic ETB"}, {"name": "Site", "value": "pokemoncenter.com"},
                         {"name": "Profile", "value": "p1"}]}]},
             {"username": "Valor", "embeds": [{"title": "Successful Checkout", "timestamp": "2026-02-01T11:00:00Z",
              "fields": [{"name": "Product", "value": "Pack"}, {"name": "Site", "value": "target.com"},
                         {"name": "Order #", "value": "912003"}, {"name": "Profile", "value": "p2"}]}]}]
    parsers.ingest(c, json.dumps(hooks))
    emails.store(c, {"message_id": "1", "retailer": "Pokemon Center", "kind": "confirmed", "order_number": "",
                     "product": "", "total": "", "ts": "2026-02-01T10:20:00+00:00", "subject": "Thanks for your order",
                     "sender": "orders@pokemoncenter.com", "store": "Pokemon Center", "body": "Prismatic ETB x1"})
    emails.store(c, {"message_id": "2", "retailer": "Target", "kind": "cancelled", "order_number": "912003",
                     "product": "", "total": "", "ts": "2026-02-02T10:00:00+00:00", "subject": "Order cancelled",
                     "sender": "orders@target.com", "store": "Target", "body": ""})
    c.commit()
    st = {r["checkout"]["site"]: (r["status"], r["matched_by"]) for r in tracker.link(c)}
    assert st["pokemoncenter.com"] == ("confirmed", "retailer+product+time")
    assert st["target.com"] == ("cancelled", "order #")
    assert tracker.retailer_stats(c)["target.com"]["cancel_rate"] == 100


def test_order_number_regex():
    from jigstream.emails import ORDER_RE
    m = ORDER_RE.search("Your order #112-1234567-7654321 has shipped")
    assert (m.group(1) or m.group(2)) == "112-1234567-7654321"
    assert ORDER_RE.search("Your order has been confirmed") is None
    m = ORDER_RE.search("Order #1001 confirmed")
    assert (m.group(1) or m.group(2)) == "1001"


def test_cancel_speed_and_classification(tmp_path):
    from jigstream import tracker
    assert emails.classify_kind("Your order has shipped", "to cancel your order visit...") == "shipped"
    assert emails.classify_kind("Update on your order", "Your order has been canceled.") == "cancelled"
    c = db.connect(str(tmp_path / "x.db"))
    hook = {"username": "Valor", "embeds": [{"title": "Successful Checkout", "timestamp": "2026-03-01T10:00:00Z",
            "fields": [{"name": "Product", "value": "Pack"}, {"name": "Site", "value": "target.com"},
                       {"name": "Order #", "value": "555111"}, {"name": "Profile", "value": "p9"}]}]}
    parsers.ingest(c, json.dumps([hook]))
    emails.store(c, {"message_id": "k", "retailer": "Target", "kind": "cancelled", "order_number": "555111", "product": "",
                     "total": "", "ts": "2026-03-01T10:05:00+00:00", "subject": "Order canceled", "sender": "t@target.com",
                     "store": "Target", "body": ""})
    c.commit()
    r = tracker.link(c)[0]
    assert r["cancel_min"] == 5 and r["cancel_speed"].startswith("instant")
    assert tracker.cancel_by_profile(c)["p9"]["rate"] == 100


def test_updater(tmp_path):
    import subprocess
    from jigstream import updater
    def g(cwd, *a):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True)
    remote, a, b = tmp_path / "r.git", tmp_path / "a", tmp_path / "b"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(a)], check=True, capture_output=True)
    (a / "f.txt").write_text("1"); (a / "requirements.txt").write_text("")
    g(a, "add", "."); g(a, "commit", "-qm", "one"); g(a, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(remote), str(b)], check=True, capture_output=True)
    assert updater.status(str(b))["behind"] == 0
    (a / "f.txt").write_text("2"); g(a, "commit", "-qam", "two"); g(a, "push", "-q", "origin", "HEAD:main")
    st = updater.status(str(b))
    assert st["behind"] == 1 and st["changes"][0].endswith("two")
    assert updater.apply(str(b))["updated"] and (b / "f.txt").read_text() == "2"


def test_llm_layer(tmp_path, monkeypatch):
    from jigstream import llm
    monkeypatch.setattr(llm, "available", lambda: (True, "ok"))
    def fake(system, user, as_json=True):
        if "webhook embed" in system:
            assert "4111111111111111" not in user  # card-like numbers are redacted before reaching the model
            return json.dumps({"status": "success", "product": "Booster Bundle", "site": "target.com",
                               "order_number": "777", "bogus": "x", "profile": 5})
        if "order email" in system:
            return json.dumps({"kind": "cancelled", "order_number": "abc123", "product": "x"})
        return "Billing ZIP differs."
    monkeypatch.setattr(llm, "_chat", fake)
    c = db.connect(str(tmp_path / "l.db"))
    odd = {"username": "Hayha", "embeds": [{"title": "🎉", "description": "card 4111111111111111",
           "fields": [{"name": "stuff", "value": "Booster Bundle target"}], "timestamp": "2026-04-01T00:00:00Z"}]}
    assert parsers.ingest(c, json.dumps([odd]), use_llm=True) == 1
    r = c.execute("SELECT * FROM checkouts").fetchone()
    assert (r["status"], r["site"], r["order_number"], r["ai"], r["profile"]) == ("success", "target.com", "777", 1, "")
    assert llm.classify_email("Update", "text")["order_number"] == "ABC123"
    assert llm.explain_failures({"name": "p"}, ["x"], []) == "Billing ZIP differs."


def test_llm_garbage_output(monkeypatch):
    from jigstream import llm
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: "not json at all")
    assert llm.extract_checkout({})["status"] == "unknown"
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: json.dumps({"kind": "delete everything"}))
    assert llm.classify_email("s", "b")["kind"] == "none"


def test_ai_routes_without_ollama(tmp_path, monkeypatch):
    from jigstream import llm
    monkeypatch.setattr(llm, "HOST", "http://127.0.0.1:9")
    cl = create_app(str(tmp_path / "w2.db")).test_client()
    assert cl.get("/ai/status").get_json()["ok"] is False
    assert cl.get("/").status_code == 200


def test_state_names():
    from jigstream import address
    assert address.suggest("1 A St", "", "Austin", "Texas", "78701")["state"] == "TX"
    assert address.suggest("1 A St", "", "Austin", "ny", "10001")["state"] == "NY"


def test_llm_providers(monkeypatch):
    from jigstream import llm
    seen = {}
    def fake_http(path, payload=None, timeout=120, base=None, headers=None):
        seen.update(path=path, base=base, headers=headers, payload=payload)
        if "chat/completions" in path:
            return {"choices": [{"message": {"content": '{"kind":"shipped"}'}}]}
        return {"content": [{"text": 'Sure:\n```json\n{"kind":"cancelled"}\n```'}]}
    monkeypatch.setattr(llm, "_http", fake_http)
    monkeypatch.setenv("OPENAI_API_KEY", "k1"); monkeypatch.setenv("ANTHROPIC_API_KEY", "k2")
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    assert llm.classify_email("s", "b")["kind"] == "shipped" and seen["headers"]["Authorization"] == "Bearer k1"
    monkeypatch.setattr(llm, "PROVIDER", "anthropic")
    assert llm.classify_email("s", "b")["kind"] == "cancelled" and seen["headers"]["x-api-key"] == "k2"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert llm.available()[0] is False


def test_discord_sync(tmp_path, monkeypatch):
    from jigstream import discord
    c = db.connect(str(tmp_path / "d.db"))
    emb = {"title": "Successful Checkout", "fields": [{"name": "Product", "value": "Pack"}, {"name": "Site", "value": "target.com"},
                                                       {"name": "Profile", "value": "p1"}]}
    pages = {None: [{"id": "30", "author": {"username": "Valor"}, "timestamp": "2026-01-03T00:00:00Z", "embeds": [emb]},
                    {"id": "20", "author": {"username": "chat"}, "embeds": []}],
             "20": [{"id": "10", "author": {"username": "Valor"}, "timestamp": "2026-01-01T00:00:00Z", "embeds": [emb]}]}
    calls = []
    def fake_get(path, token):
        calls.append(path)
        before = path.split("before=")[1] if "before=" in path else None
        return pages.get(before, [])
    monkeypatch.setattr(discord, "_get", fake_get)
    assert discord.sync(c, ["111"], token="t") == (2, 3)
    assert c.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 2
    calls.clear()
    assert discord.sync(c, token="t") == (0, 0)          # incremental: stops at last seen id, uses saved channels
    assert len(calls) == 1
    import pytest
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        discord.sync(c)


def test_env_loader(tmp_path, monkeypatch):
    from jigstream.env import load_env
    f = tmp_path / ".env"
    f.write_text("# c\nFOO_T=abc\nexport BAR_T='x y'\nKEEP_T=file\n")
    monkeypatch.setenv("KEEP_T", "real")
    monkeypatch.delenv("FOO_T", raising=False); monkeypatch.delenv("BAR_T", raising=False)
    load_env(str(f))
    import os
    assert (os.environ["FOO_T"], os.environ["BAR_T"], os.environ["KEEP_T"]) == ("abc", "x y", "real")
    for k in ("FOO_T", "BAR_T"):
        monkeypatch.delenv(k)
