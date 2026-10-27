import json
import time
import urllib.error

from cryptography.fernet import Fernet

from jigstream import db, peersync, wallet
from jigstream.app import create_app
from jigstream.orderstore import Store


ACTIVE = []  # the computer whose code is running now


class Computer:
    """One BotTrack: its own database and its own card key."""

    def __init__(self, path, name):
        create_app(str(path))
        self.con = Store(db.connect(str(path))).db
        self.key = Fernet.generate_key()
        self.name = name

    def __enter__(self):
        self._old, wallet._key = wallet._key, self.key
        ACTIVE.append(self)
        return self.con

    def __exit__(self, *a):
        wallet._key = self._old
        ACTIVE.pop()


def network(*computers):
    """A fake link: a post to the other computer runs its side right here, sealed and opened like the real thing."""
    by_url = {f"https://{c.name}.trycloudflare.com": c for c in computers}

    def post(url, path, secret, me, body):
        other = by_url[url]
        token = peersync.seal(secret, body)
        with other as con:
            if path == "/sync/peer/hello":
                secret2, answer = peersync.hello(con, me.removeprefix("invite:"), token)
            else:
                peer = dict(con.execute("SELECT * FROM sync_peers WHERE site=?", (me,)).fetchone())
                answer = peersync.receive(con, peer, peersync.unseal(peer["secret"], token))
        return peersync.unseal(secret, peersync.seal(secret, answer))
    return post


def link(a, b, post, monkeypatch):
    monkeypatch.setattr(peersync, "my_url", lambda: f"https://{ACTIVE[-1].name}.trycloudflare.com")
    monkeypatch.setattr(peersync, "sync_topic", lambda: "bottrack-sync-test")
    current = [a]
    with a as con:
        code = peersync.new_invite(con)
    current[0] = b
    with b as con:
        peersync.join(con, code, post=post)
    return current


def swap(a, b, post, current):
    current[0] = a
    with a as con:
        return peersync.swap(con, peersync.peers(con)[0], post=post)


def setup(tmp_path, monkeypatch):
    a, b = Computer(tmp_path / "mac.db", "mac"), Computer(tmp_path / "pc.db", "pc")
    post = network(a, b)
    current = link(a, b, post, monkeypatch)
    return a, b, post, current


def order(con, number, total=45.0):
    cur = con.execute("INSERT INTO tracked_orders (merge_key, retailer, order_number, total, message_ids) VALUES (?,?,?,?,?)",
                      (f"target:{number}", "Target", number, total, json.dumps([f"<{number}@x>"])))
    return cur.lastrowid


def test_linking_needs_a_fresh_code(tmp_path, monkeypatch):
    a, b, post, current = setup(tmp_path, monkeypatch)
    with a as con:
        assert [p["name"] for p in peersync.peers(con)] and peersync.peers(con)[0]["url"] == "https://pc.trycloudflare.com"
    with b as con:
        assert peersync.peers(con)[0]["url"] == "https://mac.trycloudflare.com"
    for bad in ("hello", "BT1-abc", ""):
        try:
            peersync.read_code(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_changes_go_both_ways_and_links_follow_their_rows(tmp_path, monkeypatch):
    a, b, post, current = setup(tmp_path, monkeypatch)
    with a as con:
        con.execute("INSERT INTO tracked_orders (merge_key, retailer) VALUES ('filler', 'x')")  # ids differ on each side
        oid = order(con, "1001")
        con.execute("INSERT INTO labels (order_id, status, created_at) VALUES (?, 'delivered', 'now')", (oid,))
    sent, got = swap(a, b, post, current)
    assert sent >= 3
    with b as con:
        row = con.execute("SELECT id, total FROM tracked_orders WHERE order_number='1001'").fetchone()
        assert row["total"] == 45.0
        assert con.execute("SELECT order_id FROM labels").fetchone()[0] == row["id"]
        assert con.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0  # applied quietly: nothing to send back
        con.execute("UPDATE tracked_orders SET shipping_status='Delivered' WHERE id=?", (row["id"],))
    swap(a, b, post, current)
    with a as con:
        assert con.execute("SELECT shipping_status FROM tracked_orders WHERE id=?", (oid,)).fetchone()[0] == "Delivered"
        assert con.execute("SELECT COUNT(*) FROM tracked_orders").fetchone()[0] == 2
    with b as con:
        con.execute("DELETE FROM labels")
    swap(a, b, post, current)
    with a as con:
        assert con.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0


def test_the_newer_change_wins(tmp_path, monkeypatch):
    a, b, post, current = setup(tmp_path, monkeypatch)
    with a as con:
        oid = order(con, "2002")
    swap(a, b, post, current)
    with a as con:
        con.execute("UPDATE tracked_orders SET total=50 WHERE id=?", (oid,))
    time.sleep(0.01)
    with b as con:
        con.execute("UPDATE tracked_orders SET total=60 WHERE order_number='2002'")  # later
    swap(a, b, post, current)
    for c in (a, b):
        with c as con:
            assert con.execute("SELECT total FROM tracked_orders WHERE order_number='2002'").fetchone()[0] == 60


def test_the_same_thing_made_on_both_is_kept_once(tmp_path, monkeypatch):
    a, b, post, current = setup(tmp_path, monkeypatch)
    for c in (a, b):  # both read the same inbox
        with c as con:
            con.execute("INSERT INTO emails (message_id, account, subject, kind) VALUES ('<m1>', 'me@gmail.com', 'Your order', 'order')")
            order(con, "3003")
            con.execute("INSERT INTO profiles (name, first_name) VALUES ('Profile 1', ?)", (c.name,))
        time.sleep(0.01)
    swap(a, b, post, current)
    with b as con:
        con.execute("UPDATE tracked_orders SET carrier='UPS' WHERE order_number='3003'")
    swap(a, b, post, current)
    for c in (a, b):
        with c as con:
            assert con.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*), MAX(carrier) FROM tracked_orders").fetchone()[:] == (1, "UPS")
            assert con.execute("SELECT COUNT(*), MAX(first_name) FROM profiles").fetchone()[:] == (1, "pc")


