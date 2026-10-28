"""Your payment cards, stored encrypted on this Mac.

Card numbers and CVVs are encrypted with a key kept in the macOS Keychain (on Windows, a key file locked to your
Windows account with DPAPI; elsewhere, a key file with owner-only permissions), so the database alone never
reveals them. The pages show only
the last 4 digits unless you press the eye; numbers are never written to logs. A card reaches a
bot export only through the profile you linked it to yourself.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("jigstream.wallet")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    group_name TEXT, provider TEXT, name TEXT, brand TEXT, last4 TEXT, exp TEXT,
    card_type TEXT, spend_limit REAL, status TEXT DEFAULT 'active',
    secret TEXT, fingerprint TEXT UNIQUE, created_at TEXT
);
CREATE TABLE IF NOT EXISTS card_groups (name TEXT PRIMARY KEY);
"""
PROVIDERS = ["Other", "Amex", "Capital One", "Chase", "Citi", "Discover", "Privacy", "Revolut", "Wise", "Bank of America"]
CARD_TYPES = ["Standard", "Virtual", "Debit", "Prepaid", "Business"]
KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "BotTrack", "card-key"
KEYCHAIN_NOT_FOUND = 44  # what `security` exits with when there is no such item (errSecItemNotFound)

_key = None


def _keychain(*args):
    return subprocess.run(["security", *args], capture_output=True, text=True)


