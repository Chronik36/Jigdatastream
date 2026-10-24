import json

from jigstream import db, proxyaccounts, proxylogin, proxysessions as ps
from jigstream.app import create_app

PAGE = "https://dash.prov.com/generate"


def step(act, text="", label="", value=None, page=PAGE, at=0, **el):
    s = {"act": act, "el": {"tag": "BUTTON" if act == "click" else "INPUT", "text": text, "label": label, "index": 1, **el},
         "page": page, "at": at}
    if value is not None:
        s["value"] = value
    return s


def test_clean_keeps_the_dashboard_steps_after_signing_in():
    steps = [step("click", "Log in with Discord", page="https://dash.prov.com/login", at=1),
             step("click", "Authorize", page="https://discord.com/oauth2/authorize", at=2),
             step("click", "Sticky", at=3),
             step("type", label="Minutes", value="3", at=4), step("type", label="Minutes", value="30", at=5),
             step("click", "Generate", at=6), step("click", "Copy", at=60000)]
    out, any_ttl = ps.clean(steps, "https://dash.prov.com", until=6, ttl=30)
    assert [ps.describe(s) for s in out] == ['clicked "Sticky"', 'typed the TTL in "Minutes"', 'clicked "Generate"']
    assert any_ttl and out[1]["value"] == "{ttl}"
    assert ps.fill(out, 90)[1]["value"] == "90" and out[1]["value"] == "{ttl}"


def test_choice_and_labels():
    assert ps.choice({"session": "rotating", "ttl": "30"}) == ("rotating", 0)
    assert ps.choice({"session": "sticky", "ttl": "other", "ttl_other": "45"}) == ("sticky", 45)
    assert ps.choice({"session": "first"}) == (None, 0)
    assert ps.label("sticky", 1440) == "Sticky · 1 day" and ps.label("sticky", 120) == "Sticky · 2 h" and ps.label("rotating") == "Rotating"


def test_direct_only_when_the_request_names_the_session():
    req = {"kind": "request", "url": "https://dash.prov.com/api/gen", "body": '{"type": "sticky", "ttl": 30}'}
    steps = [step("click", "Sticky"), step("click", "Generate")]
    assert ps.direct_ok(req, steps, "sticky", 30)
    assert not ps.direct_ok(dict(req, body='{"qty": 5}'), steps, "sticky", 30)  # the choice lives in the page
    assert ps.direct_ok(dict(req, body='{"qty": 5}'), [step("click", "Generate")], "sticky", 30)
    assert not ps.direct_ok({"kind": "click"}, steps, "sticky", 30)


def _db(tmp_path):
    con = ps.ensure(proxyaccounts.ensure(db.connect(str(tmp_path / "a.db"))))
    con.execute("INSERT INTO proxy_accounts (name, auth, site_url) VALUES ('Hype', 'site', 'https://dash.prov.com')")
    return con


def test_saved_settings_are_found_and_typed_ttls_work_for_any_minutes(tmp_path):
    con = _db(tmp_path)
    ps.save(con, 1, "sticky", 30, {"kind": "click", "page": PAGE, "at": 9},
            [step("click", "Sticky", at=1), step("type", label="Minutes", value="30", at=2), step("click", "Generate", at=3)],
            "https://dash.prov.com")
    ps.save(con, 1, "rotating", 0, {"kind": "request", "url": "https://dash.prov.com/api/gen?mode=rotating", "at": 9},
            [step("click", "Rotating", at=1), step("click", "Generate", at=2)], "https://dash.prov.com")
    got = ps.find(con, 1, "sticky", 45)
    assert got["ttl"] == 30 and [s.get("value") for s in got["steps"]] == [None, "45", None] and not got["direct"]
    assert ps.find(con, 1, "rotating", 0)["direct"]
    assert ps.find(con, 1, "sticky", 30)["recipe"] == {"kind": "click", "page": PAGE}
    a = proxyaccounts.accounts(con)[0]
    assert a["taught"] == ["rotating-0", "sticky-30"] and a["sess_kind"] == "rotating"


