import io
import json
import random
from datetime import date

from jigstream import db, drop, jigexpr, profilebook, proxyaccounts, proxies, review, stuck, tracking
from jigstream.app import create_app
from jigstream.mail import Email
from jigstream.orderstore import Store


def order(store, n, status, retailer="Target", day="2026-10-20", total=20.0, **extra):
    store.save(Email(f"<{n}@x>", "shop@x", "Order", day + "T10:00:00", "", "me@x"),
               dict({"kind": "order", "currency": "USD", "shipping_address": None, "tracking_number": None, "carrier": None,
                     "estimated_delivery": None, "retailer": retailer, "order_number": f"N{n}", "order_date": day, "total": total,
                     "items": [{"name": "Thing", "quantity": 1}], "shipping_status": status}, **extra))


def test_bot_module_versions_are_one_site():
    assert stuck.site_key("amazonv3") == stuck.site_key("amazon") == "amazon"
    assert stuck.site_key("Walmart V2") == "walmart"
    assert drop.site_name(["amazon", "amazonv3", "amazonv3"]) == "Amazon"
    assert drop.site_name(["Pokemon Center US"]) == "Pokemon Center US"


def test_tracking_lanes(tmp_path):
    store = Store(db.connect(str(tmp_path / "t.db")))
    order(store, 1, "ordered")
    order(store, 2, "in_transit", estimated_delivery="2026-10-25")
    order(store, 3, "out_for_delivery")
    order(store, 4, "delivered", day="2026-10-20")
    order(store, 5, "delivered", day="2026-01-01")
    order(store, 6, "cancelled")
    lanes = tracking.lanes(store.orders(), today=date(2026, 10, 24))
    assert [o["order_number"] for o in lanes["ordered"]] == ["N1"]
    assert [o["order_number"] for o in lanes["moving"]] == ["N2"]
    assert [o["order_number"] for o in lanes["delivered"]] == ["N4"]  # old deliveries and cancels are left off


def test_review_steps_through_a_store_and_marks(tmp_path):
    path = str(tmp_path / "r.db")
    store = Store(db.connect(path))
    order(store, 1, "ordered", total=10)
    order(store, 2, "delivered", total=30)
    order(store, 3, "cancelled", total=99)  # not part of the spend, so not in the list
    con = review.ensure(db.connect(path))
    title, keys = review.source(con, "store", "target", "2026")
    assert len(keys) == 2 and "target" in title.lower()
    app = create_app(path)
    c = app.test_client()
    page = c.get("/review?src=store&name=Target&year=2026")
    assert page.status_code == 200 and b"Looks right" in page.data
    c.post("/review/mark", data={"key": keys[0], "back": "/review?src=store&name=Target&year=2026", "next": keys[1]})
    assert review.marks(review.ensure(db.connect(path))) == {keys[0]: "ok"}
    for src in ("/review?src=biggest&year=2026", f"/review?src=order&name={keys[0][1:]}", "/tracking", "/spend"):
        assert c.get(src).status_code == 200, src


def test_expressions():
    rng = random.Random(3)
    out = jigexpr.expand("{address1}, APT 1-%c5%", {"address1": "123 Address St"}, rng)
    assert out.startswith("123 Address St, APT 1-") and len(out.split("-")[1]) == 5 and out.split("-")[1].isupper()
    assert jigexpr.expand("%n[7-7]% %w[Unit]% 100%%", {}, rng) == "7 Unit 100%"
    assert jigexpr.expand("%emailname%", {"email": "jacob.smith22+x@gmail.com"}) == "Jacob Smith"
    assert jigexpr.expand("%limit[3|{city}]%", {"city": "Austin"}) == "Aus"
    assert jigexpr.rewrite("12 Main St Apt 4", {}, "regex", r"\s*apt\s*\d+$", "") == "12 Main St"
    assert jigexpr.rewrite("12 Main St", {}, "regex", r"^(\d+)", r"\1A") == "12A Main St"
    try:
        jigexpr.expand("%nope%")
        assert False
    except jigexpr.ExprError:
        pass