def _dpapi(data, protect):
    """Windows DPAPI: data only this Windows user account can unlock (CryptProtectData / CryptUnprotectData)."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not crypt(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # 0x1: no prompt
        raise OSError("DPAPI couldn't " + ("lock" if protect else "unlock") + " the card key")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def key():
    """The Fernet key: from the Keychain on a Mac, else from a 0600 key file in the data folder."""
    global _key
    if _key:
        return _key
    from cryptography.fernet import Fernet
    key_file = Path(os.environ.get("BOTTRACK_CARD_KEY_FILE", ".card-key"))
    if sys.platform == "darwin" and "BOTTRACK_CARD_KEY_FILE" not in os.environ:
        found = _keychain("find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w")
        if found.returncode == 0 and found.stdout.strip():
            _key = found.stdout.strip().encode()
            return _key
        if found.returncode != KEYCHAIN_NOT_FOUND:
            # Locked, or it didn't answer: making a new key here would replace the one every saved card needs.
            raise RuntimeError(f"the Keychain didn't hand over the card key (error {found.returncode}); try again")
        # Not in the Keychain yet: a key file kept from an earlier run (when the Keychain couldn't be used) moves in.
        new = key_file.read_bytes().strip().removeprefix(b"plain:") if key_file.exists() else b""
        try:
            Fernet(new)
        except ValueError:
            new = Fernet.generate_key()
        if _keychain("add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
                     "-w", new.decode()).returncode == 0:
            _key = new
            return _key
        logger.warning("couldn't use the Keychain for the card key; using a key file instead")
    windows = sys.platform == "win32" and "BOTTRACK_CARD_KEY_FILE" not in os.environ
    if key_file.exists():
        raw = key_file.read_bytes()
        plain = raw.strip().removeprefix(b"plain:")
        locked = windows and not raw.startswith(b"plain:") and len(raw.strip()) != 44  # a Fernet key is 44 characters
        _key = _dpapi(raw, False) if locked else plain
    else:
        _key = Fernet.generate_key()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            key_file.write_bytes(_dpapi(_key, True) if windows else _key)
        except OSError:
            logger.warning("couldn't lock the card key to this Windows account; it's kept in a plain key file")
            key_file.write_bytes(b"plain:" + _key)
        os.chmod(key_file, 0o600)
    return _key


def set_key(new):
    """Use this card key from now on (a restored backup's), in the Keychain on a Mac, else the key file."""
    global _key
    new = new.strip() if isinstance(new, bytes) else new.strip().encode()
    from cryptography.fernet import Fernet
    Fernet(new)  # a real key, or ValueError
    key_file = Path(os.environ.get("BOTTRACK_CARD_KEY_FILE", ".card-key"))
    if sys.platform == "darwin" and "BOTTRACK_CARD_KEY_FILE" not in os.environ:
        if _keychain("add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
                     "-w", new.decode()).returncode == 0:
            _key = new
            return
    windows = sys.platform == "win32" and "BOTTRACK_CARD_KEY_FILE" not in os.environ
    key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        key_file.write_bytes(_dpapi(new, True) if windows else new)
    except OSError:
        key_file.write_bytes(b"plain:" + new)
    os.chmod(key_file, 0o600)
    _key = new


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(key())


def fingerprint(number):
    """Recognizes a card already added without storing its number."""
    return hmac.new(base64.urlsafe_b64decode(key()), number.encode(), hashlib.sha256).hexdigest()


def luhn_ok(number):
    digits = [int(d) for d in number][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return len(number) >= 12 and total % 10 == 0


def brand(number):
    from .profiles import card_brand
    return card_brand(number) or "Card"


def parse_lines(text):
    """'NAME:NUMBER:MM/YYYY:CVV' per line -> (cards, problems). The name is optional."""
    cards, problems = [], []
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) == 3:
            parts = [""] + parts
        if len(parts) != 4:
            problems.append(f"line {n}: expected NAME:NUMBER:MM/YYYY:CVV")
            continue
        name, number, exp, cvv = parts
        number = re.sub(r"[\s-]", "", number)
        m = re.fullmatch(r"(\d{1,2})\s*/\s*(\d{2}|\d{4})", exp)
        if not number.isdigit() or not luhn_ok(number):
            problems.append(f"line {n}: that card number doesn't look right")
        elif not m or not 1 <= int(m.group(1)) <= 12:
            problems.append(f"line {n}: expiry should be MM/YYYY")
        elif not re.fullmatch(r"\d{3,4}", cvv):
            problems.append(f"line {n}: CVV should be 3 or 4 digits")
        else:
            year = m.group(2) if len(m.group(2)) == 4 else "20" + m.group(2)
            cards.append({"name": name, "number": number, "month": m.group(1).zfill(2), "year": year, "cvv": cvv})
    return cards, problems


def add_cards(con, cards, provider="Other", card_type="Standard", spend_limit=None, group=None):
    """Store parsed cards encrypted. Returns (added, already there)."""
    added = dup = 0
    f = _fernet()
    if group:
        con.execute("INSERT OR IGNORE INTO card_groups VALUES (?)", (group,))
    for c in cards:
        secret = f.encrypt(json.dumps({"number": c["number"], "cvv": c["cvv"]}).encode()).decode()
        try:
            con.execute(
                "INSERT INTO cards (group_name, provider, name, brand, last4, exp, card_type, spend_limit, status,"
                " secret, fingerprint, created_at) VALUES (?,?,?,?,?,?,?,?, 'active', ?,?,?)",
                (group or None, provider, c["name"] or None, brand(c["number"]), c["number"][-4:],
                 f"{c['month']}/{c['year']}", card_type, spend_limit, secret, fingerprint(c["number"]),
                 datetime.now(timezone.utc).isoformat()))
            added += 1
        except Exception as e:  # sqlite3.IntegrityError: same card already stored
            if "UNIQUE" not in str(e):
                raise
            dup += 1
    con.commit()
    logger.info("added %d card(s) (%d already there)", added, dup)
    return added, dup


def add_last4(con, provider, last4, name=None, exp=None, card_type="Virtual"):
    """A card known only by its last 4 (found on the bank's page without its full number). True when it's new."""
    key = f"last4:{provider}:{last4}"  # not a secret: stands in for the number's fingerprint so it's added once
    if con.execute("SELECT 1 FROM cards WHERE fingerprint=? OR (provider=? AND last4=?)", (key, provider, last4)).fetchone():
        return False
    con.execute("INSERT INTO cards (provider, name, last4, exp, card_type, status, fingerprint, created_at)"
                " VALUES (?,?,?,?,?, 'active', ?, ?)", (provider, name or None, last4, exp or None, card_type, key,
                                                        datetime.now(timezone.utc).isoformat()))
    con.commit()
    return True


def reveal(con, card_id):
    """Full card details for one card: for the eye button and for exports."""
    row = con.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if not row or not row["secret"]:  # a card known only by its last 4
        return None
    secret = json.loads(_fernet().decrypt(row["secret"].encode()))
    month, year = (row["exp"] or "/").split("/")
    return {"number": secret["number"], "cvv": secret["cvv"], "month": month, "year": year,
            "brand": row["brand"], "name": row["name"]}


def cards(con, group=None):
    q = ("SELECT c.*, p.name AS profile FROM cards c LEFT JOIN profiles p ON p.card_id = c.id"
         + (" WHERE COALESCE(c.group_name, '') = ?" if group is not None else "") + " ORDER BY c.id DESC")
    return [dict(r) for r in con.execute(q, (group,) if group is not None else ())]


def groups(con):
    named = {r[0] for r in con.execute("SELECT name FROM card_groups")}
    counts = {r[0]: r[1] for r in con.execute(
        "SELECT COALESCE(group_name, ''), COUNT(*) FROM cards GROUP BY COALESCE(group_name, '')")}
    return [(g, counts.get(g, 0)) for g in sorted(named | {g for g in counts if g}, key=str.lower)], counts.get("", 0)


def link(con, card_id, profile):
    """Attach one card to one profile (a profile has at most one card; a card at most one profile)."""
    con.execute("UPDATE profiles SET card_id=NULL WHERE card_id=?", (card_id,))
    if profile:
        con.execute("UPDATE profiles SET card_id=? WHERE name=?", (card_id, profile))
    con.commit()


def delete(con, card_ids):
    con.executemany("UPDATE profiles SET card_id=NULL WHERE card_id=?", [(i,) for i in card_ids])
    con.executemany("DELETE FROM cards WHERE id=?", [(i,) for i in card_ids])
    con.commit()


def set_status(con, card_ids, status):
    con.executemany("UPDATE cards SET status=? WHERE id=?", [(status, i) for i in card_ids])
    con.commit()
