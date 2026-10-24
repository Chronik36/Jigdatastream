"""Your Mac and your PC, each with its own copy of BotTrack, kept the same.

Every change on either computer (an order, a verdict, a card, a training example) is written down as it is made, by
triggers on each table into sync_log. Linked computers swap what's new every minute through the phone link (the free
Cloudflare tunnel), so one of them only needs the link on. Everything sent is sealed with a key only the two
computers know.

- Rows: each one gets an id that's the same on both computers (sync_ids.gid). Numbers that point at another row
  (an order's id on a label, a card's id on a profile) are sent as that row's gid and turned back on arrival.
- Conflicts: the newest change wins, row by row. The same thing made on both (the same email read by both, the same
  card added twice) is recognized by what makes it unique and kept once.
- Cards, bank tokens and dashboard sign-ins stay encrypted with each computer's own key: they're opened to send,
  sealed again on arrival.
- Kept to each computer: Gmail passwords and other settings (.env), Undo history, trained AI files.
"""
import base64
import hashlib
import json
import logging
import os
import platform
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, request, url_for

from . import db, wallet

logger = logging.getLogger("jigstream.peersync")
bp = Blueprint("peersync", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sync_ids (tbl TEXT, rid INTEGER, gid TEXT, at TEXT, PRIMARY KEY (tbl, rid));
CREATE UNIQUE INDEX IF NOT EXISTS sync_ids_gid ON sync_ids (tbl, gid);
CREATE TABLE IF NOT EXISTS sync_alias (tbl TEXT, gid TEXT, to_gid TEXT, PRIMARY KEY (tbl, gid));
CREATE TABLE IF NOT EXISTS sync_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, tbl TEXT, rid INTEGER, gid TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS sync_quiet (on_ INTEGER);
CREATE TABLE IF NOT EXISTS sync_wait (id INTEGER PRIMARY KEY, peer INTEGER, change TEXT, since TEXT);
CREATE TABLE IF NOT EXISTS sync_peers (
    id INTEGER PRIMARY KEY, site TEXT UNIQUE, name TEXT, url TEXT, topic TEXT, secret TEXT, invite TEXT,
    invite_until TEXT, sent_upto INTEGER DEFAULT 0, got_upto INTEGER DEFAULT 0, last_ok TEXT, last_try TEXT,
    error TEXT, sent INTEGER DEFAULT 0, got INTEGER DEFAULT 0, paired_at TEXT
);
"""
# Bookkeeping that belongs to one computer: its Undo history, the phone's change counter, the price re-check
# cursor, and the AI adapters trained here (their files are on this computer only).
LOCAL_TABLES = {"undo_log", "undo_steps", "change_count", "price_recheck", "sqlite_sequence", "ai_adapters"}
LOCAL_COLUMNS = {"ai_tasks": {"adapter_id"}}
# Numbers that point at another table's row.
LINKS = {("ai_task_examples", "task_id"): "ai_tasks", ("bank_transactions", "account_id"): "bank_accounts",
         ("checkout_verdicts", "checkout_id"): "checkouts", ("expenses", "subscription_id"): "subscriptions",
         ("labels", "order_id"): "tracked_orders", ("ledger_rejects", "tx_id"): "bank_transactions",
         ("profiles", "card_id"): "cards", ("proxy_settings", "account_id"): "proxy_accounts",
         ("proxy_tests", "proxy_id"): "proxies", ("sales", "order_id"): "tracked_orders",
         ("training_examples", "label_id"): "labels", ("training_examples", "order_id"): "tracked_orders"}
# Numbers whose table depends on a kind column next to them.
KIND_LINKS = {("bank_transactions", "match_id"): "match_kind", ("ledger_rejects", "target_id"): "kind",
              ("review_marks", "item_id"): "kind", ("price_fixes", "row_id"): "kind"}
KIND_TABLES = {"order": "tracked_orders", "expense": "expenses", "expense_refund": "expenses", "sale": "sales",
               "checkout": "checkouts"}
# Encrypted with this computer's card key.
SEALED = {("cards", "secret"), ("plaid_items", "token"), ("proxy_accounts", "site_session"), ("plaid_settings", "value")}
BATCH = 400
EVERY = 60  # seconds between swaps
INVITE_HOURS = 1
NTFY = "https://ntfy.sh/"
NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%f','now')"

state = {"running": False, "last": "", "error": ""}
_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23]


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def ensure(con):
    return db.setup_once(con, "peersync", lambda c: c.executescript(SCHEMA))


def meta(con, key, default=None):
    row = ensure(con).execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key, value):
    ensure(con).execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES (?,?)", (key, value))


def site(con):
    """This computer's own id among linked computers."""
    s = meta(con, "site")
    if not s:
        s = secrets.token_hex(8)
        set_meta(con, "site", s)
    return s


def computer_name():
    name = (socket.gethostname() or "").split(".")[0] or "Computer"
    return f"{name} ({'Mac' if platform.system() == 'Darwin' else 'PC' if os.name == 'nt' else platform.system()})"


def peers(con):
    return [dict(r) for r in ensure(con).execute("SELECT * FROM sync_peers WHERE site IS NOT NULL ORDER BY id")]


def linked(con):
    return bool(ensure(con).execute("SELECT 1 FROM sync_peers WHERE site IS NOT NULL LIMIT 1").fetchone())


# ---- tables and triggers ----

def tables(con):
    """{table: info} for every table that is kept the same on linked computers."""
    rows = con.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
    virtual = [r[0] for r in rows if (r[1] or "").upper().startswith("CREATE VIRTUAL")]
    out = {}
    for name, sql in rows:
        if (name in LOCAL_TABLES or name.startswith(("sync_", "sqlite_")) or name in virtual
                or any(name.startswith(v + "_") for v in virtual) or "WITHOUT ROWID" in (sql or "").upper()):
            continue
        cols = con.execute(f"PRAGMA table_info({_q(name)})").fetchall()
        pk = [c[1] for c in sorted(cols, key=lambda c: c[5]) if c[5]]
        alias = pk[0] if len(pk) == 1 and next(c[2] for c in cols if c[1] == pk[0]).upper() == "INTEGER" else None
        linked_cols = {c for (t, c) in LINKS if t == name} | {c for (t, c) in KIND_LINKS if t == name}
        out[name] = {"cols": [c[1] for c in cols], "pk": pk, "alias": alias,
                     # A row named by its own words (a profile's name) has the same id everywhere.
                     "keyed": bool(pk) and not alias and not (set(pk) & linked_cols)}
    return out


def _gid_sql(info, prefix="NEW."):
    if info["keyed"]:
        return "'k:'||" + "||'|'||".join(f"quote({prefix}{_q(c)})" for c in info["pk"])
    return "lower(hex(randomblob(12)))"


def _order(names):
    """Tables a row can point at come first, so a first swap brings parents before the rows that use them."""
    first = set(LINKS.values()) | set(KIND_TABLES.values())
    return sorted(names, key=lambda n: (n not in first, n))


def install(con):
    """Triggers on every kept table (once each, also for tables made since), and every row already there written
    down to be sent. Returns how many tables were added."""
    ensure(con)
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'sync\\_%' ESCAPE '\\'")}
    added, infos = 0, tables(con)
    for name in _order(infos):
        info = infos[name]
        tag = "sync_" + hashlib.sha1(name.encode()).hexdigest()[:10]
        if tag + "_i" in have:
            continue
        t, lit = _q(name), "'" + name.replace("'", "''") + "'"
        quiet = "WHEN NOT EXISTS (SELECT 1 FROM sync_quiet)"
        log = (f"INSERT INTO sync_log (tbl, rid, gid, at) SELECT tbl, rid, gid, at FROM sync_ids "
               f"WHERE tbl={lit} AND rid=NEW.rowid;")
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {tag}_i AFTER INSERT ON {t} {quiet} BEGIN "
                        f"INSERT OR REPLACE INTO sync_ids (tbl, rid, gid, at) VALUES ({lit}, NEW.rowid, {_gid_sql(info)}, {NOW_SQL}); "
                        f"{log} END")
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {tag}_u AFTER UPDATE ON {t} {quiet} BEGIN "
                        f"INSERT OR IGNORE INTO sync_ids (tbl, rid, gid, at) VALUES ({lit}, NEW.rowid, {_gid_sql(info)}, {NOW_SQL}); "
                        f"UPDATE sync_ids SET at={NOW_SQL} WHERE tbl={lit} AND rid=NEW.rowid; {log} END")
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {tag}_d AFTER DELETE ON {t} {quiet} BEGIN "
                        f"INSERT INTO sync_log (tbl, rid, gid, at) SELECT tbl, rid, gid, {NOW_SQL} FROM sync_ids "
                        f"WHERE tbl={lit} AND rid=OLD.rowid; DELETE FROM sync_ids WHERE tbl={lit} AND rid=OLD.rowid; END")
            con.execute(f"INSERT OR IGNORE INTO sync_ids (tbl, rid, gid, at) SELECT {lit}, rowid, {_gid_sql(info, '')}, "
                        f"{NOW_SQL} FROM {t}")
            con.execute(f"INSERT INTO sync_log (tbl, rid, gid, at) SELECT tbl, rid, gid, at FROM sync_ids WHERE tbl={lit} ORDER BY rid")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        added += 1
    return added