def test_cards_are_sealed_again_with_each_computers_own_key(tmp_path, monkeypatch):
    a, b, post, current = setup(tmp_path, monkeypatch)
    with a as con:
        wallet.add_cards(con, [{"name": "J", "number": "4111111111111111", "cvv": "123", "month": "01", "year": "2030"}])
        cid = con.execute("SELECT id FROM cards").fetchone()[0]
        con.execute("INSERT INTO profiles (name, card_id) VALUES ('P', ?)", (cid,))
    swap(a, b, post, current)
    with b as con:
        card = con.execute("SELECT * FROM cards").fetchone()
        assert wallet.reveal(con, card["id"])["number"] == "4111111111111111"
        assert card["fingerprint"] == wallet.fingerprint("4111111111111111")
        assert con.execute("SELECT card_id FROM profiles WHERE name='P'").fetchone()[0] == card["id"]
    with a as con:
        assert con.execute("SELECT fingerprint FROM cards").fetchone()[0] != card["fingerprint"]


def test_a_row_waits_for_the_row_it_points_at(tmp_path, monkeypatch):
    a, b, post, current = setup(tmp_path, monkeypatch)
    with a as con:
        oid = order(con, "4004")
        gid = con.execute("SELECT gid FROM sync_ids WHERE tbl='tracked_orders' AND rid=?", (oid,)).fetchone()[0]
        con.execute("INSERT INTO labels (order_id, status) VALUES (?, 'shipped')", (oid,))
        changes, _, _ = peersync.outgoing(con, 0)
    label = [c for c in changes if c["t"] == "labels"]
    parent = [c for c in changes if c["t"] == "tracked_orders"]
    with b as con:
        peer = peersync.peers(con)[0]["id"]
        peersync.apply(con, peer, label)
        assert con.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0
        peersync.apply(con, peer, parent)
        rid = peersync.resolve(con, "tracked_orders", gid)[0]
        assert con.execute("SELECT order_id FROM labels").fetchone()[0] == rid


def test_unlinking_stops_writing_changes_down(tmp_path, monkeypatch):
    a, b, post, current = setup(tmp_path, monkeypatch)
    with a as con:
        peersync.unlink(con, peersync.peers(con)[0]["id"])
        order(con, "5005")
        assert con.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0


def test_the_link_lets_a_linked_computer_in_without_phone_access(tmp_path):
    app = create_app(str(tmp_path / "x.db"))
    c = app.test_client()
    # From the link (cloudflared on this computer), with phone access off: reaches the sealed check, not a 404.
    r = c.post("/sync/peer/swap", data=b"junk", headers={"X-BotTrack-Site": "nobody", "Cf-Connecting-Ip": "1.2.3.4"})
    assert r.status_code == 403
    assert c.get("/m/app", headers={"Cf-Connecting-Ip": "1.2.3.4"}).status_code == 404
    assert c.get("/computers").status_code == 200


