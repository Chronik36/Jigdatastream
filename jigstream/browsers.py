"""Browsers: Chrome or Chromium windows, each going through the proxy you pick, lined up like tasks.

Every browser has its own profile folder, so cookies and sign-ins never mix between them. With "Save session" on,
that folder is kept on this computer and the browser opens again signed in where you left it; with it off, a fresh
folder is used each time and deleted when the window closes. WebRTC is told to use the proxy only, so it can't show
your home IP.

Chrome can't sign in to a proxy by itself (no user:pass in --proxy-server), so each window talks to a small relay
on this computer (127.0.0.1) that adds the proxy's login and passes everything on to it. Only HTTP proxies.
"""
import base64
import json
import logging
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import db, proxies, storeaccounts

logger = logging.getLogger("jigstream.browsers")
bp = Blueprint("browsers", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS browsers (
    id INTEGER PRIMARY KEY, name TEXT, proxy_id INTEGER, account_id INTEGER, start_url TEXT,
    keep_session INTEGER DEFAULT 1, last_opened TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS browser_settings (key TEXT PRIMARY KEY, value TEXT);
"""
# Where Chrome-family browsers live; the first one found is used.
CANDIDATES = {
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    "win32": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
              r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
              r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
              r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
              r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"],
    "linux": ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"],
}
SITE_URLS = {"Target": "https://www.target.com", "Walmart": "https://www.walmart.com", "Best Buy": "https://www.bestbuy.com",
             "Pokemon Center": "https://www.pokemoncenter.com", "Amazon": "https://www.amazon.com",
             "GameStop": "https://www.gamestop.com", "Sam's Club": "https://www.samsclub.com",
             "Costco": "https://www.costco.com", "Nike": "https://www.nike.com", "Topps": "https://www.topps.com",
             "Fanatics": "https://www.fanatics.com", "Riot": "https://www.riotgames.com"}
running = {}  # browser id -> {"proc", "relay", "dir", "keep"}
_lock = threading.Lock()


def ensure(con):
    con.executescript(SCHEMA)
    return con


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def app_path(con):
    row = ensure(con).execute("SELECT value FROM browser_settings WHERE key = 'app'").fetchone()
    return row["value"] if row else None


def _program(path):
    """A Mac .app folder -> the program inside it (Contents/MacOS/<name>)."""
    p = Path(path)
    if p.suffix == ".app" and p.is_dir():
        inside = sorted((p / "Contents" / "MacOS").glob("*"))
        return str(inside[0]) if inside else None
    return str(p) if p.is_file() else None


def find_chrome(con=None):
    """The browser picked under Browser to use, else the first Chrome-family browser installed."""
    key = "darwin" if sys.platform == "darwin" else ("win32" if sys.platform == "win32" else "linux")
    picked = app_path(con) if con is not None else None
    for path in ([picked] if picked else []) + CANDIDATES[key]:
        found = _program(os.path.expandvars(path)) if path else None
        if found:
            return found
    return None


def sessions_dir(db_path):
    return Path(db_path).resolve().parent / "browser-sessions"


# ---------- the relay: a local proxy that signs in to the real one ----------

class Relay:
    """Listens on 127.0.0.1 and passes every request to the upstream proxy with its login added."""

    def __init__(self, p):
        self.up = (p["host"], int(p["port"]))
        self.auth = ("Proxy-Authorization: Basic " + base64.b64encode(
            f"{p['user']}:{p['password'] or ''}".encode()).decode() + "\r\n").encode() if p.get("user") else b""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.open = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while self.open:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        upstream = None
        try:
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            first, _, rest = head.partition(b"\r\n")
            if not first.upper().startswith(b"CONNECT"):
                # Plain http: one request per connection, so each one carries the login.
                lines, _, body = rest.partition(b"\r\n\r\n")
                keep = [h for h in lines.split(b"\r\n") if h and not h.lower().startswith((b"connection:", b"proxy-connection:"))]
                rest = b"\r\n".join(keep + [b"Connection: close"]) + b"\r\n\r\n" + body
            upstream = socket.create_connection(self.up, timeout=20)
            upstream.sendall(first + b"\r\n" + self.auth + rest)  # the browser's request, with the login added
            self._pipe(client, upstream)
        except OSError:
            pass
        finally:
            for s in (client, upstream):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass

    @staticmethod
    def _pipe(a, b):
        a.settimeout(None)
        b.settimeout(None)
        while True:
            ready, _, _ = select.select([a, b], [], [], 300)
            if not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)

    def close(self):
        self.open = False
        try:
            self.sock.close()
        except OSError:
            pass


# ---------- browsers ----------

def rows(con):
    ensure(con)
    proxies.ensure(con)
    storeaccounts.ensure(con)
    out = []
    for r in con.execute("SELECT b.*, p.host, p.port, p.user, p.grp, p.scheme, a.site, a.email, a.username"
                         " FROM browsers b LEFT JOIN proxies p ON p.id = b.proxy_id"
                         " LEFT JOIN store_accounts a ON a.id = b.account_id ORDER BY b.id"):
        d = dict(r)
        d["proxy"] = proxies.masked(d) if d["host"] else None
        d["running"] = is_running(d["id"])
        out.append(d)
    return out


def is_running(bid):
    with _lock:
        r = running.get(bid)
    return bool(r and r["proc"].poll() is None)


def _cleanup(bid):
    with _lock:
        r = running.pop(bid, None)
    if not r:
        return
    r["relay"] and r["relay"].close()
    if not r["keep"]:
        shutil.rmtree(r["dir"], ignore_errors=True)


def _watch(bid, proc):
    proc.wait()
    with _lock:
        same = running.get(bid, {}).get("proc") is proc
    if same:
        _cleanup(bid)
        logger.info("browser %s closed", bid)


def launch(con, bid, db_path, popen=subprocess.Popen):
    """Open one browser. Returns None when it opened, else why it couldn't."""
    b = ensure(con).execute("SELECT * FROM browsers WHERE id = ?", (bid,)).fetchone()
    if not b:
        return "That browser is gone."
    if is_running(bid):
        return None
    chrome = find_chrome(con)
    if not chrome:
        return "No Chrome, Chromium, Brave or Edge found on this computer. Install Google Chrome and try again."
    p = con.execute("SELECT * FROM proxies WHERE id = ?", (b["proxy_id"],)).fetchone() if b["proxy_id"] else None
    if p and (p["scheme"] or "http").startswith("socks"):
        return "SOCKS proxies can't be used here yet, only HTTP ones."
    keep = bool(b["keep_session"])
    folder = (sessions_dir(db_path) / str(bid)) if keep else Path(tempfile.mkdtemp(prefix="bottrack-browser-"))
    folder.mkdir(parents=True, exist_ok=True)
    relay = Relay(dict(p)) if p else None
    url = b["start_url"]
    if not url and b["account_id"]:
        a = con.execute("SELECT site FROM store_accounts WHERE id = ?", (b["account_id"],)).fetchone()
        url = SITE_URLS.get(a["site"]) if a else None
    args = [chrome, f"--user-data-dir={folder}", "--no-first-run", "--no-default-browser-check", "--new-window",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    if relay:
        args += [f"--proxy-server=http://127.0.0.1:{relay.port}", "--proxy-bypass-list=<-loopback>"]
    args.append(url or "about:blank")
    try:
        proc = popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        relay and relay.close()
        return f"Couldn't open the browser: {e}"
    with _lock:
        running[bid] = {"proc": proc, "relay": relay, "dir": folder, "keep": keep}
    threading.Thread(target=_watch, args=(bid, proc), daemon=True).start()
    con.execute("UPDATE browsers SET last_opened = ? WHERE id = ?", (_now(), bid))
    con.commit()
    logger.info("browser %s opened (proxy: %s, saved session: %s)", bid, bool(relay), keep)
    return None


def close(bid):
    with _lock:
        r = running.get(bid)
    if r and r["proc"].poll() is None:
        r["proc"].terminate()
        try:
            r["proc"].wait(5)
        except subprocess.TimeoutExpired:
            r["proc"].kill()
    _cleanup(bid)


def clear_session(bid, db_path):
    close(bid)
    shutil.rmtree(sessions_dir(db_path) / str(bid), ignore_errors=True)


def add(con, count=1, name=None, proxy_id=None, group=None, account_id=None, start_url=None, keep=True):
    """Add browsers. With a proxy group, each new browser takes the next proxy from it (the least used first)."""
    ensure(con)
    pool = []
    if group:
        used = dict(con.execute("SELECT proxy_id, COUNT(*) FROM browsers GROUP BY proxy_id").fetchall())
        pool = sorted((r["id"] for r in con.execute("SELECT id FROM proxies WHERE COALESCE(grp, '') = ?",
                                                     ("" if group == "No group" else group,))),
                      key=lambda i: (used.get(i, 0), i))
    start = con.execute("SELECT COUNT(*) FROM browsers").fetchone()[0]
    ids = []
    for n in range(max(1, min(int(count or 1), 50))):
        pid = pool[n % len(pool)] if pool else proxy_id
        label = name if name and count == 1 else f"{name or 'Browser'} {start + n + 1}"
        ids.append(con.execute("INSERT INTO browsers (name, proxy_id, account_id, start_url, keep_session, created_at)"
                               " VALUES (?,?,?,?,?,?)", (label, pid, account_id, start_url, 1 if keep else 0,
                                                         _now())).lastrowid)
    con.commit()
    return ids


# ---------- pages ----------

def _con():
    return db.connect(current_app.config["DB_PATH"])


def _back():
    return redirect(request.form.get("back") or request.referrer or url_for("browsers.page"))


def _int(name):
    v = request.form.get(name, "")
    return int(v) if v.isdigit() else None


@bp.get("/browsers")
def page():
    c = _con()
    items = rows(c)
    return render_template("browsers.html", browsers=items, chrome=find_chrome(c), app_path=app_path(c),
                           open_id=request.args.get("open", type=int),
                           groups=[(g["name"], g["proxies"]) for g in proxies.groups(c)],
                           accounts=storeaccounts.rows(c), open_n=sum(b["running"] for b in items))


@bp.post("/browsers/add")
def add_page():
    c = _con()
    ids = add(c, _int("count") or 1, (request.form.get("name") or "").strip() or None, _int("proxy_id"),
              request.form.get("group") or None, _int("account_id"), (request.form.get("start_url") or "").strip() or None,
              request.form.get("keep") == "1")
    if request.form.get("open") == "1" and ids:  # the phone's "Add and open": the app opens it on arrival
        return redirect(url_for("browsers.page", open=ids[0]))
    flash(f"Added {len(ids)} browser{'' if len(ids) == 1 else 's'}.")
    return _back()


@bp.get("/browsers/<int:bid>/config.json")
def config(bid):
    """What the Android app needs to open a browser itself: its proxy (with the login, as the app signs in to the
    proxy), where to start, and whether to keep the session. Account passwords are never included."""
    c = _con()
    b = next((r for r in rows(c) if r["id"] == bid), None)
    if not b:
        return {"error": "That browser is gone."}, 404
    p = c.execute("SELECT * FROM proxies WHERE id = ?", (b["proxy_id"],)).fetchone() if b["proxy_id"] else None
    if p and (p["scheme"] or "http").startswith("socks"):
        return {"error": "SOCKS proxies can't be used here yet, only HTTP ones."}, 400
    url = b["start_url"] or SITE_URLS.get(b["site"] or "") or "https://www.google.com"
    c.execute("UPDATE browsers SET last_opened = ? WHERE id = ?", (_now(), bid))
    c.commit()
    logger.info("browser %s opened on the phone (proxy: %s)", bid, bool(p))
    return {"id": bid, "name": b["name"], "url": url, "keep": bool(b["keep_session"]),
            "proxy": {"host": p["host"], "port": p["port"], "user": p["user"] or "", "password": p["password"] or "",
                      "label": b["proxy"], "group": b["grp"] or "",
                      "place": (json.loads(p["exit_geo"]).get("city") if p["exit_geo"] else "") or ""} if p else None,
            "account": {"site": b["site"], "email": b["email"], "username": b["username"]} if b["email"] else None}


@bp.post("/browsers/<int:bid>/edit")
def edit(bid):
    c = ensure(_con())
    c.execute("UPDATE browsers SET name = ?, proxy_id = ?, account_id = ?, start_url = ?, keep_session = ? WHERE id = ?",
              ((request.form.get("name") or "").strip() or f"Browser {bid}", _int("proxy_id"), _int("account_id"),
               (request.form.get("start_url") or "").strip() or None, 1 if request.form.get("keep") == "1" else 0, bid))
    c.commit()
    flash("Saved. A browser that's open uses the change the next time it opens.")
    return _back()


@bp.post("/browsers/app")
def set_app():
    c = ensure(_con())
    path = (request.form.get("path") or "").strip().strip('"')
    if path and not _program(os.path.expanduser(path)):
        flash("No browser program at that path.")
        return _back()
    c.execute("DELETE FROM browser_settings WHERE key = 'app'")
    if path:
        c.execute("INSERT INTO browser_settings VALUES ('app', ?)", (os.path.expanduser(path),))
    c.commit()
    flash("Browser saved." if path else "BotTrack finds Chrome by itself again.")
    return _back()


@bp.post("/browsers/<int:bid>/open")
def open_one(bid):
    flash(launch(_con(), bid, current_app.config["DB_PATH"]) or "Opened.")
    return _back()


@bp.post("/browsers/<int:bid>/close")
def close_one(bid):
    close(bid)
    row = ensure(_con()).execute("SELECT keep_session FROM browsers WHERE id = ?", (bid,)).fetchone()
    flash("Closed." + (" Its session was thrown away." if row and not row[0] else ""))
    return _back()


@bp.post("/browsers/<int:bid>/keep")
def keep(bid):
    c = ensure(_con())
    on = request.form.get("keep") == "1"
    c.execute("UPDATE browsers SET keep_session = ? WHERE id = ?", (1 if on else 0, bid))
    c.commit()
    return _back()


@bp.post("/browsers/run")
def run():
    """Open, close, clear the saved session of, or delete the ticked browsers (or one, from its row)."""
    c = _con()
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    action = request.form.get("action", "")
    if action == "open_all":
        action, ids = "open", [r[0] for r in ensure(c).execute("SELECT id FROM browsers")]
    if not ids:
        flash("Tick one or more browsers first.")
        return _back()
    path = current_app.config["DB_PATH"]
    if action == "open":
        problems = [p for p in (launch(c, i, path) for i in ids) if p]
        flash(problems[0] if problems else f"Opened {len(ids)} browser{'' if len(ids) == 1 else 's'}.")
    elif action == "close":
        for i in ids:
            close(i)
        flash(f"Closed {len(ids)}.")
    elif action == "clear":
        for i in ids:
            clear_session(i, path)
        flash("Saved sessions cleared: they open signed out next time.")
    elif action == "delete":
        for i in ids:
            clear_session(i, path)
        c.executemany("DELETE FROM browsers WHERE id = ?", [(i,) for i in ids])
        c.commit()
        flash(f"Deleted {len(ids)}.")
    return _back()


@bp.post("/accounts/<int:account_id>/browser")
def for_account(account_id):
    """The Open button on an account: its own browser (made the first time, with the proxy you pick), opened."""
    c = _con()
    b = ensure(c).execute("SELECT id FROM browsers WHERE account_id = ? ORDER BY id LIMIT 1", (account_id,)).fetchone()
    if b:
        bid = b["id"]
        if _int("proxy_id"):
            c.execute("UPDATE browsers SET proxy_id = ? WHERE id = ?", (_int("proxy_id"), bid))
            c.commit()
    else:
        a = storeaccounts.ensure(c).execute("SELECT site, email FROM store_accounts WHERE id = ?", (account_id,)).fetchone()
        if not a:
            return _back()
        bid = add(c, 1, f"{a['site']} · {a['email']}", _int("proxy_id"), account_id=account_id)[0]
    problem = launch(c, bid, current_app.config["DB_PATH"])
    flash(problem or "Opened. Its session is saved, so it stays signed in next time.")
    return _back()
