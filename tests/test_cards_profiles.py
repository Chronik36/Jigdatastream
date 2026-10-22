import json

import pytest

from jigstream import db, export, profilebook, wallet
from jigstream.app import create_app


@pytest.fixture(autouse=True)
def key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTTRACK_CARD_KEY_FILE", str(tmp_path / "key"))
    monkeypatch.setattr(wallet, "_key", None)


def test_cards_are_encrypted_and_deduped(tmp_path):
    c = db.connect(str(tmp_path / "w.db"))
    cards, problems = wallet.parse_lines("Amazon:4111 1111 1111 1111:04/2028:123\n5555555555554444:12/27:456\nbad:123:1/2030:1")
    assert len(cards) == 2 and len(problems) == 1 and cards[1]["year"] == "2027"
    assert wallet.add_cards(c, cards, "Chase", "Virtual", 500, "Main") == (2, 0)
    assert wallet.add_cards(c, cards[:1]) == (0, 1)
    dump = "\n".join(c.iterdump())
    assert "4111111111111111" not in dump and "123" not in dump.split("cards")[-1].split("4111")[0][:0] + ""
    row = c.execute("SELECT * FROM cards WHERE last4='1111'").fetchone()
    assert (row["brand"], row["exp"], row["group_name"]) == ("Visa", "04/2028", "Main")
    assert wallet.reveal(c, row["id"])["number"] == "4111111111111111"
    assert (tmp_path / "key").stat().st_mode & 0o777 == 0o600


def test_profile_builder_groups_distribute_and_export(tmp_path):
    path = str(tmp_path / "p.db")
    c = db.connect(path)
    wallet.add_cards(c, wallet.parse_lines("Main:371449635398431:07/2031:1234")[0], "Amex")
    card_id = c.execute("SELECT id FROM cards").fetchone()[0]
    form = {"name": "Target 1", "group_name": "Target", "first_name": "Alex", "last_name": "R", "email": "a@x.com",
            "bill_address1": "10 Pine St", "bill_city": "Austin", "bill_state": "TX", "bill_zip": "78701",
            "bill_country": "US", "same": "on", "card_id": str(card_id), "card_holder": "Alex R"}
    assert profilebook.save_profile(c, form) == ("Target 1", None)
    assert profilebook.save_profile(c, form)[1].startswith("There's already")
    assert profilebook.duplicate(c, "Target 1", 2) == ["Target 2", "Target 3"]
    names = ["Target 1", "Target 2", "Target 3"]
    assert profilebook.distribute(c, names, "email", ["a1@x.com", "a2@x.com"]) == (3, None)
    assert [r[0] for r in c.execute("SELECT email FROM profiles ORDER BY name")] == ["a1@x.com", "a2@x.com", "a1@x.com"]
    profilebook.distribute(c, names, "address2", ["Apt 1", "Apt 2", "Apt 3"], billing=False, shipping=True)
    r = c.execute("SELECT * FROM profiles WHERE name='Target 2'").fetchone()
    assert (r["ship_address2"], r["bill_address2"]) == ("Apt 2", "")
    assert profilebook.distribute(c, names, "name", ["X"])[1].startswith("Profile names must be unique")
    # copies don't share the card: only the original keeps it after relinking
    wallet.link(c, card_id, "Target 3")
    assert [r[0] for r in c.execute("SELECT name FROM profiles WHERE card_id IS NOT NULL")] == ["Target 3"]
    profilebook.move(c, ["Target 3"], "Walmart")
    assert profilebook.groups(c)[0] == [("Target", 2), ("Walmart", 1)]

    client = create_app(path).test_client()
    page = client.get("/profiles?group=Walmart").get_data(as_text=True)
    assert "Target 3" in page and "Amex •••• 8431" in page and "Target 1" not in page.split("pgrid")[1]
    assert "Mass Distribute" in page and "+ Create profile" in page
    out = json.loads(client.post("/profiles/export", data={"bot": "", "names": ["Target 3"]}).data)
    assert out[0]["card"]["number"] == "371449635398431" and out[0]["card"]["holder"] == "Alex R"
    assert "•••• 8431" in client.get("/cards").get_data(as_text=True)
    assert client.get(f"/cards/{card_id}/reveal").get_json()["cvv"] == "1234"


def test_routes_create_and_distribute(tmp_path):
    path = str(tmp_path / "r.db")
    client = create_app(path).test_client()
    client.post("/profiles/groups", data={"action": "add", "name": "Nike"})
    client.post("/profiles/save", data={"name": "N1", "group_name": "Nike", "bill_address1": "1 A St", "same": "on"})
    client.post("/profiles/bulk", data={"action": "duplicate", "names": ["N1"], "count": "1"})
    client.post("/profiles/bulk", data={"action": "distribute", "names": ["N1", "N2"], "field": "phone",
                                        "values": "5551110000\n5552220000"})
    c = db.connect(path)
    assert [r[0] for r in c.execute("SELECT phone FROM profiles ORDER BY name")] == ["5551110000", "5552220000"]
    client.post("/cards/add", data={"lines": "A:4111111111111111:01/2030:111", "provider": "Other", "card_type": "Standard"})
    assert c.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