def uninstall(con):
    """No computers linked any more: stop writing changes down."""
    ensure(con)
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'sync\\_%' ESCAPE '\\'").fetchall():
        con.execute(f"DROP TRIGGER IF EXISTS {_q(name)}")
    for t in ("sync_ids", "sync_alias", "sync_log", "sync_wait"):
        con.execute(f"DELETE FROM {t}")


# ---- sending ----

def _gid_of(con, tbl, rid):
    row = con.execute("SELECT gid FROM sync_ids WHERE tbl=? AND rid=?", (tbl, rid)).fetchone()
    return row[0] if row else None


def _target(name, col, row):
    if (name, col) in LINKS:
        return LINKS[(name, col)]
    if (name, col) in KIND_LINKS:
        return KIND_TABLES.get(row.get(KIND_LINKS[(name, col)]))
    return None


def encode_row(con, name, info, row):
    out = {}
    for col in info["cols"]:
        v = row[col]
        if col in LOCAL_COLUMNS.get(name, ()) or (col == info["alias"] and (name, col) not in LINKS):
            continue
        target = _target(name, col, row) if v is not None else None
        if target:
            g = _gid_of(con, target, v)
            v = {"$g": g} if g else None
        elif (name, col) in SEALED and isinstance(v, str) and v:
            try:
                v = {"$s": wallet._fernet().decrypt(v.encode()).decode()}
            except Exception:
                pass  # not encrypted (a plain setting)
        elif isinstance(v, bytes):
            v = {"$b": base64.b64encode(v).decode()}
        out[col] = v
    return out


