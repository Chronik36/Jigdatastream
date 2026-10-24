"""Account keys: BotTrack works only with a key jacob made for the person's Gmail address.

The downloads are public, so the app checks a key before it opens. A key names one email address and is signed
with jacob's signing key, which never leaves his computer; the app carries only the matching public key
(license_pub.txt) and checks the signature itself, offline. The key's address has to be one of the Gmail inboxes
connected on that computer (checked with Gmail when the key is entered), so a key passed to a friend doesn't open
their copy.

- jacob makes keys on the Account keys page (/keys), which appears on the computer that holds the signing key.
- Keys can carry an end date. A key taken back is listed in revoked.txt (shipped with the app and read again
  from GitHub once a day), and stops working within a day.
- With no public key in license_pub.txt, keys are off and the app opens as before.
- Copies that were already in use when keys were turned on get GRACE_DAYS to enter one.

The code is public, so someone determined could remove the check from their own copy; the key stops casual
sharing of the download, not a programmer.
"""
import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

logger = logging.getLogger("jigstream.accountkeys")
bp = Blueprint("accountkeys", __name__)

HERE = Path(__file__).parent
PUBLIC_FILE = HERE / "license_pub.txt"
REVOKED_FILE = HERE / "revoked.txt"
REVOKED_URL = "https://raw.githubusercontent.com/Chronik36/Jigdatastream/main/jigstream/revoked.txt"
GRACE_DAYS = 7
PREFIX = "BT1K-"
# Pages that still open while locked: entering a key, connecting the Gmail it's for, updates and logs.
OPEN_PATHS = ("/activate", "/static/", "/setup/inbox", "/update", "/logs", "/m/manifest", "/api/changes")
SCHEMA = """
CREATE TABLE IF NOT EXISTS license_keys (
    kid TEXT PRIMARY KEY, email TEXT, note TEXT, issued_at TEXT, expires TEXT, revoked_at TEXT, key TEXT
);
"""
_revoked = {"ids": None, "at": 0.0}


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_key():
    """The public key keys are checked with, or "" when keys are off."""
    try:
        return PUBLIC_FILE.read_text().strip()
    except OSError:
        return ""


def signing_key():
    return os.environ.get("BOTTRACK_SIGNING_KEY", "").strip()


def new_signing_key():
    """(private, public), both base64. The private one stays on jacob's computer."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    raw = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64(raw), _b64(pub)


def public_of(private):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.from_private_bytes(_unb64(private))
    return _b64(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def issue(private, email, days=None, note=""):
    """A key for this email address. days=None never ends."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    now = datetime.now(timezone.utc)
    body = {"e": email.strip().lower(), "i": secrets.token_hex(6), "t": now.strftime("%Y-%m-%d")}
    if days:
        body["x"] = (now + timedelta(days=int(days))).strftime("%Y-%m-%d")
    if note:
        body["n"] = note.strip()[:60]
    payload = json.dumps(body, separators=(",", ":")).encode()
    sig = Ed25519PrivateKey.from_private_bytes(_unb64(private)).sign(payload)
    return PREFIX + _b64(payload) + "." + _b64(sig), body


class BadKey(ValueError):
    pass


