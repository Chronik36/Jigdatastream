"""Retail store accounts: the email and password (and a username where the site wants one, like Riot) for each
account you check out with, so they're in one place next to your profiles and orders.

Passwords are encrypted with this computer's card key (wallet.py), like card numbers, and only shown when you
press the eye or copy button on this Mac; a phone can see the list but never a password.
"""
import logging
import re
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

from . import db, wallet

logger = logging.getLogger("jigstream.storeaccounts")
bp = Blueprint("storeaccounts", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS store_accounts (
    id INTEGER PRIMARY KEY, site TEXT, email TEXT, username TEXT, password TEXT,
    status TEXT DEFAULT 'active', notes TEXT, created_at TEXT, updated_at TEXT
);
"""
# Picked in the Add window; anything else can be typed.
SITES = ["Target", "Walmart", "Best Buy", "Pokemon Center", "Amazon", "GameStop", "Sam's Club", "Costco",
         "Nike", "Topps", "Fanatics", "Riot"]
# Sites that sign in with a username as well as an email.
NEEDS_USERNAME = ("riot",)
STATUSES = {"active": "Active", "locked": "Locked", "banned": "Banned", "unused": "Not used yet"}
EMAIL = re.compile(r"[^@\s:;,|]+@[^@\s:;,|]+\.[A-Za-z]{2,}")


def ensure(con):
    con.executescript(SCHEMA)
    return con


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def site_name(text):
    """"riot.com", "www.target.com" or "pokemon center" -> the name on the Sites list when it's one of them."""
    t = (text or "").strip()
    key = re.sub(r"^(https?://)?(www\.)?", "", t.lower()).split("/")[0]
    key = re.sub(r"\.(com|net|org|co|us)$", "", key).replace("-", " ").replace("'", "")
    for s in SITES:
        if key.replace(" ", "") == s.lower().replace(" ", "").replace("'", ""):
            return s
    return t


def needs_username(site):
    return any(k in (site or "").lower() for k in NEEDS_USERNAME)


def _seal(password):
    return wallet._fernet().encrypt(password.encode()).decode() if password else None


def password(con, account_id):
    row = ensure(con).execute("SELECT password FROM store_accounts WHERE id = ?", (account_id,)).fetchone()
    if not row or not row["password"]:
        return None
    return wallet._fernet().decrypt(row["password"].encode()).decode()


def parse_lines(text, site):
    """Pasted accounts, one per line: email:password, email:password:username or username:email:password.
    Colons, commas, tabs, semicolons or | between them. Returns (accounts, problems)."""
    out, problems = [], []
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[:\t,;|]", line)]
        at = next((i for i, p in enumerate(parts) if EMAIL.fullmatch(p)), None)
        if at is None:
            problems.append(f"line {n}: no email")
            continue
        rest = parts[:at] + parts[at + 1:]
        if at == 0:
            pw, user = (rest + ["", ""])[:2]
        else:  # username first
            user, pw = (rest + ["", ""])[:2]
        if not pw:
            problems.append(f"line {n}: no password")
            continue
        out.append({"site": site, "email": parts[at], "password": pw, "username": user or None})
    return out, problems


def save(con, site, email, pw=None, username=None, status=None, notes=None):
    """Add an account, or update it when this site already has this email. Returns (id, added)."""
    ensure(con)
    site, email = site_name(site), (email or "").strip()
    row = con.execute("SELECT id FROM store_accounts WHERE lower(site) = lower(?) AND lower(email) = lower(?)",
                      (site, email)).fetchone()
    if row:
        fields = {"updated_at": _now()}
        if pw:
            fields["password"] = _seal(pw)
        if username:
            fields["username"] = username
        if status in STATUSES:
            fields["status"] = status
        if notes is not None:
            fields["notes"] = notes or None
        con.execute(f"UPDATE store_accounts SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                    (*fields.values(), row["id"]))
        con.commit()
        return row["id"], False
    cur = con.execute("INSERT INTO store_accounts (site, email, username, password, status, notes, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,?,?)", (site, email, username or None, _seal(pw),
                                                    status if status in STATUSES else "active", notes or None,
                                                    _now(), _now()))
    con.commit()
    return cur.lastrowid, True


def rows(con, site=None, status=None):
    """Accounts with the profile that uses the same email, and how many orders that profile has."""
    ensure(con)
    where, args = [], []
    if site:
        where.append("lower(a.site) = lower(?)")
        args.append(site)
    if status:
        where.append("a.status = ?")
        args.append(status)
    q = ("SELECT a.id, a.site, a.email, a.username, a.status, a.notes, a.created_at, a.password IS NOT NULL AS has_pw,"
         " (SELECT p.name FROM profiles p WHERE lower(p.email) = lower(a.email) LIMIT 1) AS profile"
         " FROM store_accounts a" + (" WHERE " + " AND ".join(where) if where else "") +
         " ORDER BY lower(a.site), lower(a.email)")
    out = [dict(r) for r in con.execute(q, args)]
    if out:
        from .orderstore import Store
        Store(con)  # profile_name on older databases
    counts = dict(con.execute("SELECT profile_name, COUNT(*) FROM tracked_orders WHERE profile_name IS NOT NULL"
                              " GROUP BY profile_name").fetchall()) if out else {}
    for r in out:
        r["orders"] = counts.get(r["profile"], 0) if r["profile"] else 0
    return out


def sites(con):
    return [dict(r) for r in ensure(con).execute(
        "SELECT site, COUNT(*) AS n, SUM(status = 'active') AS active FROM store_accounts GROUP BY lower(site)"
        " ORDER BY n DESC, lower(site)")]


def _con():
    return db.connect(current_app.config["DB_PATH"])


def _back():
    return redirect(request.form.get("back") or request.referrer or url_for("storeaccounts.page"))


@bp.get("/accounts")
def page():
    c = _con()
    site, status = request.args.get("site") or None, request.args.get("status") or None
    from . import browsers, proxies
    opened = {r[0]: r[1] for r in browsers.ensure(c).execute(
        "SELECT account_id, proxy_id FROM browsers WHERE account_id IS NOT NULL GROUP BY account_id")}
    return render_template("accounts.html", accounts=rows(c, site, status), site_counts=sites(c), site=site,
                           proxy_groups=[(g["name"], g["proxies"]) for g in proxies.groups(c)], browser_proxy=opened,
                           status=status, statuses=STATUSES, presets=SITES,
                           total=c.execute("SELECT COUNT(*) FROM store_accounts").fetchone()[0],
                           by_status=dict(c.execute("SELECT status, COUNT(*) FROM store_accounts GROUP BY status")
                                          .fetchall()))


@bp.post("/accounts/add")
def add():
    c = _con()
    site = site_name(request.form.get("site", ""))
    if not site:
        flash("Pick or type the store.")
        return _back()
    status = request.form.get("status") or "active"
    if (request.form.get("lines") or "").strip():
        found, problems = parse_lines(request.form["lines"], site)
    else:
        email, pw = (request.form.get("email") or "").strip(), request.form.get("password") or ""
        found = [{"site": site, "email": email, "password": pw, "username": (request.form.get("username") or "").strip()}]
        problems = [] if EMAIL.fullmatch(email) else ["that email doesn't look right"]
        found = found if not problems else []
    if needs_username(site):
        missing = [a["email"] for a in found if not a["username"]]
        if missing:
            problems.append(f"{site} needs a username too ({len(missing)} without one)")
            found = [a for a in found if a["username"]]
    added = updated = 0
    for a in found:
        _, new = save(c, a["site"], a["email"], a["password"], a["username"], status,
                      (request.form.get("notes") or "").strip() or None)
        added, updated = added + new, updated + (not new)
    logger.info("store accounts: %d added, %d updated for %s", added, updated, site)  # never the passwords
    msg = f"Added {added} {site} account{'' if added == 1 else 's'}." if added else ""
    msg += f" Updated {updated} already saved." if updated else ""
    flash((msg.strip() or "Nothing added.") + (" Skipped: " + "; ".join(problems[:5]) if problems else ""))
    return redirect(url_for("storeaccounts.page", site=site if request.form.get("stay") != "all" else None))


@bp.post("/accounts/<int:account_id>/edit")
def edit(account_id):
    c = ensure(_con())
    a = c.execute("SELECT * FROM store_accounts WHERE id = ?", (account_id,)).fetchone()
    if not a:
        return _back()
    site = site_name(request.form.get("site") or a["site"])
    email = (request.form.get("email") or a["email"]).strip()
    username = (request.form.get("username") or "").strip() or None
    if needs_username(site) and not username:
        flash(f"{site} needs a username.")
        return _back()
    fields = {"site": site, "email": email, "username": username, "notes": (request.form.get("notes") or "").strip() or None,
              "updated_at": _now()}
    if request.form.get("status") in STATUSES:
        fields["status"] = request.form["status"]
    if request.form.get("password"):  # blank keeps the saved one
        fields["password"] = _seal(request.form["password"])
    c.execute(f"UPDATE store_accounts SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
              (*fields.values(), account_id))
    c.commit()
    flash(f"Saved {email}.")
    return _back()


@bp.post("/accounts/bulk")
def bulk():
    c = ensure(_con())
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    action = request.form.get("action", "")
    if not ids:
        flash("Tick one or more accounts first.")
    elif action == "delete":
        c.executemany("DELETE FROM store_accounts WHERE id = ?", [(i,) for i in ids])
        c.commit()
        flash(f"Deleted {len(ids)} account(s).")
    elif action in STATUSES:
        c.executemany("UPDATE store_accounts SET status = ?, updated_at = ? WHERE id = ?",
                      [(action, _now(), i) for i in ids])
        c.commit()
        flash(f"Marked {len(ids)} account(s) {STATUSES[action].lower()}.")
    return _back()


@bp.get("/accounts/<int:account_id>/password")
def reveal(account_id):
    """The password, for the eye and copy buttons. Never logged, never sent to a phone."""
    pw = password(_con(), account_id)
    logger.info("store account %s password shown", account_id)
    return (jsonify({"password": pw}), 200) if pw is not None else (jsonify({"error": "No password saved."}), 404)
