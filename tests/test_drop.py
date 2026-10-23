import json

from jigstream import drop, parsers
from jigstream.app import create_app
from jigstream.orderstore import Store

from test_stuck import mail, setup


def hook(c, fp, site, profile, group, proxy, color=0x3DDC84, ts="2026-10-20T15:00:00Z", number="", reason=""):
    fields = [{"name": "Site", "value": site}, {"name": "Item", "value": "Pokemon ETB - $49.99"},
              {"name": "Profile Name", "value": profile}, {"name": "Proxy Group", "value": group},
              {"name": "Proxy", "value": proxy}, {"name": "Order Number", "value": number}]
    if reason:
        fields.append({"name": "Cancel Reason", "value": reason})
    embed = {"title": "Successful Checkout!", "color": color, "timestamp": ts, "fields": fields,
             "footer": {"text": f"Hayha {fp}"}}
    parsers.ingest(c, json.dumps([{"username": "Hayha", "embeds": [embed]}]))


def test_proxy_host_drops_logins():
    assert drop.proxy_host("isp.example.net:8000:jake:s3cret") == "isp.example.net:8000"
    assert drop.proxy_host("http://jake:s3cret@gw.resi.io:7777") == "gw.resi.io:7777"
    assert drop.proxy_host("jake:12345:gw.resi.io:7777") == "gw.resi.io:7777"
    assert drop.proxy_host("1.2.3.4:3128") == "1.2.3.4:3128"
    assert drop.proxy_host("localhost") == "" and drop.proxy_host("None") == ""
    assert drop.proxy_host("jake:s3cret:what") == "(hidden)"


def test_only_real_checkouts_count(tmp_path):
    c, store = setup(tmp_path)
    hook(c, "a", "Target", "Ana", "ISP A", "isp.example.net:8000:jake:s3cret", number="9001")
    mail(store, "a1", "Target", "ordered", "2026-10-20T15:05:00+00:00", "9001")
    hook(c, "b", "Target", "Ana", "Resi B", "jake:pw@gw.resi.io:7777", number="9002")
    mail(store, "b1", "Target", "cancelled", "2026-10-21T08:00:00+00:00", "9002")
    hook(c, "c", "Target", "Jacob 1", "Resi B", "gw.resi.io:7777", color=0xFF4455, reason="Policy - Item Demand")
    hook(c, "d", "Walmart", "Jacob 1", "ISP A", "isp.example.net:8001", ts="2026-10-18T15:00:00Z")
    r = drop.report(Store(c).db)
    groups = {t["name"]: t for t in r["groups"]}
    assert groups["ISP A"]["real"] == 2 and groups["ISP A"]["confirmed"] == 1
    assert groups["Resi B"]["real"] == 0 and groups["Resi B"]["cancelled"] == 1 and groups["Resi B"]["declines"] == 1
    assert r["groups"][0]["name"] == "ISP A"
    assert [s["site"] for s in r["drops"]] == ["Target", "Walmart"]
    target = r["drops"][0]["days"][0]
    assert target["day"] and [x["proxy_group"] for x in target["checkouts"]] == ["ISP A", "Resi B", "Resi B"]
    assert r["drops"][0]["tally"]["real"] == 1
    only = drop.report(Store(c).db, site="Walmart")
    assert [s["site"] for s in only["drops"]] == ["Walmart"] and only["tally"]["tries"] == 1


def test_page_never_shows_proxy_passwords(tmp_path):
    c, store = setup(tmp_path)
    hook(c, "a", "Target", "Ana", "ISP A", "isp.example.net:8000:jake:s3cret")
    client = create_app(str(tmp_path / "m.db")).test_client()
    page = client.get("/drop").get_data(as_text=True)
    assert "isp.example.net:8000" in page and "ISP A" in page and "Ana" in page
    assert "s3cret" not in page and "jake" not in page
    assert "s3cret" not in client.get("/drop?sort=profile&site=Target").get_data(as_text=True)
    assert 'href="/drop"' in page and 'id="drop"' in client.get("/manual").get_data(as_text=True)