def read(key, public=None):
    """The key's contents when it's genuine; BadKey otherwise."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    public = public if public is not None else public_key()
    key = "".join((key or "").split())
    if not key.startswith(PREFIX) or "." not in key:
        raise BadKey("That isn't a BotTrack key. It starts with BT1K-.")
    payload, sig = key[len(PREFIX):].split(".", 1)
    try:
        raw = _unb64(payload)
        Ed25519PublicKey.from_public_bytes(_unb64(public)).verify(_unb64(sig), raw)
        return json.loads(raw)
    except (InvalidSignature, ValueError, TypeError):
        raise BadKey("That key isn't valid. Copy the whole key again, or ask for a new one.")


def revoked_ids(fetch=urllib.request.urlopen):
    """Keys taken back: the list shipped with the app plus the one on GitHub, read again once a day."""
    if _revoked["ids"] is None or time.time() - _revoked["at"] > 86400:
        ids = set()
        try:
            ids |= {line.split("#")[0].strip() for line in REVOKED_FILE.read_text().splitlines()}
        except OSError:
            pass
        try:
            with fetch(REVOKED_URL, timeout=10) as r:
                ids |= {line.split("#")[0].strip() for line in r.read().decode().splitlines()}
        except Exception as e:
            logger.info("couldn't read the list of keys taken back: %s", e)
        _revoked.update(ids=ids - {""}, at=time.time())
    return _revoked["ids"]


def check(key, accounts, today=None, revoked=None):
    """None when this key opens BotTrack on a computer with these Gmail inboxes, else why not."""
    try:
        body = read(key)
    except BadKey as e:
        return str(e)
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if body.get("x") and body["x"] < today:
        return f"This key ended on {body['x']}. Ask for a new one."
    if body.get("i") in (revoked if revoked is not None else revoked_ids()):
        return "This key was taken back. Ask for a new one."
    if body.get("e") not in {a.lower() for a in accounts}:
        return f"This key is for {body.get('e')}. Connect that Gmail inbox on this computer to use it."
    return None


def status(accounts):
    """{"open": bool, "why": str, "grace_until": str, "email": str}"""
    if not public_key():
        return {"open": True, "why": "", "grace_until": "", "email": ""}
    testing = os.environ.get("BOTTRACK_TEST_LOCK") == "1"  # jacob trying the key screen on his own copy
    if signing_key() and public_of(signing_key()) == public_key() and not testing:
        return {"open": True, "why": "", "grace_until": "", "email": "developer"}
    key = os.environ.get("BOTTRACK_KEY", "")
    why = check(key, accounts) if key else "Enter your BotTrack key."
    if why is None:
        return {"open": True, "why": "", "grace_until": "", "email": read(key).get("e", "")}
    grace = "" if testing else os.environ.get("BOTTRACK_KEY_GRACE", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {"open": bool(grace and grace >= today), "why": why, "grace_until": grace, "email": ""}


def start_grace(had_data):
    """Keys just turned on for a copy already in use: a week to enter one."""
    if public_key() and had_data and not os.environ.get("BOTTRACK_KEY") and not os.environ.get("BOTTRACK_KEY_GRACE"):
        from .config import save_env
        until = (datetime.now(timezone.utc) + timedelta(days=GRACE_DAYS)).strftime("%Y-%m-%d")
        try:
            save_env({"BOTTRACK_KEY_GRACE": until})
        except OSError:
            os.environ["BOTTRACK_KEY_GRACE"] = until


def init_app(app, had_data=False):
    from . import keyrequests
    start_grace(had_data)
    if public_key():
        threading.Thread(target=revoked_ids, daemon=True).start()
    app.register_blueprint(bp)
    app.register_blueprint(keyrequests.bp)

    @app.before_request
    def need_key():
        if request.path.startswith(OPEN_PATHS) or request.path.startswith("/keys") and signing_key():
            return None
        from .config import Config
        if not status([a for a, _ in Config.from_env().accounts])["open"]:
            return redirect(url_for("accountkeys.activate"))
        return None

    @app.context_processor
    def key_banner():
        asks = 0
        if signing_key():
            try:
                asks = len(keyrequests.waiting(_keys_con()))
            except Exception:
                pass
        if not public_key():
            return {"keys_on": False, "is_developer": bool(signing_key()), "key_grace": "", "key_asks": asks}
        from .config import Config
        s = status([a for a, _ in Config.from_env().accounts])
        return {"keys_on": True, "key_grace": s["grace_until"] if s["open"] and s["why"] else "",
                "is_developer": bool(signing_key()), "key_asks": asks}


# ---- pages ----

@bp.route("/activate", methods=["GET", "POST"])
def activate():
    from .config import Config, format_accounts, save_env
    from .mail import check_login
    config = Config.from_env()
    accounts = [a for a, _ in config.accounts]
    if request.method == "POST":
        key = "".join(request.form.get("key", "").split())
        address = request.form.get("address", "").strip()
        password = request.form.get("password", "").replace(" ", "").strip()
        try:
            body = read(key)
        except BadKey as e:
            flash(str(e))
            return redirect(url_for("accountkeys.activate"))
        if body["e"] not in {a.lower() for a in accounts}:
            if not password or address.lower() != body["e"]:
                flash(f"This key is for {body['e']}. Enter that Gmail address and its app password to connect it.")
                return redirect(url_for("accountkeys.activate", email=body["e"]))
            error = check_login(config, address, password)
            if error:
                flash(f"{address}: {error}")
                return redirect(url_for("accountkeys.activate", email=body["e"]))
            save_env({"GMAIL_ACCOUNTS": format_accounts(config.accounts + [(address, password)])})
            accounts.append(address)
        why = check(key, accounts)
        if why:
            flash(why)
            return redirect(url_for("accountkeys.activate"))
        save_env({"BOTTRACK_KEY": key})
        logger.info("BotTrack key entered for %s", body["e"])
        flash("Welcome to BotTrack. Your key works on every computer where this Gmail is connected.")
        return redirect(url_for("orders"))
    from .keyrequests import developer
    s = status(accounts)
    asked = os.environ.get("BOTTRACK_KEY_ASKED", "").split("|")
    return render_template("activate.html", s=s, accounts=accounts, email=request.args.get("email", ""),
                           can_ask=bool(developer()), asked=asked if len(asked) == 2 else None,
                           testing=os.environ.get("BOTTRACK_TEST_LOCK") == "1" and bool(signing_key()))


def ensure(con):
    from . import db
    return db.setup_once(con, "accountkeys", lambda c: c.executescript(SCHEMA))


def _keys_con():
    from . import db
    return ensure(db.connect(current_app.config.get("DB_PATH")))


@bp.get("/keys")
def keys_page():
    from .mobile import from_this_mac
    if not from_this_mac():
        return redirect("/")
    mine = signing_key()
    from . import keyrequests
    con = _keys_con()
    rows = [dict(r) for r in con.execute("SELECT * FROM license_keys ORDER BY issued_at DESC")]
    return render_template("keys.html", mine=mine, public=public_of(mine) if mine else "", shipped=public_key(),
                           rows=rows, made=request.args.get("made", ""), asks=keyrequests.waiting(con) if mine else [],
                           testing=os.environ.get("BOTTRACK_TEST_LOCK") == "1")


@bp.post("/keys/setup")
def keys_setup():
    from .config import save_env
    from .mobile import from_this_mac
    if not from_this_mac():
        return redirect("/")
    given = request.form.get("private", "").strip()
    if given:  # the signing key saved from another computer
        try:
            public_of(given)
        except Exception:
            flash("That isn't a signing key.")
            return redirect(url_for("accountkeys.keys_page"))
        save_env({"BOTTRACK_SIGNING_KEY": given})
    elif not signing_key():
        private, _ = new_signing_key()
        save_env({"BOTTRACK_SIGNING_KEY": private})
        logger.info("made a signing key for account keys")
    return redirect(url_for("accountkeys.keys_page"))


@bp.post("/keys/new")
def keys_new():
    from .mobile import from_this_mac
    if not from_this_mac() or not signing_key():
        return redirect("/")
    email = request.form.get("email", "").strip().lower()
    if "@" not in email:
        flash("Type the Gmail address the key is for.")
        return redirect(url_for("accountkeys.keys_page"))
    days = request.form.get("days") or None
    key, body = issue(signing_key(), email, days, request.form.get("note", ""))
    _keys_con().execute("INSERT INTO license_keys (kid, email, note, issued_at, expires, key) VALUES (?,?,?,?,?,?)",
                        (body["i"], email, body.get("n", ""), body["t"], body.get("x"), key))
    logger.info("made a BotTrack key for %s (%s)", email, body["i"])
    return redirect(url_for("accountkeys.keys_page", made=body["i"]))


@bp.post("/keys/test-lock")
def keys_test_lock():
    """Lock this copy as if it weren't jacob's, to go through the key screen; on again clears any key entered."""
    from .config import save_env
    from .mobile import from_this_mac
    if not from_this_mac() or not signing_key():
        return redirect("/")
    if request.form.get("on") == "1":
        save_env({"BOTTRACK_TEST_LOCK": "1", "BOTTRACK_KEY": "", "BOTTRACK_KEY_ASKED": ""})
        return redirect(url_for("accountkeys.activate"))
    save_env({"BOTTRACK_TEST_LOCK": ""})
    flash("Testing is over: this copy opens with your signing key again.")
    return redirect(url_for("accountkeys.keys_page"))


@bp.post("/keys/<kid>/revoke")
def keys_revoke(kid):
    from .mobile import from_this_mac
    if not from_this_mac():
        return redirect("/")
    _keys_con().execute("UPDATE license_keys SET revoked_at=? WHERE kid=?", (datetime.now(timezone.utc).strftime("%Y-%m-%d"), kid))
    flash(f"Marked {kid} as taken back. Ask Claude to add {kid} to the list of keys taken back; "
          "it then stops working everywhere within a day.")
    return redirect(url_for("accountkeys.keys_page"))
