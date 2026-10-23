import json

from jigstream import drop, parsers
from jigstream.app import create_app
from jigstream.orderstore import Store

from test_stuck import mail, setup
from test_profile_match import profile


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
    assert "Profile: Ana" in page and "gw.resi.io" not in page.split("Pick one to see")[1].split("<h3>Best")[0]
    assert "by=group" in client.get("/drop").get_data(as_text=True)


def real_valor(c, title, color, number, email, ts, extra=()):
    """Valor 3.0's webhooks as jacob's screenshots show them (2026-10-23)."""
    fields = [*extra, {"name": "Order Number", "value": number}, {"name": "Email", "value": email}]
    embed = {"title": title, "color": color, "timestamp": ts, "fields": fields, "footer": {"text": "Valor 3.0"}}
    return parsers.ingest(c, json.dumps([{"username": "Valor", "embeds": [embed]}]))


def test_real_valor_follow_ups(tmp_path):
    c, store = setup(tmp_path)
    item = [{"name": "Item", "value": "Pok&#233;mon Trading Card Game: Destined Rivals Three-Booster Blister"},
            {"name": "Qty", "value": "1"}]
    checkout = [{"name": "Site", "value": "Target"}, {"name": "Mode", "value": "Default"},
                {"name": "Product", "value": "(94300082) Pok&#233;mon Trading Card Game: Destined Rivals"},
                {"name": "Price", "value": "14.99"}, {"name": "Quantity", "value": "1"},
                {"name": "Profile", "value": "Ana"}, {"name": "Proxy", "value": "isp.example.net:8000:u:p"}]
    for n, mail_ in (("102003850007046", "theo@outlook.com"), ("102003854795409", "lucy@outlook.com"),
                     ("102003854750691", "mari@outlook.com")):
        real_valor(c, "Checked Out! \U0001F389", 0x3DDC84, n, mail_, "2026-10-23T09:10:00Z", checkout)
    real_valor(c, "Order Canceled! (Policy - Item Demand)", 0xED4245, "102003850007046", "theo@outlook.com",
               "2026-10-23T09:12:00Z", item)
    real_valor(c, "Order Still Good ✅", 0x3DDC84, "102003854795409", "lucy@outlook.com", "2026-10-23T09:12:00Z", item)
    real_valor(c, "Order Still Good ✅", 0x3DDC84, "", "mari@outlook.com", "2026-10-23T09:13:00Z", item)
    rows = drop.rows(Store(c).db)
    assert len(rows) == 3 and all(r["bot"] == "Valor" and r["status"] == "success" for r in rows)
    assert {r["order_number"]: r["outcome"] for r in rows} == {
        "102003850007046": "cancelled", "102003854795409": "confirmed", "102003854750691": "confirmed"}
    assert {r["proxy"] for r in rows} == {"isp.example.net:8000"}


def test_tidy_product_names_and_odd_proxies(tmp_path):
    raw = ("[(94300082) Pok&amp;#233;mon Trading Card Game: Scarlet &amp;#38; Violet&amp;#8212; Destined Rivals]"
           "(https://www.target.com/p/-/A-94300082)")
    assert parsers.tidy(raw) == "(94300082) Pokémon Trading Card Game: Scarlet & Violet— Destined Rivals"
    assert drop.proxy_host("Leaf ISP") == "Leaf ISP"  # a list's name, nothing secret
    assert drop.proxy_host("Leaf ISP - 23.1.2.3:8000:user:s3cret") == "23.1.2.3:8000"
    assert drop.proxy_host("Leaf ISP - user:s3cret") == "(hidden)"
    c, store = setup(tmp_path)
    hook(c, "a", "Target", "Ana", "ISP A", "isp.example.net:8000")
    c.execute("UPDATE checkouts SET product = ?", (raw,))
    c.commit()
    client = create_app(str(tmp_path / "m.db")).test_client()
    page = client.get("/drop").get_data(as_text=True)
    assert "Pokémon Trading Card" in page and "&amp;#233;" not in page and "target.com/p" not in page
    assert 'class="droprow"' in page and "Open ›" in page
    assert "Pokémon Trading Card" in client.get("/checkouts").get_data(as_text=True)