def outgoing(con, after, limit=BATCH):
    """(changes, last seq, more) for what changed here after seq `after`."""
    infos = tables(con)
    logs = con.execute("SELECT seq, tbl, rid, gid, at FROM sync_log WHERE seq > ? ORDER BY seq LIMIT ?",
                       (after, limit)).fetchall()
    if not logs:
        return [], after, False
    latest = {}
    for seq, tbl, rid, gid, at in logs:
        latest.pop((tbl, gid), None)
        latest[(tbl, gid)] = (rid, at)
    changes = []
    for (tbl, gid), (rid, at) in latest.items():
        info = infos.get(tbl)
        if info is None:
            continue
        cur = con.execute("SELECT gid, at FROM sync_ids WHERE tbl=? AND rid=?", (tbl, rid)).fetchone()
        row = con.execute(f"SELECT * FROM {_q(tbl)} WHERE rowid=?", (rid,)).fetchone() if cur and cur[0] == gid else None
        if row is None:
            changes.append({"t": tbl, "g": gid, "at": at, "del": True})
        else:
            changes.append({"t": tbl, "g": gid, "at": cur[1], "row": encode_row(con, tbl, info, dict(row))})
    last = logs[-1][0]
    more = con.execute("SELECT 1 FROM sync_log WHERE seq > ? LIMIT 1", (last,)).fetchone() is not None
    return changes, last, more


# ---- applying ----