def test_drops_tab_holds_every_checkout_page(tmp_path):
    c, store = setup(tmp_path)
    hook(c, "a", "Target", "Ana", "ISP A", "isp.example.net:8000")
    client = create_app(str(tmp_path / "m.db")).test_client()
    page = client.get("/drop").get_data(as_text=True)
    assert "day=" in page and 'href="/checkouts"' in page and 'href="/stuck"' in page
    assert 'href="/checkouts" title="Checkouts"' not in page  # no separate sidebar entries
    for url in ("/checkouts", "/stuck"):
        assert 'class="it on" href="/drop"' in client.get(url).get_data(as_text=True)
    day = drop.rows(Store(c).db)[0]["day"]
    page = client.get(f"/drop?day={day}").get_data(as_text=True)
    assert "Unsure" in page and 'action="/drop/' in page and "isp.example.net:8000" in page


def test_hand_verdict_sticks_and_corrects_the_order(tmp_path, monkeypatch):
    import jigstream.app as appmod
    monkeypatch.setattr(appmod.training, "rescan", lambda *a, **k: None)
    c, store = setup(tmp_path)
    hook(c, "a", "Target", "Ana", "ISP A", "isp.example.net:8000", number="9001")
    mail(store, "a1", "Target", "ordered", "2026-10-20T15:05:00+00:00", "9001")
    hook(c, "b", "Target", "Ana", "ISP A", "isp.example.net:8000", ts="2026-10-20T15:01:00Z")
    ids = {r["fingerprint"]: r["id"] for r in drop.rows(Store(c).db)}
    a, b = sorted(ids.values())
    client = create_app(str(tmp_path / "m.db")).test_client()
    client.post(f"/drop/{a}/verdict", data={"verdict": "cancelled"})
    client.post(f"/drop/{b}/verdict", data={"verdict": "good"})
    out = {r["id"]: r["outcome"] for r in drop.rows(Store(c).db)}
    assert out[a] == "cancelled" and out[b] == "confirmed"
    # The order email said ordered; it's corrected so the AI learns from its emails.
    assert c.execute("SELECT shipping_status FROM tracked_orders WHERE order_number = '9001'").fetchone()[0] == "cancelled"
    client.post(f"/drop/{a}/verdict", data={"verdict": "auto"})
    assert {r["id"]: r["outcome"] for r in drop.rows(Store(c).db)}[a] == "cancelled"  # the order now says so


def test_late_cancel_beats_an_earlier_good(tmp_path):
    c, store = setup(tmp_path)
    # Pokemon Center: confirmed at the drop, cancelled five weeks later.
    hook(c, "a", "Pokemon Center", "Ana", "ISP A", "isp.example.net:8000", ts="2026-09-01T15:00:00Z", number="PC1")
    mail(store, "a1", "Pokemon Center", "ordered", "2026-09-01T15:05:00+00:00", "PC1")
    a = drop.rows(Store(c).db)[0]["id"]
    drop.set_verdict(c, a, "good")
    c.execute("UPDATE checkout_verdicts SET set_at = '2026-09-02T00:00:00+00:00'")
    c.commit()
    assert drop.rows(Store(c).db)[0]["outcome"] == "confirmed"
    mail(store, "a2", "Pokemon Center", "cancelled", "2026-10-07T09:00:00+00:00", "PC1")
    assert drop.rows(Store(c).db)[0]["outcome"] == "cancelled"


def test_cancel_weeks_later_still_matches_without_an_order_number(tmp_path):
    c, store = setup(tmp_path)
    hook(c, "a", "Pokemon Center", "Ana", "ISP A", "isp.example.net:8000", ts="2026-09-01T15:00:00Z")
    mail(store, "a1", "Pokemon Center", "cancelled", "2026-10-03T09:00:00+00:00", address="9 Oak Ave, Dallas, TX 75201",
         name="Ana Lopez")
    c.execute("UPDATE tracked_orders SET order_date = NULL")
    c.commit()
    assert drop.rows(Store(c).db)[0]["outcome"] == "cancelled"


