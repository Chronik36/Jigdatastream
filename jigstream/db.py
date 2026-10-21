import os
import sqlite3

from . import orderstore, tracking

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
CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY,
    bot TEXT, first_name TEXT, last_name TEXT, phone TEXT, email TEXT,
    ship_address1 TEXT, ship_address2 TEXT, ship_city TEXT, ship_state TEXT,
    ship_zip TEXT, ship_country TEXT,
    bill_address1 TEXT, bill_address2 TEXT, bill_city TEXT, bill_state TEXT,
    bill_zip TEXT, bill_country TEXT,
    card_last4 TEXT, raw TEXT
);
"""


def connect(path=None):
    path = path or DB_PATH
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.executescript(orderstore.SCHEMA)
    con.executescript(tracking.SCHEMA)
    return con
