"""Optional live bank sync through Plaid, with your own free Plaid keys ("bring your own keys").

You make a free account at dashboard.plaid.com, paste its client_id and secret on the Bank page, and
sign in to your bank in Plaid's own window. BotTrack never sees your bank password: Plaid hands back an
access token, which is stored encrypted with the same Keychain-held key as your cards. Transactions then
sync every few hours into the same table as imported statement files and are matched the same way.
The client_id, secret and access tokens are never written to the logs.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import bank, wallet

logger = logging.getLogger("jigstream.plaid")

HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS plaid_settings (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS plaid_items (
    item_id TEXT PRIMARY KEY,
    institution TEXT,
    token TEXT,
    cursor TEXT,
    last_synced_at TEXT,
    error TEXT,
    created_at TEXT
);
"""
state = {"running": False, "last": None}
_lock = threading.Lock()


class PlaidError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def ensure(con):
    con.executescript(SCHEMA)
    return con


def _encrypt(text):
    return wallet._fernet().encrypt(text.encode()).decode()


def _decrypt(text):
    return wallet._fernet().decrypt(text.encode()).decode()


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------- your keys ----------

def save_keys(con, client_id, secret, env):
    ensure(con)
    if env not in HOSTS:
        env = "sandbox"
    con.executemany("INSERT OR REPLACE INTO plaid_settings VALUES (?, ?)",
                    [("client_id", _encrypt(client_id)), ("secret", _encrypt(secret)), ("env", env)])
    con.commit()


def remove_keys(con):
    ensure(con)
    con.execute("DELETE FROM plaid_settings")
    con.commit()


def keys(con):
    """(client_id, secret, env) or None."""
    ensure(con)
    s = {r[0]: r[1] for r in con.execute("SELECT name, value FROM plaid_settings")}
    if not s.get("client_id") or not s.get("secret"):
        return None
    try:
        return _decrypt(s["client_id"]), _decrypt(s["secret"]), s.get("env") or "sandbox"
    except Exception:  # the key changed (data copied to another Mac): ask for the keys again
        logger.warning("saved Plaid keys couldn't be decrypted; paste them again")
        return None


def settings(con):
    """What the page may show: never the secret, only the env and the client_id's last characters."""
    k = keys(con)
    return {"configured": bool(k), "env": k[2] if k else "sandbox", "client_hint": ("…" + k[0][-4:]) if k else ""}


# ---------- talking to Plaid ----------

def call(con, path, payload, creds=None, timeout=30):
    creds = creds or keys(con)
    if not creds:
        raise PlaidError("Paste your Plaid keys first.")
    client_id, secret, env = creds
    body = json.dumps({"client_id": client_id, "secret": secret, **payload}).encode()
    req = urllib.request.Request(HOSTS[env] + path, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()[:4000])
        except ValueError:
            err = {}
        code = err.get("error_code")
        message = err.get("display_message") or err.get("error_message") or f"HTTP {e.code}"
        raise PlaidError(f"{message} ({code})" if code else message, code) from None
    except (urllib.error.URLError, OSError) as e:
        raise PlaidError(f"couldn't reach Plaid ({e})") from None


def check_keys(client_id, secret, env):
    """Plaid accepts the keys if it will make a Link token with them."""
    link_token(None, creds=(client_id, secret, env))


def link_token(con, item_id=None, creds=None):
    """A one-time token for Plaid's bank sign-in window. With item_id, the window reconnects that bank."""
    payload = {"client_name": "BotTrack", "language": "en", "country_codes": ["US"],
               "user": {"client_user_id": "bottrack-local"}}
    if item_id:
        payload["access_token"] = _token(con, item_id)
    else:
        payload["products"] = ["transactions"]
        payload["transactions"] = {"days_requested": 730}
    return call(con, "/link/token/create", payload, creds)["link_token"]


def _token(con, item_id):
    row = ensure(con).execute("SELECT token FROM plaid_items WHERE item_id = ?", (item_id,)).fetchone()
    if not row:
        raise PlaidError("That bank isn't linked anymore.")
    return _decrypt(row[0])


def exchange(con, public_token, institution):
    """After you sign in: swap the one-time public token for an access token and remember the accounts."""
    data = call(con, "/item/public_token/exchange", {"public_token": public_token})
    item_id, token = data["item_id"], data["access_token"]
    con.execute("INSERT OR REPLACE INTO plaid_items (item_id, institution, token, created_at) VALUES (?,?,?,?)",
                (item_id, institution or "Bank", _encrypt(token), _now()))
    con.commit()
    _save_accounts(con, item_id, institution, token)
    logger.info("Plaid: linked %s", institution or "a bank")
    return item_id


def _save_accounts(con, item_id, institution, token):
    for a in call(con, "/accounts/get", {"access_token": token}).get("accounts", []):
        name = " ".join(x for x in (institution, a.get("name"), f"…{a['mask']}" if a.get("mask") else "") if x)
        bank.account_for(con, name, "plaid", institution, a.get("mask"), item_id, a["account_id"])
    con.commit()


def remove_item(con, item_id, keep_transactions=True):
    try:
        call(con, "/item/remove", {"access_token": _token(con, item_id)})
    except PlaidError as e:
        logger.warning("Plaid: removing the link failed on Plaid's side (%s); forgetting it here anyway", e)
    if not keep_transactions:
        for (account_id,) in con.execute("SELECT id FROM bank_accounts WHERE plaid_item_id = ?", (item_id,)).fetchall():
            bank.delete_account(con, account_id)
    con.execute("DELETE FROM plaid_items WHERE item_id = ?", (item_id,))
    con.commit()
    logger.info("Plaid: unlinked a bank")


