from jigstream import app as appmod, db, emailbook


def test_parse_takes_first_address_per_line_and_dedupes():
    text = "a@outlook.com:pass\nnot an email\nb@outlook.com, extra\nA@outlook.com\n"
    assert emailbook.parse(text) == ["a@outlook.com", "b@outlook.com"]


def test_groups_usage_and_export(tmp_path):
    client = appmod.create_app(str(tmp_path / "e.db")).test_client()
    client.post("/emails/add", data={"lines": "a@x.com\nb@x.com\nc@x.com", "new_group": "Target"})
    c = db.connect(str(tmp_path / "e.db"))
    c.execute("INSERT INTO profiles (name, email) VALUES ('P1', 'B@x.com')")
    c.commit()
    assert emailbook.groups(c) == ([("Target", 3)], 0)
    assert emailbook.addresses(c, "Target") == ["a@x.com", "b@x.com", "c@x.com"]
    assert emailbook.addresses(c, "Target", unused_only=True) == ["a@x.com", "c@x.com"]
    r = client.get("/emails/export?group=Target&unused=1&download=0")
    assert r.data.decode() == "a@x.com\nc@x.com"
    page = client.get("/emails?group=Target").data.decode()
    assert "In use" in page and "P1" in page
    ids = [e["id"] for e in emailbook.emails(c, "Target")]
    client.post("/emails/bulk", data={"ids": ids[:1], "action": "move", "new_group": "Walmart"})
    assert dict(emailbook.groups(c)[0]) == {"Target": 2, "Walmart": 1}
    assert emailbook.update(c, ids[1], "nope") and not emailbook.update(c, ids[1], "z@x.com")
    client.post("/emails/groups", data={"action": "delete", "name": "Walmart", "emails": "delete"})
    assert dict(emailbook.groups(c)[0]) == {"Target": 2}