def test_rewrite_profiles_preview_and_save(tmp_path):
    con = db.connect(str(tmp_path / "p.db"))
    con.execute("INSERT INTO profiles (name, ship_address1, bill_address1) VALUES ('A', '1 Oak St', '1 Oak St')")
    con.commit()
    rows, err = profilebook.rewrite(con, ["A"], "address1", "template", "", "{value} #%d3%", shipping=True, billing=False, save=False)
    assert err is None and len(rows) == 1 and rows[0][2].startswith("1 Oak St #")
    assert con.execute("SELECT ship_address1 FROM profiles").fetchone()[0] == "1 Oak St"  # preview writes nothing
    profilebook.rewrite(con, ["A"], "address1", "regex", "oak", "Elm", shipping=True, billing=True)
    assert tuple(con.execute("SELECT ship_address1, bill_address1 FROM profiles").fetchone()) == ("1 Elm St", "1 Elm St")
    assert profilebook.rewrite(con, ["A"], "address1", "template", "", "%bad%")[1].startswith("Unknown expression")


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_provider_account_usage_and_pool(tmp_path):
    con = proxyaccounts.ensure(db.connect(str(tmp_path / "a.db")))
    con.execute("INSERT INTO proxy_accounts (name, api_key, auth, usage_url, used_path, left_path, unit, list_url, grp)"
                " VALUES ('Resi', 'KEY123456', 'bearer', 'https://api.x/usage', 'data.used', 'data.left', 'mb',"
                " 'https://api.x/list?k={key}', 'Resi pool')")
    con.commit()
    acct = proxyaccounts._acct(con, 1)
    seen = []

    def opener(req, timeout):
        seen.append((req.full_url, req.headers.get("Authorization")))
        if "usage" in req.full_url:
            return Answer(json.dumps({"data": {"used": 1536, "left": 512}}).encode())
        return Answer(b"1.2.3.4:80:u:p\n5.6.7.8:80:u:p\n")
    assert proxyaccounts.check_usage(con, acct, opener) == (1.5, 0.5, None)
    assert seen[0] == ("https://api.x/usage", "Bearer KEY123456")
    assert proxyaccounts.refresh_pool(con, acct, opener) == 2
    assert seen[1][0] == "https://api.x/list?k=KEY123456"
    assert con.execute("SELECT COUNT(*) FROM proxies WHERE grp='Resi pool' AND provider='Resi'").fetchone()[0] == 2
    a = proxyaccounts.accounts(con)[0]
    assert a["low"] and "api_key" not in a and a["key_hint"] == "••••3456"
    assert proxyaccounts.pool_lines('[{"ip": "9.9.9.9", "port": 1, "username": "a", "password": "b"}]') == ["9.9.9.9:1:a:b"]


