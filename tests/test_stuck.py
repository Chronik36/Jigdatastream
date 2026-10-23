from jigstream import db, stuck
from jigstream.app import create_app
from jigstream.extract import normalize
from jigstream.mail import Email
from jigstream.orderstore import Store

from test_profile_match import profile


def checkout(c, fp, site, prof, number="", ts="2026-10-01T10:00:00Z", email=""):
    c.execute("INSERT INTO checkouts (fingerprint, bot, status, site, product, profile, order_number, email, ts)"
              " VALUES (?, 'Valor', 'success', ?, 'Pokemon ETB', ?, ?, ?, ?)", (fp, site, prof, number, email, ts))
    c.commit()


def mail(store, mid, retailer, status, date, number=None, address=None, name=None):
    e = Email(f"<{mid}@x>", f"{retailer} <orders@x.com>", f"Order {status}", date, "Total $50.00", "inbox@gmail.com", "")
    store.save(e, normalize({"kind": "order", "retailer": retailer, "order_number": number,
                             "shipping_address": address, "shipping_name": name, "shipping_status": status}, e))


def setup(tmp_path):
    c = db.connect(str(tmp_path / "m.db"))
    store = Store(c)
    profile(c, "Jacob 1", "Jacob", "Smith", "jacob@gmail.com", "123 Main Street")
    profile(c, "Ana", "Ana", "Lopez", "ana@x.com", "9 Oak Ave", zip_="75201", city="Dallas")
    return c, store


def by_fp(rows):
    return {r["fingerprint"]: r for r in rows}


def test_order_number_then_site_and_profile(tmp_path):
    c, store = setup(tmp_path)
    # Order number on both sides; the cancel email follows the confirmation.
    checkout(c, "a", "Target", "Jacob 1", "#9001")
    mail(store, "a1", "Target", "ordered", "2026-10-01T10:05:00+00:00", "9001")
    mail(store, "a2", "Target", "cancelled", "2026-10-02T08:00:00+00:00", "9001")
    # No order number in the webhook: matched by site + profile (from the address) and time.
    checkout(c, "b", "target.com", "Ana", ts="2026-10-03T12:00:00Z")
    mail(store, "b1", "Target", "shipped", "2026-10-04T09:00:00+00:00", "T-55", "Ana Lopez, 9 Oak Ave, Dallas TX 75201")
    # Same profile and site, but the email is weeks later: not this checkout.
    checkout(c, "c", "Walmart", "Jacob 1", ts="2026-09-01T12:00:00Z")
    mail(store, "c1", "Walmart", "cancelled", "2026-09-20T09:00:00+00:00", "W-1", "Jacob Smith, 123 Main St, Austin TX 78701")
    # A failed checkout is not listed.
    c.execute("INSERT INTO checkouts (fingerprint, status, site, profile) VALUES ('d', 'failure', 'Target', 'Ana')")
    rows = by_fp(stuck.cross_reference(c))
    assert set(rows) == {"a", "b", "c"}
    assert rows["a"]["state"] == "cancelled" and rows["a"]["matched_on"] == "order number"
    assert rows["b"]["state"] == "emailed" and rows["b"]["order"]["order_number"] == "T-55"
    assert rows["b"]["matched_on"] == "same site and profile"
    assert rows["c"]["state"] == "no_email"
    assert stuck.counts(rows.values()) == {"all": 3, "emailed": 1, "cancelled": 1, "no_email": 1, "not_order": 0, "stuck": 2}


def test_two_checkouts_of_one_drop_dont_share_an_email(tmp_path):
    c, store = setup(tmp_path)
    checkout(c, "x", "Best Buy", "Ana", ts="2026-10-05T10:00:00Z")
    checkout(c, "y", "Best Buy US", "Ana", ts="2026-10-05T10:01:00Z")
    mail(store, "x1", "Best Buy", "cancelled", "2026-10-05T10:02:00+00:00", "BBY01", "Ana Lopez, 9 Oak Ave, Dallas TX 75201")
    rows = by_fp(stuck.cross_reference(c))
    assert sorted(r["state"] for r in rows.values()) == ["cancelled", "no_email"]
    assert rows["y"]["state"] == "cancelled"  # the closer one in time


def test_different_order_numbers_never_match(tmp_path):
    c, store = setup(tmp_path)
    checkout(c, "a", "Target", "Ana", "111")
    mail(store, "a1", "Target", "cancelled", "2026-10-01T11:00:00+00:00", "222", "Ana Lopez, 9 Oak Ave, Dallas TX 75201")
    assert stuck.cross_reference(c)[0]["state"] == "no_email"


def test_site_names():
    assert stuck.same_site("www.target.com", "Target")
    assert stuck.same_site("Pokemon Center", "pokemoncenter.com")
    assert not stuck.same_site("Target", "Walmart")
    assert not stuck.same_site("", "Walmart")


