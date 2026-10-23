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
    assert stuck.counts(rows.values()) == {"all": 3, "emailed": 1, "cancelled": 1, "no_email": 1, "stuck": 2}


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