def test_reveal_switch_is_remembered(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("UI_REVEAL", raising=False)
    c = create_app(str(tmp_path / "v.db")).test_client()
    assert b'class="reveal"' not in c.get("/orders").data
    c.post("/prefs/reveal", data={"reveal": "1"})
    assert "UI_REVEAL=1" in (tmp_path / ".env").read_text()
    assert b'class="reveal"' in c.get("/orders").data
    c.post("/prefs/reveal", data={"reveal": "0"})


def test_deletes_ask_in_the_page(tmp_path):
    c = create_app(str(tmp_path / "d.db")).test_client()
    page = c.get("/profiles").data.decode()
    assert "return confirm(" not in page and 'data-confirm="Delete the ticked profiles?"' in page and "btConfirm" in page


def test_shared_learning_sends_lessons_not_emails(tmp_path, monkeypatch):
    from jigstream import packs
    monkeypatch.setenv("GMAIL_ACCOUNTS", "jacob.real@gmail.com:abcdabcdabcdabcd,me@myjigs.com:abcdabcdabcdabcd")
    con = db.connect(str(tmp_path / "k.db"))
    store = Store(con)
    con.execute("INSERT INTO profiles (name, first_name, last_name, ship_address1) VALUES ('P', 'Jacobus', 'Realname', '7100 County Road 9')")
    prompt = ("From: Target <orders@target.com>\nSubject: Jacobus, your order 102000000000404 has shipped\nDate: x\n\n"
              "Hi Jordan Reyes, it ships to Sam Ortiz, 7100 County Road 9 Apt 4, Columbus, OH 43215.\n"
              "Order # 102000000000404\nTracking number: 770000000001\nOrder total: $126.99\n"
              "Pokemon Prismatic Booster Bundle. Call 614-555-1234. Card 4111 1111 1111 1111.")
    answer = {"kind": "order", "order_number": "102000000000404", "tracking_number": "770000000001", "total": 126.99,
              "shipping_name": "Sam Ortiz", "shipping_address": "7100 County Road 9", "shipping_status": "in_transit",
              "items": [{"name": "Pokemon Prismatic Booster Bundle"}]}
    rows = [("<1>", "Target <orders@target.com>", "Jacobus, your order 102000000000404 has shipped", prompt, "in_transit"),
            ("<2>", "Mom <mom@gmail.com>", "your order", prompt, "in_transit"),  # personal mail: never shared
            ("<3>", "Me <me@myjigs.com>", "your order", prompt, "in_transit")]  # the user's own domain: never shared
    for mid, sender, subject, p, status in rows:
        con.execute("INSERT INTO training_examples (message_id, sender, subject, prompt, corrected, status, model_was_right)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0)", (mid, sender, subject, p, json.dumps(answer), status))
    con.commit()
    pack = packs.build(con, store)
    assert len(pack["lessons"]) == 1 and pack["left_out"] == 2
    sent = packs.public(pack)
    blob = json.dumps(sent)
    for secret in ("Jacobus", "Realname", "Jordan", "Sam", "Ortiz", "7100", "43215", "jacob.real", "614", "4111",
                   "102000000000404", "770000000001", "126.99", "Pokemon", "Prismatic", "ref", "prompt"):
        assert secret not in blob, secret
    ls = sent["lessons"][0]
    assert ls["sender_domain"] == "target.com" and ls["label"] == "in_transit"
    assert ls["answer"] == {"kind": "order", "shipping_status": "in_transit"}
    assert ls["cues"] == ["your order", "has shipped"]
    assert ls["fields"] == {"order_number": "Order #", "tracking_number": "Tracking number", "total": "Order total"}
    # round trip through the email body, then into another Mac's shared lessons
    other = db.connect(str(tmp_path / "o.db"))
    Store(other)
    assert packs.import_pack(other, packs.decode(packs.encode(sent)), "test") == 1
    assert packs.import_pack(other, sent, "again") == 0
    # someone else's pack is checked again: made-up fields and words off the lists don't get in
    bad = {"format": packs.FORMAT, "lessons": [{"sender_domain": "target.com", "label": "Jordan's stuff", "cues": ["sam ortiz"],
                                                "answer": {"kind": "order", "shipping_name": "Sam Ortiz"},
                                                "fields": {"order_number": "Order 1234", "shipping_name": "Ship to"}}]}
    assert packs.import_pack(other, bad, "bad") == 1
    kept = json.loads(other.execute("SELECT lesson FROM shared_lessons WHERE source = 'bad'").fetchone()[0])
    assert "Sam" not in json.dumps(kept) and kept["label"] == "" and kept["fields"] == {} and kept["cues"] == []
    picker = packs.picker(Store(other), default=[("default", "{}")])
    email = Email("<2>", "Target <orders@target.com>", "Your order has shipped", "", "", "me@x")
    assert picker(email) == [("default", "{}")]  # other people's lessons are hints, never example emails
    hint = picker.hint(email)
    assert "target.com" in hint and '"Order #"' in hint and "in_transit" in hint
    assert picker.hint(Email("<3>", "Shop <hi@elsewhere.io>", "Weekly deals", "", "", "me@x")) == ""


def test_old_text_packs_keep_only_lessons(tmp_path):
    from jigstream import packs
    old = {"format": "bottrack-examples", "examples": [{"sender_domain": "bestbuy.com", "subject": "Your order has been cancelled",
           "label": "cancelled", "prompt": "Hi Alex Smith, order BBY01-806 was cancelled", "answer": {"kind": "order", "shipping_status": "cancelled"}}]}
    pack = packs.decode(json.dumps(old))
    assert pack["format"] == packs.FORMAT and "prompt" not in json.dumps(pack) and "Smith" not in json.dumps(pack)
    con = db.connect(str(tmp_path / "m.db"))
    Store(con)
    con.execute("CREATE TABLE shared_examples (id INTEGER PRIMARY KEY, source TEXT, sender_domain TEXT, subject TEXT,"
                " label TEXT, prompt TEXT, answer TEXT)")
    con.execute("INSERT INTO shared_examples (source, sender_domain, subject, label, prompt, answer) VALUES"
                " ('old', 'bestbuy.com', 'Your order has been cancelled', 'cancelled', 'Hi Alex Smith', ?)",
                (json.dumps({"kind": "order", "shipping_status": "cancelled"}),))
    con.commit()
    assert [ls["cues"] for ls in packs.shared(con)] == [["your order has been cancelled"]]
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'shared_examples'").fetchone()


def test_jig_styles_from_drop_results(tmp_path):
    from jigstream import jigstats
    con = db.connect(str(tmp_path / "j.db"))
    con.execute("INSERT INTO profiles (name, ship_address1) VALUES ('front', 'XQZ 7100 County Road 9'),"
                " ('after', '7100 KJH County Road 9 Apt 4')")
    con.commit()
    items = [{"site": "Target", "profile": "front", "outcome": "cancelled"}] * 6 + \
            [{"site": "target.com", "profile": "after", "outcome": "confirmed"}] * 5 + \
            [{"site": "Target", "profile": "after", "outcome": "cancelled"}] + \
            [{"site": "Target", "profile": "after", "outcome": "decline"}] * 9
    r = jigstats.report(con, items)["Target"]
    front = {row["style"]: row for row in r["groups"]["Front"]}
    assert front["Letters after the number"]["rate"] == 17 and front["Letters after the number"].get("best")
    assert front["Letters before the number"]["rate"] == 100
    assert front["Letters after the number"]["recipe"]["replace"] == r"\1 %c3% "
    assert {row["style"] for row in r["groups"]["Unit"]} == {"Apt", "No unit"}


def test_provider_presets():
    from datetime import datetime
    p = proxyaccounts.PRESETS["Webshare"]
    url = proxyaccounts.fill_dates(p["usage_url"], datetime(2026, 10, 24, 5, 6, 7))
    assert "timestamp__gte=2026-10-01T00:00:00Z" in url and "timestamp__lte=2026-10-24T05:06:07Z" in url
    page = json.dumps({"count": 2, "results": [
        {"proxy_address": "1.2.3.4", "port": 80, "username": "u", "password": "p", "valid": True},
        {"proxy_address": "5.6.7.8", "port": 81, "username": "u", "password": "p", "valid": False}]})
    assert proxyaccounts.pool_lines(page) == ["1.2.3.4:80:u:p"]
    assert proxyaccounts._request(p["list_url"], "K", p["auth"]).headers["Authorization"] == "Token K"


def test_star_paths_sum_every_plan_and_list_body_is_posted(tmp_path):
    usage = {"packages": [{"usage": {"gb_used": 1.2, "gb_total": 5, "gb_remaining": 3.8}},
                          {"usage": {"gb_used": 0.3, "gb_total": 2, "gb_remaining": 1.7}}]}
    assert proxyaccounts.dig(usage, "packages.*.usage.gb_remaining") == 5.5
    assert proxyaccounts.dig({"balances": {"a": {"gb": 2}, "b": {"gb": 0.5}}}, "balances.*.gb") == 2.5
    assert proxyaccounts.dig({"balances": {}}, "balances.*.gb") is None

    con = proxyaccounts.ensure(db.connect(str(tmp_path / "a.db")))
    p = proxyaccounts.PRESETS["FlameProxies"]
    con.execute("INSERT INTO proxy_accounts (name, api_key, auth, usage_url, used_path, left_path, total_path, unit, list_url, list_body)"
                " VALUES ('Flame', 'fp_live_k', ?, ?, ?, ?, ?, ?, ?, ?)",
                (p["auth"], p["usage_url"], p["used_path"], p["left_path"], p["total_path"], p["unit"], p["list_url"], p["list_body"]))
    sent = []

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        sent.append(req)
        if req.get_method() == "POST":
            return Answer(json.dumps({"count": 2, "proxies": ["proxy.flameproxies.com:8989:u1:p", "proxy.flameproxies.com:8989:u2:p"]}).encode())
        return Answer(json.dumps(usage).encode())

    acct = proxyaccounts._acct(con, 1)
    assert proxyaccounts.check_usage(con, acct, opener) == (1.5, 5.5, 7.0)
    assert proxyaccounts.refresh_pool(con, acct, opener) == 2
    post = sent[-1]
    assert post.get_header("Authorization") == "Bearer fp_live_k" and post.get_header("Content-type") == "application/json"
    assert json.loads(post.data)["package_id"] == 0
    # ResiFactory's plain list answer: one string of lines inside the JSON.
    assert proxyaccounts.pool_lines(json.dumps({"proxies": "h:7777:a:b\nh:7777:c:d", "count": 2})) == ["h:7777:a:b", "h:7777:c:d"]


def test_vantage_preset_reads_balances_and_lines():
    p = proxyaccounts.PRESETS["Vantage Proxies"]
    ans = {"balances": [{"tier": "mobile", "total_gb": 10.0, "used_gb": 2.415, "remaining_gb": 7.585},
                        {"tier": "core_residential", "total_gb": 100.0, "used_gb": 0.0, "remaining_gb": 100.0}]}
    assert proxyaccounts.dig(ans, p["left_path"]) == 107.585 and proxyaccounts.dig(ans, p["total_path"]) == 110
    lines = {"count": 2, "lines": ["mob.vantageproxies.com:8000:u-sid-a:p", "mob.vantageproxies.com:8000:u-sid-b:p"]}
    assert proxyaccounts.pool_lines(json.dumps(lines)) == lines["lines"]
    assert json.loads(p["list_body"])["tier"] == "apex_residential"


def test_guessed_fields_and_wrapped_errors(tmp_path):
    body = {"code": 200, "message": "ok", "body": {"planName": "basic", "price": 12, "totalFlow": 10, "usedFlow": 2.5,
                                                    "remainingFlow": 7.5, "expireTime": "2026-12-01"}}
    assert proxyaccounts.guess(body) == (2.5, 7.5, 10)
    assert proxyaccounts.guess({"data": {"remaining_bytes": 3 * 1024 ** 3, "used_mb": 512}}) == (0.5, 3.0, None)

    con = proxyaccounts.ensure(db.connect(str(tmp_path / "a.db")))
    p = proxyaccounts.PRESETS["IPFist Residential"]
    con.execute("INSERT INTO proxy_accounts (name, api_key, auth, usage_url, used_path, left_path, total_path, unit)"
                " VALUES ('IPFist', 'ak_k', ?, ?, '', '', '', 'gb')", (p["auth"], p["usage_url"]))
    answers = [body, {"code": 403, "message": "API key scope cannot access this resource", "body": None}]

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    opener = lambda req, timeout: Answer(json.dumps(answers.pop(0)).encode())  # noqa: E731
    acct = proxyaccounts._acct(con, 1)
    assert proxyaccounts.check_usage(con, acct, opener) == (2.5, 7.5, 10)
    try:
        proxyaccounts.check_usage(con, acct, opener)
        raise AssertionError("a wrapped 403 should fail")
    except ValueError as e:
        assert "scope cannot access" in str(e)