def test_pages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(str(tmp_path / "m.db"))
    client = app.test_client()
    assert "No successful checkouts yet" in client.get("/stuck").get_data(as_text=True)
    with app.app_context():
        c = db.connect(str(tmp_path / "m.db"))
        store = Store(c)
        checkout(c, "a", "Target", "Ana", "#9001")
        checkout(c, "b", "Walmart", "Ana", "#7")
        mail(store, "a1", "Target", "cancelled", "2026-10-01T11:00:00+00:00", "9001")
    page = client.get("/stuck").get_data(as_text=True)
    assert "Walmart" in page and "No email found" in page and "#9001" not in page
    page = client.get("/stuck?show=cancelled").get_data(as_text=True)
    assert "#9001" in page and "Walmart" not in page
    assert "Cancelled" in client.get("/checkouts").get_data(as_text=True)
    assert 'href="/stuck"' in page and 'id="stuck"' in client.get("/manual").get_data(as_text=True)


def refund(c, desc, amount, day, mask=None):
    from jigstream import bank
    acct = bank.account_for(c, "Card " + (mask or "x"), mask=mask)
    bank.import_rows(c, acct, [{"date": day, "description": desc, "amount": -amount, "category": ""}], "file")


def test_bank_refund_cancels_a_checkout(tmp_path):
    c, store = setup(tmp_path)
    c.execute("UPDATE profiles SET card_last4='4242' WHERE name='Ana'")
    # Target refunds at once; no cancel email came. The refund carries tax on top of the bot's price.
    checkout(c, "a", "Target", "Ana", ts="2026-10-20T15:00:00Z")
    c.execute("UPDATE checkouts SET price='$49.99' WHERE fingerprint='a'")
    refund(c, "TARGET.COM * 800-591-3869 MN", 54.11, "2026-10-21", mask="4242")
    # Same store and amount, but the refund went to another card.
    checkout(c, "b", "Target", "Ana", ts="2026-10-20T15:00:00Z")
    c.execute("UPDATE checkouts SET price='$20.00' WHERE fingerprint='b'")
    refund(c, "TARGET.COM", 20.00, "2026-10-21", mask="1111")
    # An order email's total wins over the bot's price; another store's refund doesn't count.
    checkout(c, "d", "Walmart", "Ana", "W-9", ts="2026-10-20T15:00:00Z")
    mail(store, "d1", "Walmart", "ordered", "2026-10-20T15:01:00+00:00", "W-9")
    c.execute("UPDATE tracked_orders SET total=88.20 WHERE order_number='W-9'")
    refund(c, "WALMART.COM 8009256278", 88.20, "2026-10-22")
    refund(c, "BEST BUY 00012", 88.20, "2026-10-22")
    c.commit()
    rows = by_fp(stuck.cross_reference(c))
    assert rows["a"]["state"] == "cancelled" and rows["a"]["refund"]["amount"] == -54.11
    assert rows["b"]["state"] == "no_email" and rows["b"]["refund"] is None
    assert rows["d"]["state"] == "cancelled" and "WALMART" in rows["d"]["refund"]["description"]
    page = create_app(str(tmp_path / "m.db")).test_client().get("/stuck?show=cancelled").get_data(as_text=True)
    assert "$54.11" in page and "$88.20" in page


def test_hayha_is_read_by_embed_color(tmp_path):
    from jigstream import parsers
    green = {"username": "Hayha", "embeds": [{"title": "Successful Checkout!", "color": 0x2ECC71,
                                              "fields": [{"name": "Site", "value": "Target"}, {"name": "Profile", "value": "Ana"}]}]}
    red = {"username": "Hayha", "embeds": [{"title": "Successful Checkout!", "color": 0xE74C3C, "description": "Card declined",
                                            "fields": [{"name": "Site", "value": "Target"}, {"name": "Profile", "value": "Ana"}]}]}
    import json
    recs = parsers.parse_text(json.dumps([green, red]))
    assert [r["status"] for r in recs] == ["success", "decline"]
    assert recs[1]["reason"] == "Card declined"
    # Another bot keeps reading the text.
    other = {"username": "Valor", "embeds": [dict(red["embeds"][0], description="")]}
    assert parsers.parse_text(json.dumps(other))[0]["status"] == "success"
    # Hayha declines stored before this rule are fixed when the app opens the database.
    c = db.connect(str(tmp_path / "old.db"))
    rec = parsers.parse_text(json.dumps(red))[0]
    rec.update(status="success", fingerprint="old", reason="")
    c.execute("INSERT INTO checkouts (fingerprint,bot,status,reason,site,product,size,price,profile,order_number,email,ts,raw)"
              " VALUES (:fingerprint,:bot,:status,:reason,:site,:product,:size,:price,:profile,:order_number,:email,:ts,:raw)", rec)
    c.commit()
    assert parsers.reclassify(c) == 1
    assert c.execute("SELECT status, reason FROM checkouts").fetchone()[:] == ("decline", "Card declined")
    assert parsers.ingest(c, json.dumps(red)) == 0  # importing it again doesn't add a copy