class Waiting(Exception):
    """A row points at one that hasn't arrived yet."""


def resolve(con, tbl, gid):
    """(rowid, at) here for a gid, following merged twins."""
    for _ in range(3):
        row = con.execute("SELECT rid, at FROM sync_ids WHERE tbl=? AND gid=?", (tbl, gid)).fetchone()
        if row:
            return row[0], row[1]
        alias = con.execute("SELECT to_gid FROM sync_alias WHERE tbl=? AND gid=?", (tbl, gid)).fetchone()
        if not alias:
            return None, None
        gid = alias[0]
    return None, None


def decode_row(con, name, info, row):
    out = {}
    for col, v in row.items():
        if col not in info["cols"] or col in LOCAL_COLUMNS.get(name, ()):
            continue
        if isinstance(v, dict) and "$g" in v:
            target = _target(name, col, row)
            rid = resolve(con, target, v["$g"])[0] if target else None
            if rid is None:
                raise Waiting(f"{name}.{col}")
            v = rid
        elif isinstance(v, dict) and "$s" in v:
            plain, v = v["$s"], wallet._fernet().encrypt(v["$s"].encode()).decode()
            if name == "cards" and col == "secret":  # a card's fingerprint is made with this computer's key
                try:
                    out["fingerprint"] = wallet.fingerprint(json.loads(plain)["number"])
                except (ValueError, KeyError, TypeError):
                    pass
        elif isinstance(v, dict) and "$b" in v:
            v = base64.b64decode(v["$b"])
        if not (name == "cards" and col == "fingerprint" and "fingerprint" in out):
            out[col] = v
    return out


def _twin(con, name, values):
    """The row here that the same thing made on the other computer collides with (same unique values)."""
    for idx in con.execute(f"PRAGMA index_list({_q(name)})").fetchall():
        if not idx[2]:  # not unique
            continue
        cols = [c[2] for c in con.execute(f"PRAGMA index_info({_q(idx[1])})")]
        if not cols or any(values.get(c) is None for c in cols):
            continue
        row = con.execute(f"SELECT rowid FROM {_q(name)} WHERE " + " AND ".join(f"{_q(c)}=?" for c in cols),
                          [values[c] for c in cols]).fetchone()
        if row:
            return row[0]
    return None


def _newer(incoming, local, ties=False):
    """The change from the other computer is newer; on the very same moment one computer always wins (ties)."""
    return local is None or incoming > local or (ties and incoming == local)


def apply_one(con, change, infos, ties=False):
    """Apply one change from another computer. Returns True when it changed something here."""
    name, gid, at = change["t"], change["g"], change["at"]
    info = infos.get(name)
    if info is None:
        return False  # a table this version doesn't have yet
    rid, local_at = resolve(con, name, gid)
    t = _q(name)
    if change.get("del"):
        if rid is None or not _newer(at, local_at, ties):
            return False
        con.execute(f"DELETE FROM {t} WHERE rowid=?", (rid,))
        con.execute("DELETE FROM sync_ids WHERE tbl=? AND rid=?", (name, rid))
        return True
    values = decode_row(con, name, info, change["row"])
    if rid is not None:
        if not _newer(at, local_at, ties):
            return False
        if values:
            con.execute(f"UPDATE {t} SET " + ", ".join(f"{_q(c)}=?" for c in values) + " WHERE rowid=?",
                        [*values.values(), rid])
        con.execute("UPDATE sync_ids SET at=? WHERE tbl=? AND rid=?", (at, name, rid))
        return True
    twin = _twin(con, name, values)
    if twin is None:
        try:
            cur = con.execute(f"INSERT INTO {t} (" + ", ".join(_q(c) for c in values) + ") VALUES ("
                              + ",".join("?" * len(values)) + ")", list(values.values()))
        except Exception as e:  # sqlite3.IntegrityError the unique check above didn't foresee
            logger.warning("couldn't add a %s row from the other computer: %s", name, e)
            return False
        con.execute("INSERT OR REPLACE INTO sync_ids (tbl, rid, gid, at) VALUES (?,?,?,?)", (name, cur.lastrowid, gid, at))
        return True
    mine = _gid_of(con, name, twin)
    if mine is None:
        con.execute("INSERT OR REPLACE INTO sync_ids (tbl, rid, gid, at) VALUES (?,?,?,?)", (name, twin, gid, ""))
    else:
        con.execute("INSERT OR REPLACE INTO sync_alias (tbl, gid, to_gid) VALUES (?,?,?)", (name, gid, mine))
    local_at = con.execute("SELECT at FROM sync_ids WHERE tbl=? AND rid=?", (name, twin)).fetchone()[0]
    if _newer(at, local_at, ties) and values:
        try:
            con.execute(f"UPDATE {t} SET " + ", ".join(f"{_q(c)}=?" for c in values) + " WHERE rowid=?",
                        [*values.values(), twin])
            con.execute("UPDATE sync_ids SET at=? WHERE tbl=? AND rid=?", (at, name, twin))
        except Exception as e:
            logger.warning("couldn't update a %s row from the other computer: %s", name, e)
    return True


