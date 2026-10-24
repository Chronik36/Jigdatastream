"""Session type (rotating or sticky) and sticky TTL for providers you sign in to (proxylogin.py).

Every provider's dashboard sets these its own way: a switch, a dropdown, buttons, or a box you type minutes into.
So each choice is taught once: you pick it here, BotTrack opens the dashboard, and you set it there and generate a
list. The steps you took (what you clicked, picked and typed, never sign-in boxes) and the request or button that
made the list are kept as that choice's "setting", and Generate repeats them from then on. A TTL you typed into a
box is kept as a blank, so any other number of minutes works without teaching it again.

Shared steps: the steps (only the motions: which control, which choice, and the words on it; no sign-ins, cookies,
tokens, list addresses or anything typed except minutes) can be emailed to the developer and ship with updates,
so the next person on the same provider can have them done for them while they teach it.
"""
import base64
import gzip
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, Response, current_app, flash, redirect, request, url_for

from . import db, logs

bp = Blueprint("proxysessions", __name__)

SESSIONS = {"rotating": "Rotating", "sticky": "Sticky"}
TTLS = [1, 5, 10, 15, 30, 60, 120, 360, 720, 1440]  # minutes offered in the TTL list; any other can be typed
MAX_TTL = 60 * 24 * 7
FORMAT = "bottrack-motions"
SHARED_FILE = Path(__file__).with_name("shared") / "motions.json"
SIGNIN_PAGE = re.compile(r"login|signin|sign-in|auth|oauth|callback|discord\.com", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS proxy_settings (
    id INTEGER PRIMARY KEY, account_id INTEGER, session TEXT, ttl INTEGER DEFAULT 0,
    steps TEXT, recipe TEXT, any_ttl INTEGER DEFAULT 0, direct INTEGER DEFAULT 0, made_at TEXT,
    UNIQUE (account_id, session, ttl)
);
CREATE TABLE IF NOT EXISTS shared_motions (
    id INTEGER PRIMARY KEY, source TEXT, host TEXT, session TEXT, ttl INTEGER DEFAULT 0, any_ttl INTEGER DEFAULT 0,
    steps TEXT, UNIQUE (host, session, ttl, steps)
);
"""


def ensure(con):
    return db.setup_once(con, "proxysessions", lambda c: c.executescript(SCHEMA))


def ttl_words(ttl):
    ttl = int(ttl or 0)
    if ttl and ttl % 1440 == 0:
        return f"{ttl // 1440} day{'s' if ttl > 1440 else ''}"
    if ttl and ttl % 60 == 0:
        return f"{ttl // 60} h"
    return f"{ttl} min"


def label(session, ttl=0):
    if session == "sticky":
        return f"Sticky · {ttl_words(ttl)}"
    return SESSIONS.get(session, "As first shown")


def choice(form):
    """(session, ttl in minutes) from the Generate form, or (None, 0) for "as first shown"."""
    session = form.get("session") or ""
    if session not in SESSIONS:
        return None, 0
    if session == "rotating":
        return "rotating", 0
    raw = form.get("ttl_other") if form.get("ttl") == "other" else form.get("ttl")
    try:
        ttl = int(float(raw or 0))
    except ValueError:
        ttl = 0
    if not 1 <= ttl <= MAX_TTL:
        raise ValueError("Pick how long a sticky session lasts (1 minute to 7 days).")
    return "sticky", ttl


def host_of(url):
    h = (urlparse(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _same_site(page, host):
    h = host_of(page)
    return bool(host) and (h == host or h.endswith("." + host) or host.endswith("." + h))


# ---- turning what the window saw into a setting ----

def clean(steps, site_url, until=None, ttl=0):
    """The steps that set the choice and made the list: only on the provider's own site, after any sign-in, up to
    the moment the list showed up, without repeats. A typed TTL becomes {ttl}. Returns (steps, any_ttl)."""
    host = host_of(site_url)
    steps = [s for s in steps if isinstance(s, dict) and s.get("act") in ("click", "select", "type", "check")]
    if until:
        steps = [s for s in steps if (s.get("at") or 0) <= until + 1500]
    last_signin = max((i for i, s in enumerate(steps) if SIGNIN_PAGE.search(s.get("page") or "")), default=-1)
    steps = [s for s in steps[last_signin + 1:] if _same_site(s.get("page"), host)]
    out = []
    for s in steps:
        s = {k: s[k] for k in ("act", "el", "value", "text", "on", "page") if k in s}
        if out and s["act"] in ("type", "select") and out[-1]["act"] == s["act"] and _same_el(out[-1]["el"], s["el"]):
            out[-1] = s  # typed or picked again: only the last value counts
            continue
        out.append(s)
    any_ttl = False
    if ttl:
        for s in out:
            if s["act"] == "type" and _number(s.get("value")) == ttl:
                s["value"], any_ttl = "{ttl}", True
    return out, any_ttl


def _same_el(a, b):
    return all((a or {}).get(k) == (b or {}).get(k) for k in ("tag", "id", "name", "label", "index"))


def _number(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def fill(steps, ttl):
    return [dict(s, value=str(ttl)) if s.get("value") == "{ttl}" else s for s in steps]


def describe(s):
    el = s.get("el") or {}
    what = el.get("label") or el.get("text") or el.get("name") or el.get("tag", "").lower()
    if s["act"] == "click":
        return f'clicked "{el.get("text") or what}"'
    if s["act"] == "select":
        return f'picked "{s.get("text") or s.get("value")}"' + (f' in "{el["label"]}"' if el.get("label") else "")
    if s["act"] == "check":
        return f'{"ticked" if s.get("on") else "unticked"} "{what}"'
    v = "the TTL" if s.get("value") == "{ttl}" else f'"{s.get("value")}"'
    return f"typed {v}" + (f' in "{what}"' if what else "")


def direct_ok(recipe, steps, session, ttl):
    """Can Generate send the dashboard's request straight from BotTrack? Only when the request itself says which
    session it wants (or nothing had to be set first); otherwise the choice may live in the page, so the steps are
    done again in the window."""
    if not recipe or recipe.get("kind") != "request":
        return False
    setting_steps = steps[:-1] if steps and steps[-1]["act"] == "click" else steps
    if not setting_steps:
        return True
    said = f"{recipe.get('url', '')} {recipe.get('body') or ''}".lower()
    if session == "sticky":
        return bool(re.search(rf"(?<!\d){ttl}(?!\d)", said)) or bool(re.search(rf"(?<!\d){ttl * 60}(?!\d)", said))
    return "rotat" in said


def save(con, aid, session, ttl, recipe, steps, site_url):
    ensure(con)
    until = (recipe or {}).pop("at", None)
    steps, any_ttl = clean(steps, site_url, until, ttl)
    direct = direct_ok(recipe, steps, session, ttl) and not any_ttl
    con.execute("INSERT INTO proxy_settings (account_id, session, ttl, steps, recipe, any_ttl, direct, made_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (account_id, session, ttl) DO UPDATE SET steps=excluded.steps, "
                "recipe=excluded.recipe, any_ttl=excluded.any_ttl, direct=excluded.direct, made_at=excluded.made_at",
                (aid, session, ttl, json.dumps(steps), json.dumps(recipe), int(any_ttl), int(direct),
                 datetime.now().isoformat(timespec="seconds")))
    con.execute("UPDATE proxy_accounts SET sess_kind=?, sess_ttl=? WHERE id=?", (session, ttl, aid))
    con.commit()
    return steps


def settings(con, aid):
    ensure(con)
    out = []
    for r in con.execute("SELECT id, session, ttl, steps, any_ttl, direct, made_at FROM proxy_settings WHERE account_id=? "
                         "ORDER BY session, ttl", (aid,)):
        d = dict(r)
        d["steps"] = json.loads(d["steps"] or "[]")
        d["label"] = label(d["session"], d["ttl"])
        d["words"] = [describe(s) for s in d["steps"]]
        out.append(d)
    return out


def find(con, aid, session, ttl):
    """The taught setting for this choice: the exact one, or a sticky one whose TTL was typed (any minutes work)."""
    ensure(con)
    row = con.execute("SELECT * FROM proxy_settings WHERE account_id=? AND session=? AND ttl=?", (aid, session, ttl)).fetchone()
    if row is None and session == "sticky":
        row = con.execute("SELECT * FROM proxy_settings WHERE account_id=? AND session='sticky' AND any_ttl=1 "
                          "ORDER BY made_at DESC", (aid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["recipe"] = json.loads(d["recipe"] or "{}")
    d["steps"] = fill(json.loads(d["steps"] or "[]"), ttl)
    d["direct"] = bool(d["direct"]) and d["ttl"] == ttl
    return d


# ---- shared steps ----

TOKENISH = re.compile(r"[A-Za-z0-9_\-]{20,}|@|\d{6,}")
RANDOM_ID = re.compile(r"\d{3,}|[a-f0-9]{8,}", re.I)  # same as the window's own check on ids


def scrub(steps):
    """Steps as shared: page paths without ids or queries, no typed values but minutes, no odd-looking ids."""
    out = []
    for s in steps:
        el = {k: v for k, v in (s.get("el") or {}).items() if k in ("tag", "text", "label", "name", "id", "role", "type", "index")}
        for k in ("text", "label", "name", "id"):
            if TOKENISH.search(str(el.get(k) or "")) or (k == "id" and RANDOM_ID.search(str(el.get(k) or ""))):
                el[k] = ""
        t = {"act": s["act"], "el": el}
        if s["act"] == "type":
            v = s.get("value")
            if (v != "{ttl}" and _number(v) is None) or len(str(v)) > 6:
                continue  # only minutes and counts are typed into a shared step
            t["value"] = v
        elif s["act"] == "select":
            t["text"] = "" if TOKENISH.search(str(s.get("text") or "")) else s.get("text")
            t["value"] = "" if TOKENISH.search(str(s.get("value") or "")) else s.get("value")
        elif s["act"] == "check":
            t["on"] = bool(s.get("on"))
        path = urlparse(s.get("page") or "").path
        t["page"] = re.sub(r"/[^/]*\d{3,}[^/]*|/[0-9a-f-]{16,}", "/*", path)
        out.append(t)
    return out


def build(con):
    """Every taught setting on this Mac as a shareable pack: provider site, choice and scrubbed steps."""
    ensure(con)
    motions = []
    for r in con.execute("SELECT a.site_url, s.session, s.ttl, s.any_ttl, s.steps FROM proxy_settings s "
                         "JOIN proxy_accounts a ON a.id = s.account_id"):
        steps = scrub(json.loads(r["steps"] or "[]"))
        if steps and host_of(r["site_url"]):
            motions.append({"host": host_of(r["site_url"]), "session": r["session"], "ttl": r["ttl"],
                            "any_ttl": bool(r["any_ttl"]), "steps": steps})
    return {"format": FORMAT, "version": 1, "created": date.today().isoformat(), "motions": motions}


def encode(pack):
    from .packs import encode as enc
    return enc(pack)


def decode(text):
    """A pack from a file or an email body: plain JSON, or the encoded block training packs use too."""
    from .packs import FORMAT as EXAMPLES
    text = text.strip()
    m = re.search(r"-----BEGIN BOTTRACK PACK-----(.*?)-----END BOTTRACK PACK-----", text, re.S)
    data = json.loads(gzip.decompress(base64.b64decode(re.sub(r"\s", "", m.group(1)))) if m else text)
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        if isinstance(data, dict) and data.get("format") == EXAMPLES:
            raise ValueError("That's a training pack; import it on Training > Shared learning.")
        raise ValueError("That isn't a BotTrack shared steps pack.")
    return data


def merge(packs):
    seen, out = set(), []
    for pack in packs:
        for m in pack.get("motions", []):
            if not (m.get("host") and m.get("session") in SESSIONS and m.get("steps")):
                continue
            key = (m["host"], m["session"], int(m.get("ttl") or 0), json.dumps(m["steps"], sort_keys=True))
            if key not in seen:
                seen.add(key)
                out.append(m)
    return {"format": FORMAT, "version": 1, "created": date.today().isoformat(), "motions": out}


def import_pack(con, pack, source):
    ensure(con)
    n = 0
    for m in merge([pack])["motions"]:
        n += con.execute("INSERT OR IGNORE INTO shared_motions (source, host, session, ttl, any_ttl, steps) VALUES (?,?,?,?,?,?)",
                         (source, m["host"], m["session"], int(m.get("ttl") or 0), int(bool(m.get("any_ttl"))),
                          json.dumps(scrub(m["steps"]), sort_keys=True))).rowcount
    con.commit()
    return n


def built_in():
    try:
        return decode(SHARED_FILE.read_text())["motions"]
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def shared(con):
    ensure(con)
    rows = [dict(r) for r in con.execute("SELECT host, session, ttl, any_ttl, steps FROM shared_motions")]
    for r in rows:
        r["steps"] = json.loads(r["steps"])
    return built_in() + rows


def shared_for(con, site_url, session, ttl):
    """Shared steps for this provider and choice: the same TTL first, else a sticky one with a typed TTL."""
    host = host_of(site_url)
    mine = [m for m in shared(con) if m["host"] == host and m["session"] == session]
    exact = [m for m in mine if int(m.get("ttl") or 0) == ttl]
    pick = exact or [m for m in mine if m.get("any_ttl")]
    return fill(pick[0]["steps"], ttl) if pick else None


def send(pack, sender, password, version="", to=None, smtp=None):
    import smtplib
    import ssl
    from email.message import EmailMessage
    from .packs import developer
    to = to or developer()
    if not to:
        raise RuntimeError("This copy of BotTrack has no developer address to share with.")
    msg = EmailMessage()
    msg["Subject"] = f"BotTrack shared steps ({len(pack['motions'])} settings, {version or 'unknown build'})"
    msg["From"], msg["To"] = sender, to
    msg.set_content("Proxy dashboard steps shared from BotTrack: which controls set rotating/sticky and the TTL, and the "
                    "Generate button. No sign-ins, cookies, tokens or typed text except minutes.\n"
                    "Merge into jigstream/shared/motions.json, or import on the Proxies tab.\n\n" + encode(pack))
    msg.add_attachment(json.dumps(pack, indent=1).encode(), maintype="application", subtype="json",
                       filename="bottrack-shared-steps.json")
    if smtp is None:
        smtp = smtplib.SMTP_SSL(logs.SMTP_HOST, 465, context=ssl.create_default_context(), timeout=30)
    with smtp as s:
        s.login(sender, password)
        s.send_message(msg)
    return to


# ---- routes ----

def _con():
    from . import proxyaccounts
    return ensure(proxyaccounts.ensure(db.connect(current_app.config.get("DB_PATH"))))


def _back():
    return redirect(url_for("proxies.page") + "#accounts")


def generate(con, acct, form):
    """Generate on a signed-in account: the taught setting for the picked choice, or teach it now."""
    from . import proxyaccounts, proxylogin
    try:
        session, ttl = choice(form)
    except ValueError as e:
        flash(str(e))
        return _back()
    aid = acct["id"]
    con.execute("UPDATE proxy_accounts SET sess_kind=?, sess_ttl=? WHERE id=?", (session or "", ttl, aid))
    con.commit()
    if session is None:
        n = proxyaccounts.refresh_pool(con, acct)
        flash(f"{acct['name']}: the {acct['grp'] or acct['name']} group now has {n} proxies from the provider.")
        return _back()
    setting = find(con, aid, session, ttl)
    if setting:
        try:
            n = proxyaccounts.refresh_pool(con, acct, setting=setting)
            flash(f"{acct['name']} ({label(session, ttl)}): the {acct['grp'] or acct['name']} group now has {n} proxies.")
        except ValueError as e:
            flash(f"{acct['name']} ({label(session, ttl)}): {e}")
        return _back()
    # Not taught yet: open the dashboard and watch.
    try:
        saved = proxylogin.unseal(acct.get("site_session"))
        page = (json.loads(acct.get("recipe") or "{}").get("page")) or acct.get("site_url")
        proxylogin.session().open(aid, page, acct["name"], saved, combo={"session": session, "ttl": ttl})
    except proxylogin.SignIn as e:
        flash(str(e))
        return _back()
    flash(f"Teach {acct['name']} {label(session, ttl)} once: in the window, choose {label(session, ttl).lower()} the way "
          "the dashboard asks, then generate a list. BotTrack keeps those steps for next time.")
    return redirect(url_for("proxies.page", learning=aid) + "#accounts")


@bp.post("/proxies/sessions/forget")
def forget():
    con = _con()
    con.execute("DELETE FROM proxy_settings WHERE id=?", (request.form.get("id", type=int),))
    con.commit()
    flash("Forgot that setting. Pick it again to teach it again.")
    return _back()


@bp.post("/proxies/sessions/share")
def share():
    from .config import Config
    con = _con()
    accounts = dict(Config.from_env().accounts)
    sender = next(iter(accounts), "")
    pack = build(con)
    if not request.form.get("consent"):
        flash("Tick the box to say it's OK to share these steps.")
    elif not pack["motions"]:
        flash("Nothing taught yet: pick a session type and TTL on a signed-in account and generate once.")
    elif not sender:
        flash("Connect a Gmail inbox on Setup first; the steps are sent from it.")
    else:
        try:
            from .updater import installed_version
            send(pack, sender, accounts[sender], installed_version())
            flash(f"Shared {len(pack['motions'])} taught settings. Thanks! They'll help others in a coming update.")
        except Exception as e:
            flash(f"Couldn't send them: {logs.redact(str(e))}")
    return _back()


@bp.post("/proxies/sessions/import")
def import_():
    con = _con()
    f = request.files.get("pack")
    text = f.read().decode("utf-8", "replace") if f and f.filename else request.form.get("text", "")
    try:
        pack = decode(text)
        n = import_pack(con, pack, (f.filename if f and f.filename else "pasted") + " " + date.today().isoformat())
        flash(f"Added {n} shared setting{'s' if n != 1 else ''}.")
    except (ValueError, json.JSONDecodeError, OSError) as e:
        flash(f"Couldn't read that: {str(e)[:200]}")
    return _back()


@bp.get("/proxies/sessions/export.json")
def export():
    """Every shared and taught setting on this Mac, scrubbed: for the developer to ship as jigstream/shared/motions.json."""
    con = _con()
    merged = merge([{"motions": shared(con)}, build(con)])
    return Response(json.dumps(merged, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=motions.json"})