def test_hayha_cancel_reason_and_item_price():
    import json
    from jigstream import parsers
    # Shaped like jacob's real Hayha webhooks: both say "Successful Checkout!".
    fields = [("Site", "Target"), ("Item", "Pok&#233;mon Trading Card Game: 30th Celebration Booster Bundle Box - $63.98"),
              ("Mode", "onlineSafe (api) - Global Monitor"), ("Quantity", "2"), ("Profile Name", "Ana"),
              ("Order Number", "912000111"), ("Account", "ana@x.com")]
    embed = lambda color, extra: {"title": "Successful Checkout!", "description": "Way to go", "color": color,
                                  "footer": {"text": "HayhaAIO 2.12.59"},
                                  "fields": [{"name": k, "value": v} for k, v in fields + extra]}
    ok, bad = parsers.parse_text(json.dumps({"username": "HayhaAIO", "embeds": [
        embed(0x2ECC71, []), embed(0xE74C3C, [("Cancel Reason", "Policy - Item Demand")])]}))
    assert (ok["status"], ok["bot"], ok["price"]) == ("success", "Hayha", "$63.98")
    assert ok["product"] == "Pokémon Trading Card Game: 30th Celebration Booster Bundle Box"
    assert (ok["profile"], ok["order_number"], ok["email"]) == ("Ana", "912000111", "ana@x.com")
    assert (bad["status"], bad["reason"]) == ("decline", "Policy - Item Demand")
    # Even with no color, a Cancel Reason is a decline.
    assert parsers.classify(embed(None, [("Cancel Reason", "Policy - Item Demand")])) == "decline"


def test_declines_show_earlier_checkouts_on_the_profile_or_email(tmp_path):
    c, store = setup(tmp_path)
    checkout(c, "s1", "Target", "Ana", "#1", ts="2026-10-01T10:00:00Z")
    mail(store, "m1", "Target", "cancelled", "2026-10-01T11:00:00+00:00", "1")
    checkout(c, "s2", "Walmart", "Other", ts="2026-10-02T10:00:00Z", email="a.na+x@gmail.com")
    checkout(c, "s3", "Target", "Ana", ts="2026-10-30T10:00:00Z")  # after the decline: not counted
    for fp, prof, email, ts in (("d1", "Ana", "ana@gmail.com", "2026-10-05T10:00:00Z"),
                                ("d2", "New", "", "2026-10-06T10:00:00Z")):
        c.execute("INSERT INTO checkouts (fingerprint, bot, status, site, profile, email, ts) VALUES (?, 'Hayha', 'decline',"
                  " 'Target', ?, ?, ?)", (fp, prof, email, ts))
    c.commit()
    ids = {r["fingerprint"]: r["id"] for r in c.execute("SELECT id, fingerprint FROM checkouts")}
    h = stuck.decline_history(c)
    assert h[ids["d1"]]["checkouts"] == 2 and h[ids["d1"]]["cancelled"] == 1 and h[ids["d1"]]["stuck"] == 1
    assert h[ids["d1"]]["last"]["site"] == "Walmart" and h[ids["d1"]]["orders"] == 1  # the Target email, matched to Ana by its order number
    assert h[ids["d2"]]["checkouts"] == 0
    page = create_app(str(tmp_path / "m.db")).test_client().get("/checkouts?status=decline").get_data(as_text=True)
    assert "Checked out 2x before" in page and "Never checked out on this profile/email before" in page


def test_to_address_beats_profile_when_matching(tmp_path):
    c, store = setup(tmp_path)
    checkout(c, "a", "Target", "Ana", ts="2026-10-20T15:00:00Z", email="ana.alt+tg@gmail.com")
    # A closer email that only matches on the profile's address...
    mail(store, "p1", "Target", "ordered", "2026-10-20T15:01:00+00:00", "T-1", "Ana Lopez, 9 Oak Ave, Dallas TX 75201")
    # ...and one sent to the account the bot checked out with (gmail dots and +tags ignored), later.
    e = Email("<t2@x>", "Target <orders@x.com>", "Order cancelled", "2026-10-20T18:00:00+00:00", "", "inbox@gmail.com",
              "anaalt@gmail.com")
    store.save(e, normalize({"kind": "order", "retailer": "Target", "order_number": "T-2",
                             "shipping_status": "cancelled"}, e))
    row = stuck.cross_reference(c)[0]
    assert row["order"]["order_number"] == "T-2" and row["matched_on"] == "same site and To email"
    assert row["state"] == "cancelled"
