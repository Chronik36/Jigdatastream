"""Every order detail can be fixed from where it's shown, and each one says which email it was read from."""
from types import SimpleNamespace

from jigstream import db, inbox, ordersheet
from jigstream.app import create_app
from jigstream.orderstore import Store

from test_flex1028 import Now
from test_profile_match import profile
from test_round4 import order


def setup(tmp_path, monkeypatch, **extra):
    path = str(tmp_path / "c.db")
    store = Store(db.connect(path))
    order(store, 1, "ordered", retailer="Amazon", shipping_address="230 Downs Street, Kingston, NY 12401", **extra)
    profile(store.db, "Jig-1", "Jacob", "Smith", "j@x.com", "6432 State Route 22", zip_="12953", city="Plattsburgh")
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@x:pw")
    monkeypatch.setattr(ordersheet.threading, "Thread", Now)
    labelled = []
    monkeypatch.setattr(inbox, "label_emails", lambda config, st, llm, picks, label, sale=None, what=None:
                        labelled.append((picks[0]["message_id"], label)))
    monkeypatch.setattr("jigstream.ordersheet.from_config", lambda config: None, raising=False)
    monkeypatch.setattr("jigstream.llm.from_config", lambda config: None)
    return path, store, create_app(path).test_client(), store.orders()[0]["id"], labelled


def test_every_status_picker_can_move_an_order_to_expenses_or_sales(tmp_path, monkeypatch):
    path, store, c, oid, labelled = setup(tmp_path, monkeypatch)
    for page in ("/tracking?day=all", "/orders"):
        html = c.get(page).get_data(as_text=True)
        assert 'value="expense"' in html and 'value="sale"' in html and "Botting expense" in html, page
    sheet = c.get(f"/orders/{oid}/sheet").get_data(as_text=True)
    assert sheet.count('value="expense"') >= 2  # the Status picker and the edit form's Status
    c.post(f"/orders/{oid}/label", data={"status": "expense", "back": "1"})
    assert not Store(db.connect(path)).orders() and labelled == [("<1@x>", "expense")]


def test_status_in_the_edit_form_moves_it_too(tmp_path, monkeypatch):
    path, store, c, oid, labelled = setup(tmp_path, monkeypatch)
    c.post(f"/orders/{oid}/edit", data={"email_type": "order", "status": "sale", "back": "1"})
    assert not Store(db.connect(path)).orders() and labelled == [("<1@x>", "sale")]


def test_ship_to_is_fixable_from_shipping_with_profile_addresses_to_pick(tmp_path, monkeypatch):
    path, store, c, oid, labelled = setup(tmp_path, monkeypatch)
    page = c.get("/tracking?day=all").get_data(as_text=True)
    assert f'data-sheet-fix="{oid}"' in page and "230 Downs Street" in page
    sheet = c.get(f"/orders/{oid}/sheet").get_data(as_text=True)
    assert 'list="os-addrs"' in sheet and "6432 State Route 22" in sheet and "Fix address" in sheet
    assert '"Jig-1": "6432 State Route 22' in sheet  # Use the profile's address
    taught = []
    monkeypatch.setattr(ordersheet, "teach", lambda config, order_id, changed, path=None: taught.append(changed))
    c.post(f"/orders/{oid}/edit", data={"shipping_address": "6432 State Route 22, Plattsburgh, NY 12953",
                                        "profile": "Jig-1", "back": "1"})
    o = Store(db.connect(path)).order(oid)
    assert o["shipping_address"].startswith("6432") and o["profile_name"] == "Jig-1"
    assert taught == [{"shipping_address": "6432 State Route 22, Plattsburgh, NY 12953"}]


def test_where_each_detail_came_from(tmp_path, monkeypatch):
    path, store, c, oid, labelled = setup(tmp_path, monkeypatch, tracking_number="TBA123456789")
    body = "Your order N1 of Thing. Ship to: Jacob Smith 230 Downs Street Kingston NY 12401. Order total $20.00"
    monkeypatch.setattr(inbox, "open_email", lambda config, account, mid: SimpleNamespace(subject="Ordered: Thing", body=body))
    d = c.get(f"/orders/{oid}/sources").get_json()
    f = d["fields"]
    assert f["shipping_address"][0]["subject"] == "Order" and "230 Downs Street" in f["shipping_address"][0]["snippet"]
    assert f["order_number"] and f["total"] and f["items"]
    assert f["tracking_number"] == []  # not in its emails: shown as typed by hand or a guess
    assert d["opened"] == 1