def test_base_street_under_a_jig():
    from jigstream.address import base_line1
    assert base_line1("XJ3 123 Main Street Apt 4") == base_line1("123 main st., #7B") == "123 MAIN ST"
    assert base_line1("Ana Lopez, 9 Oak Avenue Unit 7, Dallas, TX 75201") == "9 OAK AVE"
    assert base_line1("4500 W Parkway Blvd Suite 210") == "4500 W PKWY BLVD"
    assert base_line1("100 N Main St NW Apt 2") == "100 N MAIN ST NW"
    assert base_line1("Dallas, TX 75201") == ""


def test_group_checkouts_by_address(tmp_path):
    c, store = setup(tmp_path)  # Jacob 1 ships to 123 Main Street, Ana to 9 Oak Ave
    profile(c, "Jacob 2", "Jacob", "Smith", "j2@gmail.com", "QZ 123 Main St Apt 9")
    hook(c, "a", "Target", "Jacob 1", "ISP A", "isp.example.net:8000", number="1")
    hook(c, "b", "Target", "Jacob 2", "ISP A", "isp.example.net:8000", number="2")
    hook(c, "c", "Target", "Ana", "ISP A", "isp.example.net:8000", number="3")
    hook(c, "d", "Target", "Nobody", "ISP A", "isp.example.net:8000", number="4")
    mail(store, "m4", "Target", "cancelled", "2026-10-20T16:00:00+00:00", "4", address="Bo, 77 Elm Rd Ste 5, Austin, TX 78701")
    addr = {x["profile"]: x["address"] for x in drop.rows(Store(c).db)}
    assert addr["Jacob 1"] == addr["Jacob 2"] and addr["Jacob 1"].startswith("123 MAIN ST")
    assert addr["Nobody"] == "77 ELM RD · 78701"  # from its order email
    r = drop.report(Store(c).db)
    board = {t["name"]: t for t in r["addresses"]}
    assert board[addr["Jacob 1"]]["tries"] == 2 and board["77 ELM RD · 78701"]["cancelled"] == 1
    only = drop.report(Store(c).db, by="address", name=addr["Jacob 1"])
    assert sorted(x["profile"] for s in only["drops"] for d in s["days"] for x in d["checkouts"]) == ["Jacob 1", "Jacob 2"]
    page = create_app(str(tmp_path / "m.db")).test_client().get("/drop").get_data(as_text=True)
    assert "by=address" in page and "Addresses" in page


def test_jigged_spellings_share_a_base_address_and_list_the_full_ones(tmp_path):
    c, store = setup(tmp_path)  # Jacob 1 ships to 123 Main Street, 78701
    profile(c, "Jacob 2", "Jacob", "Smith", "j2@gmail.com", "123 Mainn St", a2="Apt 4")
    profile(c, "Jacob 3", "Jacob", "Smith", "j3@gmail.com", "AB 123 Main St Suite 9")
    for fp, who in (("a", "Jacob 1"), ("b", "Jacob 2"), ("c", "Jacob 3"), ("d", "Jacob 1")):
        hook(c, fp, "Target", who, "ISP A", "isp.example.net:8000", number=fp)
    addr = {x["profile"]: x["address"] for x in drop.rows(Store(c).db)}
    assert addr["Jacob 1"] == addr["Jacob 2"] == addr["Jacob 3"] == "123 MAIN ST · 78701"
    assert {t["name"] for t in drop.report(Store(c).db)["addresses"]} == {"123 MAIN ST · 78701"}
    only = drop.report(Store(c).db, by="address", name="123 MAIN ST · 78701")
    used = {u["full"]: u for u in only["full_addresses"]}
    assert used["123 Main Street, Austin, TX 78701"]["tries"] == 2
    assert used["123 Mainn St Apt 4, Austin, TX 78701"]["profiles"] == ["Jacob 2"]
    page = create_app(str(tmp_path / "m.db")).test_client().get(
        "/drop?by=address&name=123 MAIN ST · 78701").get_data(as_text=True)
    assert "Full addresses used at 123 MAIN ST · 78701" in page and "AB 123 Main St Suite 9" in page


