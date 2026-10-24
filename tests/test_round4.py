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


def test_training_pack_scrubs_and_picks(tmp_path, monkeypatch):
    from jigstream import packs
    monkeypatch.setenv("GMAIL_ACCOUNTS", "jacob.real@gmail.com:abcdabcdabcdabcd")
    con = db.connect(str(tmp_path / "k.db"))
    store = Store(con)
    con.execute("INSERT INTO profiles (name, first_name, last_name, ship_address1) VALUES ('P', 'Jacobus', 'Realname', '7100 County Road 9')")
    prompt = ("From: Target <orders@target.com>\nSubject: Your order 102000000000404 has shipped\nDate: x\n\n"
              "Hi Jacobus, order 102000000000404 ships to Jacobus Realname, 7100 County Road 9 Apt 4, Columbus, OH 43215. "
              "Track 770000000001 at https://track.example.com/t?id=secret. Call 614-555-1234. Card 4111 1111 1111 1111. "
              "Sent to jacob.real@gmail.com or orders@target.com")
    answer = {"kind": "order", "order_number": "102000000000404", "tracking_number": "770000000001",
              "shipping_name": "Jacobus Realname", "shipping_address": "7100 County Road 9", "shipping_status": "shipped"}
    con.execute("INSERT INTO training_examples (message_id, sender, subject, prompt, corrected, status, model_was_right)"
                " VALUES ('<1>', 'Target <orders@target.com>', 'Your order has shipped', ?, ?, 'shipped', 0)",
                (prompt, json.dumps(answer)))
    con.commit()
    pack = packs.build(con, store, rng=random.Random(1))
    assert len(pack["examples"]) == 1
    ex = pack["examples"][0]
    blob = ex["prompt"] + json.dumps(ex["answer"])
    for secret in ("Jacobus", "Realname", "7100", "43215", "jacob.real", "614-555-1234", "4111", "102000000000404",
                   "770000000001", "secret"):
        assert secret not in blob, secret
    assert "orders@target.com" in ex["prompt"] and ex["sender_domain"] == "target.com"
    assert len(ex["answer"]["order_number"]) == 15 and ex["answer"]["order_number"].isdigit()
    # round trip through the email body, then into another Mac's shared examples
    other = db.connect(str(tmp_path / "o.db"))
    Store(other)
    assert packs.import_pack(other, packs.decode(packs.encode(pack)), "test") == 1
    assert packs.import_pack(other, pack, "again") == 0
    picker = packs.picker(Store(other), default=[("default", "{}")])
    near = picker(Email("<2>", "Target <orders@target.com>", "Your order has shipped", "", "", "me@x"))
    far = picker(Email("<3>", "Shop <hi@elsewhere.io>", "Weekly deals", "", "", "me@x"))
    assert near and near[0][0].startswith("From: Target") and far == [("default", "{}")]


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
