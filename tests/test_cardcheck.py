from jigstream import bank, cardcheck, drop
from jigstream.app import create_app
from jigstream.orderstore import Store

from test_stuck import checkout, setup


def charges(c, mask, rows):
    acct = bank.account_for(c, "Card " + mask, mask=mask)
    bank.import_rows(c, acct, [dict({"category": "", "time": None}, **r) for r in rows], "file")


def priced(c, fp, price):
    c.execute("UPDATE checkouts SET price = ? WHERE fingerprint = ?", (price, fp))
    c.commit()


def runs_by(r):
    return {(run["store"], run["amount"], run["first"].isoformat(), run["card"]["last4"]): run for run in r["runs"]}


def test_charges_minus_refunds_per_card_store_and_price(tmp_path):
    c, store = setup(tmp_path)
    c.execute("UPDATE profiles SET card_last4 = '4242' WHERE name = 'Ana'")
    c.execute("UPDATE profiles SET card_last4 = '1111' WHERE name = 'Jacob 1'")
    # Five Target checkouts on Ana's card; the card shows four charges (one never went through) and one refund.
    for i in range(5):
        checkout(c, f"t{i}", "Target", "Ana", ts=f"2026-10-20T15:0{i}:00Z")
        priced(c, f"t{i}", "$49.99")
    charges(c, "4242", [{"date": "2026-10-21", "description": "TARGET.COM * 800-591-3869 MN", "amount": 54.11}] * 4
            + [{"date": "2026-10-23", "description": "TARGET.COM * 800-591-3869 MN", "amount": -54.11},
               {"date": "2026-10-21", "description": "TARGET.COM", "amount": 54.11, "pending": True}])
    # A month and a half later the same amount at Target with no checkout: the price says what it was.
    charges(c, "4242", [{"date": "2026-12-15", "description": "TARGET 00012", "amount": 54.11}])
    # One Walmart checkout, one charge on that profile's card: the only one of each, so they're tied.
    checkout(c, "w", "Walmart", "Jacob 1", ts="2026-10-19T12:00:00Z")
    priced(c, "w", "$20.00")
    charges(c, "1111", [{"date": "2026-10-19", "description": "WALMART.COM 8009256278", "amount": 21.65}])
    # A Walmart charge on Ana's card can't be Jacob 1's checkout.
    charges(c, "4242", [{"date": "2026-10-19", "description": "WALMART.COM 8009256278", "amount": 21.65}])
    r = cardcheck.report(c)
    runs = runs_by(r)
    t = runs[("target", 54.11, "2026-10-21", "4242")]
    assert (len(t["charges"]), t["refunded"], t["went_through"], t["bot"], t["never_charged"]) == (4, 1, 3, 5, 1)
    assert t["linked"] == 0 and t["product"] == "Pokemon ETB" and t["product_from"] == "bot"
    assert t["drop_day"] and t["card"]["last4"] == "4242"
    later = runs[("target", 54.11, "2026-12-15", "4242")]
    assert later["bot"] == 0 and later["product"] == "Pokemon ETB" and later["product_from"] == "price"
    w = runs[("walmart", 21.65, "2026-10-19", "1111")]
    assert w["card"]["last4"] == "1111" and w["bot"] == 1 and w["linked"] == 1
    assert w["charges"][0]["linked_by"] == "the only charge and checkout"
    other = runs[("walmart", 21.65, "2026-10-19", "4242")]
    assert other["bot"] == 0 and other["linked"] == 0
    cards = {a["last4"]: a for a in r["cards"]}
    assert cards["4242"]["went_through"] == 5 and cards["4242"]["profiles"] == ["Ana"]
    # The tied Walmart checkout had no order email (Unsure); its kept charge makes it Good.
    rows = {x["fingerprint"]: x for x in drop.rows(c)}
    assert rows["w"]["outcome"] == "confirmed" and rows["w"]["card"]["by"] == "the only charge and checkout"
    assert rows["t1"]["card"] is None


def test_charge_times_tie_single_checkouts(tmp_path):
    c, store = setup(tmp_path)
    c.execute("UPDATE profiles SET card_last4 = '4242' WHERE name = 'Ana'")
    checkout(c, "a", "Best Buy", "Ana", ts="2026-10-18T12:00:00Z")
    checkout(c, "b", "Best Buy", "Ana", ts="2026-10-18T12:40:00Z")
    checkout(c, "z", "Best Buy", "Ana", ts="2026-10-18T13:30:00Z")
    for fp in "abz":
        priced(c, fp, "$100.00")
    charges(c, "4242", [{"date": "2026-10-18", "description": "BESTBUY.COM 888-237-8289", "amount": 108.25,
                         "time": "2026-10-18T12:41:00Z"},
                        {"date": "2026-10-18", "description": "BESTBUY.COM 888-237-8289", "amount": 108.25,
                         "time": "2026-10-18T12:02:00Z"},
                        {"date": "2026-10-20", "description": "BESTBUY.COM 888-237-8289", "amount": -108.25}])
    r = cardcheck.report(c)
    run = r["runs"][0]
    assert run["bot"] == 3 and run["linked"] == 2 and run["went_through"] == 1 and run["never_charged"] == 1
    tied = {tx["checkout"]["fingerprint"]: tx for tx in run["charges"]}
    assert set(tied) == {"a", "b"} and tied["a"]["time"].startswith("2026-10-18T12:02")
    assert tied["a"]["linked_by"] == "charged 2 min after" and tied["b"]["linked_by"] == "charged 1 min after"
    # The refund gives back the later charge, so b is the cancelled one.
    assert r["links"][tied["b"]["checkout"]["id"]]["refunded"] and not r["links"][tied["a"]["checkout"]["id"]]["refunded"]


def test_price_fits_tax_and_shipping():
    assert cardcheck.price_fits(54.11, 49.99) is not None
    assert cardcheck.price_fits(49.99, 49.99) == 0
    assert cardcheck.price_fits(10.98, 4.99) is not None  # flat shipping
    assert cardcheck.price_fits(40.00, 49.99) is None and cardcheck.price_fits(80.00, 49.99) is None


def test_pages(tmp_path):
    c, store = setup(tmp_path)
    client = create_app(str(tmp_path / "m.db")).test_client()
    assert "Import a card statement" in client.get("/drop/cards").get_data(as_text=True)
    c.execute("UPDATE profiles SET card_last4 = '4242' WHERE name = 'Ana'")
    checkout(c, "a", "Target", "Ana", ts="2026-10-20T15:00:00Z")
    priced(c, "a", "$49.99")
    charges(c, "4242", [{"date": "2026-10-21", "description": "TARGET.COM * 800-591-3869 MN", "amount": 54.11}])
    page = client.get("/drop/cards").get_data(as_text=True)
    assert "$54.11" in page and "Pokemon ETB" in page and "the only charge and checkout" in page and "4242" in page
    day = drop.report(Store(c).db)["drops"][0]["days"][0]["day"]
    page = client.get(f"/drop?site=Target&day={day}").get_data(as_text=True)
    assert "Cards say" in page and "Card charged $54.11" in page and 'href="/drop/cards"' in page
    assert 'id="card-check"' in client.get("/manual").get_data(as_text=True)