def test_not_an_order_is_left_out_of_every_count_and_can_be_set_anywhere(tmp_path, monkeypatch):
    import jigstream.app as appmod
    from jigstream import overview, stuck
    monkeypatch.setattr(appmod.training, "rescan", lambda *a, **k: None)
    c, store = setup(tmp_path)
    hook(c, "a", "Target", "Ana", "ISP A", "isp.example.net:8000", number="9001")
    mail(store, "a1", "Target", "ordered", "2026-10-20T15:05:00+00:00", "9001")
    hook(c, "b", "Target", "Ana", "ISP A", "isp.example.net:8000", ts="2026-10-20T15:01:00Z")
    a, b = sorted(r["id"] for r in drop.rows(Store(c).db))
    client = create_app(str(tmp_path / "m.db")).test_client()
    for page in ("/drop?day=2026-10-20&site=Target", "/stuck", "/checkouts"):
        assert 'value="not_order"' in client.get(page).get_data(as_text=True), page
    client.post(f"/drop/{b}/verdict", data={"verdict": "not_order"})
    rows = drop.rows(Store(c).db)
    assert {r["id"]: r["outcome"] for r in rows}[b] == "not_order"
    t = drop.tally(rows)
    assert (t["tries"], t["real"], t["not_order"]) == (1, 1, 1)
    xs = stuck.cross_reference(c)
    assert stuck.counts(xs)["all"] == 1 and stuck.counts(xs)["not_order"] == 1
    assert overview.summary(c, "2026")["checkouts"] == 1
    # The order email isn't touched: the test checkout was wrong, not the email.
    assert c.execute("SELECT shipping_status FROM tracked_orders WHERE order_number = '9001'").fetchone()[0] == "ordered"
    assert "Not an order" in client.get("/stuck?show=not_order").get_data(as_text=True)


def test_click_through_checkout_profile_address(tmp_path, monkeypatch):
    c, store = setup(tmp_path)
    from jigstream import profiles as P
    P.ingest(c, json.dumps([{"name": "Ana", "shipping": {"address1": "12 Main St Apt 4", "city": "Austin", "state": "TX", "zip": "78701"}},
                            {"name": "Bo", "shipping": {"address1": "XJ 12 Mainn Street", "city": "Austin", "state": "TX", "zip": "78701"}},
                            {"name": "Cy", "shipping": {"address1": "9 Elm St", "city": "Austin", "state": "TX", "zip": "78701"}}]), "Hayha")
    hook(c, "a", "Target", "Ana", "ISP A", "isp.example.net:8000", number="9001")
    hook(c, "b", "Target", "Bo", "ISP A", "isp.example.net:8000", ts="2026-10-20T15:01:00Z")
    hook(c, "c", "Target", "Cy", "ISP A", "isp.example.net:8000", ts="2026-10-20T15:02:00Z")
    mail(store, "a1", "Target", "ordered", "2026-10-20T15:05:00+00:00", "9001")
    client = create_app(str(tmp_path / "m.db")).test_client()
    profile = client.get("/profile-orders?profile=Ana").get_data(as_text=True)
    assert "Checkouts on this profile" in profile and 'value="not_order"' in profile
    link = profile.split('href="/address?name=')[1].split('"')[0]
    page = client.get("/address?name=" + link).get_data(as_text=True)
    assert "Profiles here" in page and ">Ana<" in page and ">Bo<" in page and ">Cy<" not in page
    assert page.count('action="/drop/') == 2  # both checkouts, each correctable
    assert "Everything at this address" in client.get("/profiles").get_data(as_text=True)
    assert "Open profile" in client.get("/drop?day=2026-10-20&site=Target&by=profile&name=Ana").get_data(as_text=True)
    assert client.get("/address").status_code == 200
