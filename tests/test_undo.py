from jigstream import db, undo
from jigstream.app import create_app


def _names(path):
    return sorted(r[0] for r in db.connect(path).execute("SELECT name FROM profiles"))


def test_undo_and_redo_any_number_of_steps(tmp_path):
    path = str(tmp_path / "u.db")
    app = create_app(path)
    c = app.test_client()
    form = {"name": "Main", "first_name": "Jo", "last_name": "Bee", "ship_address1": "1 Elm St", "ship_city": "Austin",
            "ship_state": "TX", "ship_zip": "78701", "ship_country": "US"}
    c.post("/profiles/save", data=form)
    c.post("/profiles/save", data={**form, "name": "Second"})
    c.post("/profiles/save", data={**form, "original": "Main", "first_name": "Joe"})
    c.post("/profiles/Second/delete")
    assert _names(path) == ["Main"]

    con = db.connect(path)
    assert undo.state(con)["undo_count"] == 4 and undo.state(con)["redo_count"] == 0
    c.post("/undo")  # the delete
    assert _names(path) == ["Main", "Second"]
    c.post("/undo")  # the edit
    assert db.connect(path).execute("SELECT first_name FROM profiles WHERE name='Main'").fetchone()[0] == "Jo"
    assert undo.state(con)["redo_count"] == 2
    c.post("/redo")
    assert db.connect(path).execute("SELECT first_name FROM profiles WHERE name='Main'").fetchone()[0] == "Joe"
    c.post("/undo")
    c.post("/undo")
    c.post("/undo")
    assert _names(path) == []
    r = c.post("/undo", headers={"Accept": "application/json"}).get_json()
    assert r["done"] is None and r["undo_count"] == 0 and r["redo_count"] == 4
    for _ in range(4):
        c.post("/redo")
    assert _names(path) == ["Main"]
    # A new change after undoing ends the redo branch.
    c.post("/undo")
    c.post("/profiles/save", data={**form, "name": "Third"})
    assert undo.state(con)["redo_count"] == 0


def test_changes_outside_page_actions_are_not_steps(tmp_path):
    path = str(tmp_path / "u.db")
    app = create_app(path)
    con = db.connect(path)  # no request: like the sync thread
    con.execute("INSERT INTO profiles (name) VALUES ('fromsync')")
    assert undo.state(con)["undo_count"] == 0
    with app.test_client() as c:
        assert c.get("/undo/state").get_json()["undo"] is None


def test_replace_and_quotes_round_trip(tmp_path):
    path = str(tmp_path / "u.db")
    create_app(path)
    con = db.connect(path)
    con.execute("INSERT INTO profiles (name, first_name) VALUES ('a', 'O''Neil')")
    undo.record(con, 1)
    con.execute("INSERT OR REPLACE INTO profiles (name, first_name) VALUES ('a', 'new')")
    con.execute("DELETE FROM undo_cur")
    con.execute("INSERT INTO undo_steps VALUES (1, 'undo', 'replace', '', '')")
    undo.step(con, "undo")
    assert con.execute("SELECT first_name FROM profiles WHERE name='a'").fetchone()[0] == "O'Neil"
    undo.step(con, "redo")
    assert con.execute("SELECT first_name FROM profiles WHERE name='a'").fetchone()[0] == "new"