def apply(con, peer_id, changes):
    """Apply a batch from another computer without writing it down to be sent back. Rows that point at one not
    here yet wait and are tried again with the next batch. Returns how many changed something."""
    ensure(con)
    infos = tables(con)
    theirs = con.execute("SELECT site FROM sync_peers WHERE id=?", (peer_id,)).fetchone()
    ties = bool(theirs and theirs[0] > site(con))
    done = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("INSERT INTO sync_quiet VALUES (1)")
        waiting = [(r[0], json.loads(r[1])) for r in con.execute("SELECT id, change FROM sync_wait WHERE peer=?", (peer_id,))]
        pending = [(None, c) for c in changes] + waiting
        while pending:
            left = []
            for wid, change in pending:
                try:
                    done += apply_one(con, change, infos, ties)
                    if wid:
                        con.execute("DELETE FROM sync_wait WHERE id=?", (wid,))
                except Waiting:
                    left.append((wid, change))
            if len(left) == len(pending):
                break
            pending = left
        old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for wid, change in pending:
            if wid is None:
                con.execute("INSERT INTO sync_wait (peer, change, since) VALUES (?,?,?)", (peer_id, json.dumps(change), now()))
        # Rows that waited a week for a row that never came: keep them without that link.
        for wid, raw in con.execute("SELECT id, change FROM sync_wait WHERE peer=? AND since < ?", (peer_id, old)).fetchall():
            change = json.loads(raw)
            change["row"] = {k: (None if isinstance(v, dict) and "$g" in v else v) for k, v in change.get("row", {}).items()}
            done += apply_one(con, change, infos, ties)
            con.execute("DELETE FROM sync_wait WHERE id=?", (wid,))
        con.execute("DELETE FROM sync_quiet")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return done


# ---- sealing and linking ----

def _box(secret):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("bottrack-sync|" + secret).encode()).digest()))


def seal(secret, data):
    return _box(secret).encrypt(json.dumps(data).encode())


def unseal(secret, token, ttl=900):
    return json.loads(_box(secret).decrypt(token, ttl=ttl))


def _invite_tag(secret):
    return hashlib.sha256(("invite|" + secret).encode()).hexdigest()[:24]


def my_url():
    from . import tunnel
    return tunnel.state.get("url") or ""


def sync_topic():
    topic = os.environ.get("SYNC_TOPIC")
    if not topic:
        from .config import save_env
        topic = "bottrack-sync-" + secrets.token_hex(16)
        save_env({"SYNC_TOPIC": topic})
    return topic


def new_invite(con):
    """A code to type on the other computer. Good for an hour, once."""
    secret = secrets.token_urlsafe(24)
    until = (datetime.now(timezone.utc) + timedelta(hours=INVITE_HOURS)).isoformat()
    ensure(con).execute("DELETE FROM sync_peers WHERE site IS NULL")
    con.execute("INSERT INTO sync_peers (secret, invite, invite_until) VALUES (?,?,?)", (secret, _invite_tag(secret), until))
    code = {"u": my_url(), "s": secret, "t": sync_topic(), "i": site(con), "n": computer_name()}
    return "BT1-" + base64.urlsafe_b64encode(json.dumps(code, separators=(",", ":")).encode()).decode().rstrip("=")