def test_shared_steps_carry_only_the_motions(tmp_path):
    con = _db(tmp_path)
    ps.save(con, 1, "sticky", 30, {"kind": "click", "page": PAGE},
            [step("click", "Sticky", page=PAGE + "/8812345?k=secret"), step("type", label="Order note", value="for jacob"),
             step("select", label="Package", value="pkg_9f8e7d6c5b4a3f2e1d0c9b8a", text="Resi 10 GB"),
             step("type", label="Minutes", value="30"), step("click", "Generate", id="user-88123")], "https://dash.prov.com")
    pack = ps.build(con)
    text = json.dumps(pack)
    assert "jacob" not in text and "secret" not in text and "8812345" not in text and "pkg_9f8e" not in text and "user-88123" not in text
    m = pack["motions"][0]
    assert m["host"] == "dash.prov.com" and m["any_ttl"] and m["steps"][0]["page"] == "/generate/*"
    assert [s["act"] for s in m["steps"]] == ["click", "select", "type", "click"]

    other = ps.ensure(proxyaccounts.ensure(db.connect(str(tmp_path / "b.db"))))
    assert ps.import_pack(other, ps.decode(ps.encode(pack)), "test") == 1
    assert ps.import_pack(other, pack, "again") == 0
    got = ps.shared_for(other, "https://www.dash.prov.com/x", "sticky", 60)
    assert [s.get("value") for s in got if s["act"] == "type"] == ["60"]
    assert ps.shared_for(other, "https://dash.prov.com", "rotating", 0) is None

    sent = []

    class Smtp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg):
            sent.append(msg)
    ps.send(pack, "me@example.com", "pw", "1.0", to="dev@example.com", smtp=Smtp())
    assert "shared steps" in sent[0]["Subject"] and "BEGIN BOTTRACK PACK" in sent[0].get_body().get_content()


class FakeSession:
    def __init__(self):
        self.opened, self.seen, self.windows = [], {}, {}

    def open(self, aid, url, name, saved=None, combo=None):
        self.opened.append((aid, url, combo))

    def capture(self, aid):
        return None

    def keep(self, aid):
        return self.seen[aid]


def test_generate_teaches_a_new_choice_then_uses_it(tmp_path, monkeypatch):
    path = str(tmp_path / "a.db")
    app = create_app(path)
    con = _db(tmp_path)
    fake = FakeSession()
    monkeypatch.setattr(proxylogin, "_webview", object())
    monkeypatch.setattr(proxylogin, "_session", fake)
    c = app.test_client()
    r = c.post("/proxies/accounts/1/pool", data={"session": "sticky", "ttl": "30"})
    assert "learning=1" in r.headers["Location"] and fake.opened == [(1, "https://dash.prov.com", {"session": "sticky", "ttl": 30})]

    # The window saw the steps and the list: Use this keeps them as the Sticky 30 min setting.
    fake.windows[1] = type("W", (), {"hide": lambda self: None})()
    fake.seen[1] = {"lines": ["1.2.3.4:80:u:p"], "recipe": {"kind": "click", "page": PAGE, "button": {"text": "Generate"}, "at": 5},
                    "steps": [step("click", "Sticky", at=1), step("click", "Generate", at=5)], "combo": {"session": "sticky", "ttl": 30},
                    "watching": True, "cookie": ""}
    c.post("/proxies/login/save", data={"id": 1})
    assert ps.find(con, 1, "sticky", 30)["steps"][0]["el"]["text"] == "Sticky"
    assert con.execute("SELECT COUNT(*) FROM proxies WHERE grp='Hype'").fetchone()[0] == 1
    assert json.loads(con.execute("SELECT recipe FROM proxy_accounts").fetchone()[0])["kind"] == "click"

    used = []
    monkeypatch.setattr(proxyaccounts, "refresh_pool", lambda con, acct, opener=None, setting=None: used.append(setting) or 3)
    c.post("/proxies/accounts/1/pool", data={"session": "sticky", "ttl": "30"})
    assert used[0]["session"] == "sticky" and len(fake.opened) == 1
    html = c.get("/proxies").get_data(as_text=True)
    assert "Sticky · 30 min" in html and 'name="ttl"' in html and "Shared steps" in html