def test_a_swap_over_http_through_the_link(tmp_path, monkeypatch):
    """The real pages on both sides: hello and swap, sealed, arriving through the link (cloudflared)."""
    apps = {"mac": create_app(str(tmp_path / "m.db")), "pc": create_app(str(tmp_path / "p.db"))}
    monkeypatch.setattr(peersync, "sync_topic", lambda: "bottrack-sync-test")
    who = []
    monkeypatch.setattr(peersync, "my_url", lambda: f"https://{who[-1]}.trycloudflare.com" if who else "")

    def post(url, path, secret, me, body):
        name = url.split("//")[1].split(".")[0]
        who.append(name)
        try:
            r = apps[name].test_client().post(path, data=peersync.seal(secret, body),
                                              headers={"X-BotTrack-Site": me, "Cf-Connecting-Ip": "1.2.3.4"})
        finally:
            who.pop()
        if r.status_code != 200:
            raise urllib.error.HTTPError(url + path, r.status_code, "no", {}, None)
        return peersync.unseal(secret, r.data)
    monkeypatch.setattr(peersync, "_post", post)
    monkeypatch.setattr(peersync.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    mac, pc = db.connect(str(tmp_path / "m.db")), db.connect(str(tmp_path / "p.db"))
    who.append("mac")
    code = peersync.new_invite(mac)
    who[-1] = "pc"
    peersync.join(pc, code)
    order(pc, "6006")
    sent, got = peersync.swap(pc, peersync.peers(pc)[0])
    assert sent >= 1
    assert mac.execute("SELECT COUNT(*) FROM tracked_orders WHERE order_number='6006'").fetchone()[0] == 1
    try:  # an old code can't be used again
        peersync.join(pc, code)
        raise AssertionError("joined twice")
    except ValueError as e:
        assert "said no" in str(e)


def test_undo_still_works_and_goes_across(tmp_path, monkeypatch):
    a, b, post, current = setup(tmp_path, monkeypatch)
    from jigstream import undo
    with a as con:
        oid = order(con, "7007")
    swap(a, b, post, current)
    with a as con:
        undo.record(con, 1)
        con.execute("UPDATE tracked_orders SET total=99 WHERE id=?", (oid,))
        con.execute("DELETE FROM undo_cur")
        assert not con.execute("SELECT 1 FROM undo_log WHERE sql LIKE '%sync_%'").fetchone()
        for (sql,) in con.execute("SELECT sql FROM undo_log WHERE grp=1 ORDER BY seq DESC").fetchall():
            con.execute(sql)
    swap(a, b, post, current)
    with b as con:
        assert con.execute("SELECT total FROM tracked_orders WHERE order_number='7007'").fetchone()[0] == 45


class FakeBox:
    """A BotTrack-Link label both computers can read, like one Gmail."""

    def __init__(self, label, address="me@gmail.com"):
        self.label, self.address = label, address

    def notes(self):
        return [json.loads(json.dumps(n)) for n in self.label.values()]

    def post(self, site, note):
        self.label.pop(site, None)
        if note is not None:
            self.label[site] = note


def test_computers_on_the_same_gmail_link_by_themselves(tmp_path, monkeypatch):
    from jigstream import gmaillink
    a, b = Computer(tmp_path / "mac.db", "mac"), Computer(tmp_path / "pc.db", "pc")
    post = network(a, b)
    monkeypatch.setattr(peersync, "my_url", lambda: f"https://{ACTIVE[-1].name}.trycloudflare.com")
    monkeypatch.setattr(peersync, "sync_topic", lambda: "bottrack-sync-test")
    label = {}
    with a as con:
        assert gmaillink.turn(con, None, True, [FakeBox(label)]) == []  # first one: nobody else yet
    time.sleep(0.01)  # "since" is to the millisecond: started in the same one, the tie goes to either computer
    with b as con:
        assert len(gmaillink.turn(con, None, True, [FakeBox(label)])) == 1  # found the first
    with a as con:
        gmaillink.meet(con, None, [FakeBox(label)])
        assert [p["url"] for p in peersync.peers(con)] == ["https://pc.trycloudflare.com"]
        order(con, "8008")
    current = [a]
    swap(a, b, post, current)
    with b as con:
        assert con.execute("SELECT COUNT(*) FROM tracked_orders WHERE order_number='8008'").fetchone()[0] == 1
    with a as con:
        gmaillink.turn(con, None, False, [FakeBox(label)])
    assert len(label) == 1  # its note is gone; the other's stays


def test_two_gmail_groups_started_at_once_become_one(tmp_path, monkeypatch):
    from jigstream import gmaillink
    a, b = Computer(tmp_path / "mac.db", "mac"), Computer(tmp_path / "pc.db", "pc")
    monkeypatch.setattr(peersync, "my_url", lambda: "")
    monkeypatch.setattr(peersync, "sync_topic", lambda: "t")
    first, second = {}, {}
    with a as con:
        gmaillink.turn(con, None, True, [FakeBox(first)])
    time.sleep(0.01)
    with b as con:
        gmaillink.turn(con, None, True, [FakeBox(second)])  # couldn't see the first yet
        second.update(first)
        gmaillink.meet(con, None, [FakeBox(second)])
        assert peersync.meta(con, "gmail_group") == first[peersync.site(a.con)]["key"]