def read_code(code):
    code = "".join((code or "").split())
    if not code.startswith("BT1-"):
        raise ValueError("That isn't a BotTrack link code. It starts with BT1-.")
    body = code[4:]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        raise ValueError("That code is cut short. Copy the whole thing again.")
    if not all(data.get(k) for k in ("s", "i")):
        raise ValueError("That code is cut short. Copy the whole thing again.")
    if not data.get("u"):
        raise ValueError("The other computer's link wasn't on yet when it made that code. Make a new one there.")
    return data


def _post(url, path, secret, me, body, timeout=60):
    req = urllib.request.Request(url.rstrip("/") + path, data=seal(secret, body), method="POST",
                                 headers={"Content-Type": "application/octet-stream", "X-BotTrack-Site": me,
                                          "User-Agent": "BotTrack-sync"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return unseal(secret, r.read())


def join(con, code, post=None):
    """This computer types the other's code: link them and say hello."""
    data = read_code(code)
    me = site(con)
    if data["i"] == me:
        raise ValueError("That code was made on this computer. Type it on the other one.")
    hello = {"site": me, "name": computer_name(), "url": my_url(), "topic": sync_topic()}
    post = post or _post
    try:
        answer = post(data["u"], "/sync/peer/hello", data["s"], "invite:" + _invite_tag(data["s"]), hello)
    except urllib.error.HTTPError as e:
        raise ValueError("The other computer said no. The code may be over an hour old or used already: make a new one."
                         if e.code in (403, 404, 410) else f"The other computer answered {e.code}.")
    except Exception as e:
        raise ValueError(f"Couldn't reach the other computer ({e}). Is BotTrack open there?")
    ensure(con).execute("DELETE FROM sync_peers WHERE site=?", (data["i"],))
    con.execute("INSERT INTO sync_peers (site, name, url, topic, secret, paired_at) VALUES (?,?,?,?,?,?)",
                (data["i"], answer.get("name") or data.get("n") or "Other computer", data["u"], data.get("t"), data["s"], now()))
    install(con)
    return answer.get("name") or data.get("n")


def hello(con, invite_tag, token):
    """The other computer typed our code."""
    row = ensure(con).execute("SELECT * FROM sync_peers WHERE invite=? AND site IS NULL", (invite_tag,)).fetchone()
    if row is None or (row["invite_until"] or "") < datetime.now(timezone.utc).isoformat():
        abort(410)
    try:
        data = unseal(row["secret"], token)
    except Exception:
        abort(403)
    if not data.get("site") or data["site"] == site(con):
        abort(403)
    con.execute("DELETE FROM sync_peers WHERE site=?", (data["site"],))
    con.execute("UPDATE sync_peers SET site=?, name=?, url=?, topic=?, invite=NULL, invite_until=NULL, paired_at=? WHERE id=?",
                (data["site"], (data.get("name") or "Other computer")[:80], data.get("url") or "", data.get("topic"), now(), row["id"]))
    install(con)
    return row["secret"], {"name": computer_name(), "site": site(con)}


def unlink(con, peer_id):
    ensure(con).execute("DELETE FROM sync_peers WHERE id=?", (peer_id,))
    con.execute("DELETE FROM sync_wait WHERE peer=?", (peer_id,))
    if not linked(con):
        uninstall(con)


# ---- swapping ----

def receive(con, peer, data):
    """The other computer's side of a swap, arriving here: apply its changes, answer with ours."""
    if data.get("url"):
        con.execute("UPDATE sync_peers SET url=?, topic=COALESCE(?, topic) WHERE id=?", (data["url"], data.get("topic"), peer["id"]))
    got = apply(con, peer["id"], data.get("changes") or [])
    if data.get("changes") is not None:
        con.execute("UPDATE sync_peers SET got_upto=MAX(got_upto, ?), got=got+?, last_ok=?, error=NULL WHERE id=?",
                    (data.get("upto") or 0, got, now(), peer["id"]))
    since = int(data.get("since") or 0)  # what the other computer already has from here
    con.execute("UPDATE sync_peers SET sent_upto=MAX(sent_upto, ?) WHERE id=?", (since, peer["id"]))
    changes, upto, more = outgoing(con, since)
    con.execute("UPDATE sync_peers SET sent=sent+? WHERE id=?", (len(changes), peer["id"]))
    return {"changes": changes, "upto": upto, "more": more, "url": my_url(), "name": computer_name()}


def _find_new_url(topic, get=urllib.request.urlopen):
    """The other computer's newest link address, from its private ntfy topic."""
    if not topic:
        return ""
    try:
        with get(NTFY + topic + "/json?poll=1&since=12h", timeout=15) as r:
            lines = [json.loads(x) for x in r.read().decode().splitlines() if x.strip()]
    except Exception as e:
        logger.info("couldn't check the other computer's link: %s", e)
        return ""
    found = [m.get("message", "") for m in lines if m.get("event") == "message"]
    return found[-1].strip() if found else ""


def swap(con, peer, post=None, rounds=50):
    """Send what's new here and take what's new there, until both are caught up."""
    post = post or _post
    me = site(con)
    total_sent = total_got = 0
    for _ in range(rounds):
        peer = dict(con.execute("SELECT * FROM sync_peers WHERE id=?", (peer["id"],)).fetchone())
        changes, upto, more_here = outgoing(con, peer["sent_upto"])
        body = {"changes": changes, "upto": upto, "since": peer["got_upto"], "url": my_url(), "topic": sync_topic()}
        try:
            answer = post(peer["url"], "/sync/peer/swap", peer["secret"], me, body)
        except Exception as e:
            new = _find_new_url(peer.get("topic"))
            if new and new != peer["url"]:
                con.execute("UPDATE sync_peers SET url=? WHERE id=?", (new, peer["id"]))
                logger.info("the other computer has a new link: %s", new)
                continue
            con.execute("UPDATE sync_peers SET last_try=?, error=? WHERE id=?", (now(), _say(e), peer["id"]))
            raise
        got = apply(con, peer["id"], answer.get("changes") or [])
        con.execute("UPDATE sync_peers SET sent_upto=?, got_upto=MAX(got_upto, ?), sent=sent+?, got=got+?, "
                    "name=COALESCE(?, name), last_ok=?, last_try=?, error=NULL WHERE id=?",
                    (upto, answer.get("upto") or 0, len(changes), got, answer.get("name"), now(), now(), peer["id"]))
        if answer.get("url") and answer["url"] != peer["url"]:
            con.execute("UPDATE sync_peers SET url=? WHERE id=?", (answer["url"], peer["id"]))
        total_sent, total_got = total_sent + len(changes), total_got + got
        if not more_here and not answer.get("more"):
            break
    return total_sent, total_got


def _say(e):
    if isinstance(e, urllib.error.HTTPError):
        return {403: "The other computer doesn't know this one any more. Link them again.",
                404: "The other computer's link is off or BotTrack is closed there."}.get(e.code, f"The other computer answered {e.code}.")
    return f"Couldn't reach the other computer: {e}"


def run_once(db_path=None):
    if not _lock.acquire(blocking=False):
        return
    try:
        con = db.connect(db_path)
        if not linked(con):
            return
        install(con)  # tables made since the last time
        state.update(running=True)
        for peer in peers(con):
            if not peer["url"] and not peer.get("topic"):
                continue
            try:
                if not peer["url"]:
                    url = _find_new_url(peer["topic"])
                    if not url:
                        continue
                    con.execute("UPDATE sync_peers SET url=? WHERE id=?", (url, peer["id"]))
                    peer["url"] = url
                sent, got = swap(con, peer)
                if sent or got:
                    logger.info("swapped with %s: sent %d, got %d", peer["name"], sent, got)
                state.update(last=now(), error="")
            except Exception as e:
                logger.info("couldn't swap with %s: %s", peer["name"], e)
                state.update(error=_say(e))
        # Changes every linked computer has: no need to keep them.
        lowest = con.execute("SELECT MIN(sent_upto) FROM sync_peers WHERE site IS NOT NULL").fetchone()[0]
        if lowest:
            con.execute("DELETE FROM sync_log WHERE seq <= ?", (lowest,))
    finally:
        state.update(running=False)
        _lock.release()


def start(app):
    """Swap with linked computers every minute while BotTrack is open."""
    path = app.config.get("DB_PATH")

    def loop():
        time.sleep(20)
        while True:
            try:
                run_once(path)
            except Exception:
                logger.exception("computer sync failed")
            time.sleep(EVERY)
    threading.Thread(target=loop, daemon=True, name="peersync").start()


def link_wanted(db_path=None):
    """The link has to be on for the other computer to reach this one."""
    try:
        return linked(db.connect(db_path))
    except Exception:
        return False


# ---- pages ----

def _con():
    return db.connect(current_app.config.get("DB_PATH"))


def _peer_for(con, sender):
    row = ensure(con).execute("SELECT * FROM sync_peers WHERE site=?", (sender,)).fetchone()
    if row is None:
        abort(403)
    return dict(row)


@bp.post("/sync/peer/hello")
def peer_hello():
    sender = request.headers.get("X-BotTrack-Site", "")
    if not sender.startswith("invite:"):
        abort(403)
    con = _con()
    secret, answer = hello(con, sender[7:], request.get_data())
    threading.Thread(target=run_once, args=(current_app.config.get("DB_PATH"),), daemon=True).start()
    return current_app.response_class(seal(secret, answer), mimetype="application/octet-stream")


@bp.post("/sync/peer/swap")
def peer_swap():
    con = _con()
    peer = _peer_for(con, request.headers.get("X-BotTrack-Site", ""))
    try:
        data = unseal(peer["secret"], request.get_data())
    except Exception:
        abort(403)
    answer = receive(con, peer, data)
    return current_app.response_class(seal(peer["secret"], answer), mimetype="application/octet-stream")


def _local():
    from .mobile import from_this_mac
    if not from_this_mac():
        abort(404)


def _link_on():
    """Turn on the link (the phone link's tunnel) so the other computer can reach this one."""
    from . import tunnel
    from .config import save_env
    save_env({"SYNC_LINK": "1"})
    if current_app.config.get("PHONE_LINK") and tunnel.state["status"] in ("off", "error"):
        tunnel.ensure(current_app.config.get("PORT") or 8765)


@bp.get("/computers")
def page():
    _local()
    from flask import render_template
    con = _con()
    return render_template("computers.html", peers=peers(con), me=computer_name(), link=my_url(), state=state,
                           invite=request.args.get("code", ""), backlog=con.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0]
                           if linked(con) else 0)


@bp.post("/computers/invite")
def make_invite():
    _local()
    _link_on()
    for _ in range(40):  # the link takes a few seconds the first time
        if my_url():
            break
        time.sleep(0.5)
    if not my_url():
        flash("The link is still starting. Try again in a minute.")
        return redirect(url_for("peersync.page"))
    return redirect(url_for("peersync.page", code=new_invite(_con())))


@bp.post("/computers/join")
def join_page():
    _local()
    _link_on()
    try:
        name = join(_con(), request.form.get("code", ""))
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("peersync.page"))
    threading.Thread(target=run_once, args=(current_app.config.get("DB_PATH"),), daemon=True).start()
    flash(f"Linked with {name}. The first sync can take a few minutes; after that changes show up within a minute.")
    return redirect(url_for("peersync.page"))


@bp.post("/computers/now")
def sync_now():
    _local()
    threading.Thread(target=run_once, args=(current_app.config.get("DB_PATH"),), daemon=True).start()
    flash("Syncing with your other computers.")
    return redirect(url_for("peersync.page"))


@bp.post("/computers/<int:peer_id>/unlink")
def unlink_page(peer_id):
    _local()
    unlink(_con(), peer_id)
    flash("Unlinked. Both computers keep everything they have.")
    return redirect(url_for("peersync.page"))


@bp.get("/computers/status")
def status():
    _local()
    con = _con()
    return jsonify(running=state["running"], peers=[{k: p[k] for k in ("id", "name", "last_ok", "error", "sent", "got")}
                                                    for p in peers(con)],
                   backlog=con.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] if linked(con) else 0)
