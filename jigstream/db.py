import os
import sqlite3

from . import discord, emailbook, orderstore, parsers, wallet

DB_PATH = os.environ.get("JIGSTREAM_DB", os.path.join("data", "jigstream.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE,
    bot TEXT, status TEXT, reason TEXT,
    site TEXT, product TEXT, size TEXT, price TEXT,
    profile TEXT, order_number TEXT, email TEXT,
    ts TEXT, raw TEXT
);
-- What you taught on Things to train (webtrain.py): which bot a webhook sender is, and what a kind of message is.
CREATE TABLE IF NOT EXISTS webhook_rules (kind TEXT, key TEXT, value TEXT, PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY,
    bot TEXT, first_name TEXT, last_name TEXT, phone TEXT, email TEXT,
    ship_address1 TEXT, ship_address2 TEXT, ship_city TEXT, ship_state TEXT,
    ship_zip TEXT, ship_country TEXT,
    bill_address1 TEXT, bill_address2 TEXT, bill_city TEXT, bill_state TEXT,
    bill_zip TEXT, bill_country TEXT,
    card_last4 TEXT, raw TEXT,
    card_brand TEXT, card_exp TEXT, fmt TEXT,
    group_name TEXT, card_holder TEXT
);
-- Training page tasks (tasks.py): examples, and adapters trained on them (finetune.py).
CREATE TABLE IF NOT EXISTS ai_tasks (
    id INTEGER PRIMARY KEY, name TEXT, instructions TEXT, answer TEXT DEFAULT 'text',
    adapter_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_task_examples (
    id INTEGER PRIMARY KEY, task_id INTEGER, input TEXT, output TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_adapters (
    id INTEGER PRIMARY KEY, task_key TEXT, base_model TEXT, path TEXT, examples INTEGER, iters INTEGER,
    loss REAL, created_at TEXT
);
CREATE INDEX IF NOT EXISTS checkouts_order ON checkouts (order_number);
CREATE INDEX IF NOT EXISTS checkouts_profile ON checkouts (profile, status);
CREATE INDEX IF NOT EXISTS checkouts_ts ON checkouts (ts);
-- Good / cancelled set by hand on the Drops page; wins over what the emails and bank say.
CREATE TABLE IF NOT EXISTS checkout_verdicts (checkout_id INTEGER PRIMARY KEY, verdict TEXT, set_at TEXT);
-- A bot's follow-up on an earlier checkout (Valor posts one when the order stuck or got cancelled).
CREATE TABLE IF NOT EXISTS checkout_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT UNIQUE, bot TEXT, verdict TEXT,
    site TEXT, profile TEXT, order_number TEXT, email TEXT, ts TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS profile_groups (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS saved_addresses (
    nickname TEXT PRIMARY KEY,
    address1 TEXT, address2 TEXT, city TEXT, state TEXT, zip TEXT, country TEXT
);
"""

# Columns added after the first release; created on older databases at startup.
MIGRATIONS = [("profiles", "card_brand", "TEXT"), ("profiles", "card_exp", "TEXT"), ("profiles", "fmt", "TEXT"),
              ("profiles", "group_name", "TEXT"), ("profiles", "card_holder", "TEXT"),
              ("profiles", "card_id", "INTEGER"), ("profiles", "site", "TEXT"),
              # The name the bot knows the profile by. `name` is BotTrack's own, unique key: the same as the bot's
              # name, or tagged with its retail site ("Profile 1 (Target)") when two sites share a name.
              ("profiles", "bot_name", "TEXT"),
              ("checkouts", "labeled", "INTEGER")]


_ready = set()  # (database file, part) pairs already set up by this process


def setup_once(con, part, setup):
    """Run setup(con) the first time this process opens this database file, so pages don't re-run
    CREATE TABLE scripts and migrations on every request. In-memory databases are always set up."""
    path = con.execute("PRAGMA database_list").fetchone()[2]
    key = None
    if path:
        st = os.stat(path)
        key = (path, st.st_dev, st.st_ino, part)  # a replaced file (restored backup) is set up again
    if key is None or st.st_size == 0 or key not in _ready:
        setup(con)
        if key:
            _ready.add(key)
    return con


def _setup(con):
    if con.execute("PRAGMA database_list").fetchone()[2]:
        con.execute("PRAGMA journal_mode = WAL")  # stays set in the file
    con.executescript(SCHEMA)
    for table, column, kind in MIGRATIONS:
        if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    con.executescript(orderstore.SCHEMA)
    con.executescript(discord.SCHEMA)
    con.executescript(wallet.SCHEMA)
    con.executescript(emailbook.SCHEMA)
    parsers.reclassify(con)  # Hayha declines stored before they were told apart by color
    parsers.move_updates(con)  # bot follow-ups (Valor) stored as checkouts before they were told apart


_locked_down = set()


def _owner_only(path):
    """The database holds names, addresses and order history: only your Mac account may read it."""
    if path in _locked_down:
        return
    path = str(path)
    for p in (path, path + "-wal", path + "-shm"):
        try:
            if os.path.exists(p) and os.stat(p).st_mode & 0o077:
                os.chmod(p, 0o600)
        except OSError:
            pass
    _locked_down.add(path)


def connect(path=None):
    path = path or DB_PATH
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    # Pages, the sync, carrier tracking, Plaid and the keyword finder all use the database at once, from
    # their own threads. Three settings keep them from failing with "database is locked":
    # - autocommit (isolation_level=None): each write is saved as it's made, so no thread holds a write
    #   lock while it waits on Gmail, a carrier page or the model (nothing here rolls back a transaction);
    # - WAL: reading never waits for a write, and a write never waits for a page that is reading;
    # - a 30 s busy timeout instead of 5 s, for the rare moment two writes meet.
    con = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    if path != ":memory:":
        con.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; saves a disk flush per write
        _owner_only(path)
    return setup_once(con, "core", _setup)