def valor(c, title, number="", profile="Ana", site="Target", ts="2026-10-25T10:00:00Z", fields=()):
    embed = {"title": title, "timestamp": ts, "fields": [{"name": "Site", "value": site}, {"name": "Profile", "value": profile},
                                                         {"name": "Order Number", "value": number}, *fields]}
    return parsers.ingest(c, json.dumps([{"username": "Valor AIO", "embeds": [embed]}]))


def test_valor_follow_up_decides_good_or_cancelled(tmp_path):
    c, store = setup(tmp_path)
    for fp, n in (("a", "V1"), ("b", "V2"), ("c", "")):
        valor(c, "Successful Checkout", n, ts="2026-10-20T15:00:00Z")
    assert valor(c, "Order Cancelled", "V1") == 1
    valor(c, "Order Update", "V2", fields=[{"name": "Status", "value": "Stuck"}])
    valor(c, "Order Cancelled", "", ts="2026-10-21T10:00:00Z")  # no order number: same bot, site and profile
    # Follow-ups are not checkouts: they don't add tries.
    assert c.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 3
    out = {r["order_number"]: r["outcome"] for r in drop.rows(Store(c).db)}
    assert out == {"V1": "cancelled", "V2": "confirmed", "": "cancelled"}
    # An order email cancelling it wins over the bot's "good".
    mail(store, "m", "Target", "cancelled", "2026-10-30T09:00:00+00:00", "V2")
    assert {r["order_number"]: r["outcome"] for r in drop.rows(Store(c).db)}["V2"] == "cancelled"


def test_checkout_webhooks_are_not_follow_ups():
    embed = lambda title, fields=(): {"title": title, "fields": list(fields)}
    assert parsers.update_verdict(embed("Successful Checkout!")) is None
    assert parsers.update_verdict(embed("Checkout Cancelled")) is None
    assert parsers.update_verdict(embed("Order Placed")) is None
    assert parsers.update_verdict(embed("Order Stuck")) == "good"
    assert parsers.update_verdict(embed("Order status", [{"name": "Cancel Reason", "value": "x"}])) is None


def test_follow_ups_stored_as_checkouts_move_out(tmp_path):
    c, store = setup(tmp_path)
    valor(c, "Successful Checkout", "V1")
    raw = json.dumps({"payload_username": "Valor AIO", "embed": {"title": "Order Cancelled", "fields": [
        {"name": "Order Number", "value": "V1"}]}})
    c.execute("INSERT INTO checkouts (fingerprint, bot, status, raw) VALUES ('old', 'Valor', 'failure', ?)", (raw,))
    assert parsers.move_updates(c) == 1
    assert c.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 1
    assert drop.rows(Store(c).db)[0]["outcome"] == "cancelled"


def test_click_into_a_group(tmp_path):
    c, store = setup(tmp_path)
    hook(c, "a", "Target", "Ana", "ISP A", "isp.example.net:8000", number="1")
    hook(c, "b", "Target", "Jacob 1", "Resi B", "gw.resi.io:7777", number="2")
    day = drop.rows(Store(c).db)[0]["day"]
    r = drop.report(Store(c).db, day=day, by="group", name="Resi B")
    assert [x["profile"] for s in r["drops"] for d in s["days"] for x in d["checkouts"]] == ["Jacob 1"]
    assert {t["name"] for t in r["chips"]["group"]} == {"ISP A", "Resi B"}  # the others stay clickable
    client = create_app(str(tmp_path / "m.db")).test_client()
    page = client.get(f"/drop?day={day}&by=profile&name=Ana").get_data(as_text=True)
    assert "Profile: Ana" in page and "gw.resi.io" not in page.split("Click a group")[1].split("<h3>Best")[0]
    assert "by=group" in client.get("/drop").get_data(as_text=True)