def items(con):
    ensure(con)
    return [{k: r[k] for k in r.keys() if k != "token"} for r in
            con.execute("SELECT * FROM plaid_items ORDER BY institution")]


def _row(tx, account_ids):
    category = (tx.get("personal_finance_category") or {}).get("primary") or ", ".join(tx.get("category") or [])
    return {"uid": f"plaid:{tx['transaction_id']}", "date": tx.get("authorized_date") or tx["date"],
            "description": tx.get("merchant_name") or tx.get("name") or "",
            "amount": round(float(tx["amount"]), 2),  # Plaid: positive = money out, like ours
            "currency": tx.get("iso_currency_code") or tx.get("unofficial_currency_code") or "USD",
            "pending": tx.get("pending"), "category": category.replace("_", " ").title() if category else None,
            "account_id": account_ids.get(tx["account_id"]), "replaces": tx.get("pending_transaction_id")}


def sync_item(con, item_id):
    """Pull everything new since the last sync for one linked bank. Returns (added, updated, removed)."""
    row = con.execute("SELECT * FROM plaid_items WHERE item_id = ?", (item_id,)).fetchone()
    token, cursor = _decrypt(row["token"]), row["cursor"]
    account_ids = {r[0]: r[1] for r in con.execute(
        "SELECT plaid_account_id, id FROM bank_accounts WHERE plaid_item_id = ?", (item_id,))}
    added = modified = removed = []
    for attempt in range(3):
        added, modified, removed, next_cursor = [], [], [], cursor
        try:
            while True:
                page = call(con, "/transactions/sync", {"access_token": token, "cursor": next_cursor or "",
                                                         "count": 500})
                added += page.get("added", [])
                modified += page.get("modified", [])
                removed += page.get("removed", [])
                next_cursor = page.get("next_cursor")
                if not page.get("has_more"):
                    break
            break
        except PlaidError as e:
            if e.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" or attempt == 2:
                raise
            time.sleep(2)  # Plaid changed the data mid-pull: start this pull over
    if any(t["account_id"] not in account_ids for t in added + modified):
        _save_accounts(con, item_id, row["institution"], token)  # a new card on the same login
        account_ids = {r[0]: r[1] for r in con.execute(
            "SELECT plaid_account_id, id FROM bank_accounts WHERE plaid_item_id = ?", (item_id,))}
    for t in removed:
        _drop(con, f"plaid:{t['transaction_id']}")
    rows = [_row(t, account_ids) for t in added + modified]
    for r in rows:
        if r["replaces"]:  # a pending charge posted: keep its match on the posted one
            old = con.execute("SELECT match_kind, match_id, match_how FROM bank_transactions WHERE uid = ?",
                              (f"plaid:{r['replaces']}",)).fetchone()
            _drop(con, f"plaid:{r['replaces']}")
            if old and old["match_how"]:
                r["carry"] = tuple(old)
    by_account = {}
    for r in rows:
        by_account.setdefault(r["account_id"], []).append(r)
    new = 0
    for account_id, group in by_account.items():
        new += bank.import_rows(con, account_id, group, "plaid")[0]
    for r in rows:
        if r.get("carry"):
            con.execute("UPDATE bank_transactions SET match_kind = ?, match_id = ?, match_how = ? WHERE uid = ?",
                        (*r["carry"], r["uid"]))
    con.execute("UPDATE plaid_items SET cursor = ?, last_synced_at = ?, error = NULL WHERE item_id = ?",
                (next_cursor, _now(), item_id))
    con.commit()
    return new, len(modified), len(removed)


def _drop(con, uid):
    con.execute("DELETE FROM expenses WHERE message_id = ?", (f"bank:{uid}",))
    con.execute("DELETE FROM bank_transactions WHERE uid = ?", (uid,))


def run(con):
    """Sync every linked bank, then match. Safe to call from a timer; one run at a time."""
    if not _lock.acquire(blocking=False):
        return None
    state["running"] = True
    parts = []
    try:
        ensure(con)
        for item in items(con):
            name = item["institution"] or "Bank"
            try:
                new, changed, gone = sync_item(con, item["item_id"])
                parts.append(f"{name}: {new} new")
                logger.info("Plaid: %s synced, %d new, %d changed, %d removed", name, new, changed, gone)
            except PlaidError as e:
                hint = " Press Reconnect to sign in again." if e.code in ("ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION") else ""
                con.execute("UPDATE plaid_items SET error = ? WHERE item_id = ?", (str(e) + hint, item["item_id"]))
                con.commit()
                parts.append(f"{name}: failed")
                logger.warning("Plaid: %s sync failed: %s", name, e)
            except Exception:
                logger.exception("Plaid: %s sync failed", name)
                parts.append(f"{name}: failed (see Logs)")
        matched = bank.auto_match(con)
        state["last"] = f"Synced {_now()[:16].replace('T', ' ')} UTC. " + "; ".join(parts) + f". {matched} matched."
    finally:
        state["running"] = False
        _lock.release()
    return state["last"]


def due(con, every_hours):
    """True when a bank is linked and the last sync is older than every_hours."""
    if every_hours <= 0 or state["running"] or not keys(con):
        return False
    linked = items(con)
    oldest = min((i["last_synced_at"] or "" for i in linked), default=None)
    if oldest is None:
        return False
    cutoff = datetime.fromtimestamp(time.time() - every_hours * 3600, timezone.utc).isoformat()
    return oldest < cutoff
