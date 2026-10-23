from jigstream import db, profilebook, profilematch
from jigstream.app import create_app
from jigstream.extract import normalize
from jigstream.mail import Email, parse_message
from jigstream.orderstore import Store


def profile(c, name, first, last, email, a1, zip_="78701", a2="", city="Austin"):
    form = {"name": name, "first_name": first, "last_name": last, "email": email, "bill_address1": a1,
            "bill_address2": a2, "bill_city": city, "bill_state": "TX", "bill_zip": zip_, "bill_country": "US",
            "same": "on"}
    assert profilebook.save_profile(c, form)[1] is None


def order(store, number, address, name=None, to=None, retailer="Target", status="ordered", account="inbox@gmail.com"):
    e = Email(f"<{number}@x>", f"{retailer} <orders@x.com>", f"Order {number}", "2026-10-01T10:00:00+00:00",
              "Total $50.00", account, to or "")
    r = normalize({"kind": "order", "retailer": retailer, "order_number": number, "shipping_address": address,
                   "shipping_name": name, "shipping_status": status}, e)
    store.save(e, r)
    return store.db.execute("SELECT id FROM tracked_orders WHERE order_number=?", (number,)).fetchone()[0]


def matched(c, oid):
    return c.execute("SELECT profile_name, profile_match, profile_candidates FROM tracked_orders WHERE id=?",
                     (oid,)).fetchone()


def setup(tmp_path):
    c = db.connect(str(tmp_path / "m.db"))
    store = Store(c)
    # Two profiles at one house, two people; a third shares Jacob's name at another address.
    profile(c, "Jacob 1", "Jacob", "Smith", "jacob.smith+t1@gmail.com", "123 Main Street", a2="Apt 4")
    profile(c, "Jacob 2", "Jacob", "Smith", "jsmith.alt@outlook.com", "123 Main Street", a2="Apt 4")
    profile(c, "Ana", "Ana", "Lopez", "ana@x.com", "123 Main St")
    profile(c, "Jacob Dallas", "Jacob", "Smith", "jd@x.com", "77 Elm Ave", zip_="75201", city="Dallas")
    return c, store


def test_address_formatting_and_name_pick_the_profile(tmp_path):
    c, store = setup(tmp_path)
    oid = order(store, "A1", "ANA LOPEZ, 123 Main St., Austin, Texas 78701-1234")
    profilematch.match_orders(c)
    assert matched(c, oid)["profile_name"] == "Ana"
    assert matched(c, oid)["profile_match"] == "address + name"
    # Same name, other address: the address decides.
    oid = order(store, "A2", "77 ELM AVENUE DALLAS TX 75201", name="Jacob Smith")
    profilematch.match_orders(c)
    assert matched(c, oid)["profile_name"] == "Jacob Dallas"


def test_shared_address_and_name_fall_back_to_email_or_ask(tmp_path):
    c, store = setup(tmp_path)
    both = order(store, "B1", "Jacob Smith 123 Main St Apt 4 Austin TX 78701")
    by_email = order(store, "B2", "Jacob Smith 123 Main St Apt 4 Austin TX 78701", to="Jacob <JacobSmith@gmail.com>")
    profilematch.match_orders(c)
    row = matched(c, both)
    assert row["profile_name"] is None and set(store.order(both)["profile_candidates"]) == {"Jacob 1", "Jacob 2"}
    assert matched(c, by_email)["profile_name"] == "Jacob 1"  # gmail dots and +tags ignored
    nothing = order(store, "B3", "9 Other Rd, Houston TX 77002", name="Someone Else")
    profilematch.match_orders(c)
    assert matched(c, nothing)["profile_name"] is None


def test_bot_checkout_order_number_wins_and_hand_picks_stick(tmp_path):
    c, store = setup(tmp_path)
    c.execute("INSERT INTO checkouts (fingerprint, status, site, profile, order_number) VALUES ('f','success','Target','Jacob 2','#C-77')")
    oid = order(store, "C77", "Jacob Smith 123 Main St Apt 4 Austin TX 78701")
    profilematch.match_orders(c)
    assert matched(c, oid)["profile_name"] == "Jacob 2"
    other = order(store, "C78", "Jacob Smith 123 Main St Apt 4 Austin TX 78701")
    profilematch.set_profile(c, other, "Jacob 1")
    profilematch.match_orders(c)
    assert matched(c, other)["profile_name"] == "Jacob 1"
    # Renaming the profile carries its orders along.
    form = dict(c.execute("SELECT * FROM profiles WHERE name='Jacob 1'").fetchone())
    form = {k: v or "" for k, v in form.items()}
    form["name"] = "Jacob Main"
    profilebook.save_profile(c, form, "Jacob 1")
    assert matched(c, other)["profile_name"] == "Jacob Main"


def test_to_header_is_read():
    raw = b"Message-ID: <1@x>\r\nFrom: shop@x.com\r\nTo: Jacob <j.smith@gmail.com>\r\nSubject: hi\r\n\r\nbody"
    assert parse_message(raw).to == "j.smith@gmail.com"


def test_pages(tmp_path):
    path = str(tmp_path / "m.db")
    c, store = setup(tmp_path)
    c.close()
    c = db.connect(path)
    store = Store(c)
    profile(c, "Ana B", "Ana", "Lopez", "anab@x.com", "5 Oak Dr")
    oid = order(store, "D1", "Ana Lopez, 5 Oak Dr, Austin TX 78701", retailer="Walmart", status="delivered")
    c.execute("INSERT INTO checkouts (fingerprint, status, site, profile) VALUES ('g','success','Best Buy','Ana B')")
    c.commit()
    client = create_app(path).test_client()
    page = client.get("/orders").get_data(as_text=True)
    assert "Ana B" in page and "address + name" in page
    page = client.get("/profile-orders?profile=Ana%20B").get_data(as_text=True)
    assert "Walmart" in page and "Best Buy" in page and "Delivered" in page
    assert "Ana B" in client.get("/profile-orders").get_data(as_text=True)
    assert "1 order from your inboxes" in client.get("/profiles").get_data(as_text=True)
    client.post(f"/orders/{oid}/profile", data={"profile": ""})
    assert matched(c, oid)["profile_name"] is None
    client.post(f"/orders/{oid}/profile", data={"profile": "__auto__"})
    assert matched(c, oid)["profile_name"] == "Ana B"
