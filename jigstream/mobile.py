"""Phone access: a phone-sized BotTrack at /m that people add to their home screen.

Phones reach the Mac over Tailscale (free, encrypted) or, if allowed, the same Wi-Fi. Every
request that doesn't come from BotTrack's own window must sign in with the email and password
set on the Setup page, and can only reach the /m pages: Setup, Logs and updates, which hold
inbox passwords and tokens, stay on the Mac. Card numbers are never stored, so they never
reach a phone either.
"""
import hmac
import ipaddress
import logging
import os
import re
import secrets
import socket
import subprocess
import time
from datetime import timedelta

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, updater
from .config import save_env

logger = logging.getLogger("jigstream.mobile")
bp = Blueprint("mobile", __name__, url_prefix="/m")

TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
FORWARDED = ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Tailscale-User-Login")
PUBLIC = {"mobile.login", "mobile.manifest", "static"}  # reachable before signing in
MIN_PASSWORD = 10
LOCK_AFTER, LOCK_WINDOW = 5, 15 * 60  # failed sign-ins per address, and for how long they count
# Pages a phone never opens, even signed in: they hold inbox passwords, API keys, bank links and logs,
# show full card numbers, or export cards. Everything else in BotTrack works from a phone.
PHONE_BLOCKED = ("/setup", "/logs", "/bank/plaid", "/update/rollback", "/profiles/export")
PHONE_BLOCKED_ENDPOINTS = {"cards_reveal"}
failures = {}  # ip -> [times of recent failed sign-ins]


def settings():
    env = os.environ.get
    return {"on": env("PHONE_ACCESS") == "1", "email": env("PHONE_EMAIL", ""),
            "password_hash": env("PHONE_PASSWORD_HASH", ""), "allow_lan": env("PHONE_ALLOW_LAN") == "1",
            "lockout": env("PHONE_LOCKOUT", "1") != "0"}


def from_this_mac():
    """BotTrack's own window: a direct loopback request for 127.0.0.1/localhost, not something a
    proxy on this Mac (like `tailscale serve`) passed along from another device."""
    host = request.host.rsplit(":", 1)[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
    return (_is_loopback(request.remote_addr) and host in LOCAL_HOSTS
            and not any(h in request.headers for h in FORWARDED))


def _is_loopback(addr):
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def network_allowed(addr, allow_lan):
    """Tailscale devices always; same-network devices only when allowed; never the open internet."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback or any(ip in net for net in TAILNET):
        return True  # loopback here means a proxy on this Mac, which already sits on the tailnet
    return allow_lan and ip.is_private and not ip.is_link_local


def _locked(ip):
    if not settings()["lockout"]:
        return False  # switched off on Setup, for testing
    now = time.time()
    failures[ip] = [t for t in failures.get(ip, []) if now - t < LOCK_WINDOW]
    return len(failures[ip]) >= LOCK_AFTER


def _session_version(password_hash):
    # Changing the password changes this, which signs every phone out.
    return hmac.new(current_app.secret_key.encode(), password_hash.encode(), "sha256").hexdigest()[:16]


def signed_in():
    s = settings()
    return bool(s["password_hash"]) and session.get("phone") == s["email"] \
        and session.get("phone_v") == _session_version(s["password_hash"])


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def token_ok():
    expected = session.get("csrf")
    sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")  # base.html adds both on phones
    return bool(expected) and hmac.compare_digest(sent, expected)


def init_app(app):
    """Guard every request and give the app a real secret key (sessions now leave the Mac)."""
    # Saved to .env when phone access is turned on, so phones stay signed in across restarts.
    app.secret_key = os.environ.get("PHONE_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.register_blueprint(bp)
    try:
        watch_changes(db.connect(app.config.get("DB_PATH")))
    except Exception:
        logger.exception("couldn't set up live updates")

    @app.context_processor
    def phone_flags():
        up, app_version = updater.state, android_app_version()
        return {"on_phone": not from_this_mac(), "update": up, "app_version": app_version,
                "apk_update": bool(app_version and up.get("apk_url") and up.get("apk_version", 0) > app_version)}

    @app.before_request
    def guard():
        if from_this_mac():
            return None
        s = settings()
        if not s["on"] or not network_allowed(request.remote_addr, s["allow_lan"]):
            logger.warning("blocked %s %s from %s", request.method, request.path, request.remote_addr)
            abort(404)
        if request.endpoint in PUBLIC:
            return None
        if not signed_in():
            return redirect(url_for("mobile.login"))
        if request.path.startswith(PHONE_BLOCKED) or request.endpoint in PHONE_BLOCKED_ENDPOINTS:
            logger.info("phone asked for Mac-only %s", request.path)
            if request.method == "GET" and request.endpoint not in PHONE_BLOCKED_ENDPOINTS:
                flash("That part of BotTrack only opens on your Mac.")
                return redirect("/")
            abort(403)
        if request.method == "POST" and not token_ok():
            abort(400)
        return None


# ---- live updates: every open page, on the Mac or a phone, reloads when the data changes ---------

WATCHED = ("tracked_orders", "expenses", "profiles")


def watch_changes(c):
    """SQLite triggers count every change to orders, expenses and profiles, whoever makes it: a phone,
    the Mac's own pages, or a background sync. Pages poll the count and refresh when it moves."""
    c.execute("CREATE TABLE IF NOT EXISTS change_count (id INTEGER PRIMARY KEY CHECK (id = 1), n INTEGER)")
    c.execute("INSERT OR IGNORE INTO change_count VALUES (1, 0)")
    for table in WATCHED:
        for op in ("INSERT", "UPDATE", "DELETE"):
            c.execute(f"CREATE TRIGGER IF NOT EXISTS count_{table}_{op.lower()} AFTER {op} ON {table} "
                      "BEGIN UPDATE change_count SET n = n + 1 WHERE id = 1; END")
    c.commit()


@bp.get("/api/changes")
def changes():
    row = con().execute("SELECT n FROM change_count WHERE id = 1").fetchone()
    return jsonify(n=row[0] if row else 0), 200, {"Cache-Control": "no-store"}


def android_app_version():
    """The Android app adds "BotTrackApp/<versionCode>" to its user agent."""
    m = re.search(r"BotTrackApp/(\d+)", request.headers.get("User-Agent", ""))
    return int(m.group(1)) if m else (1 if "BotTrackApp" in request.headers.get("User-Agent", "") else 0)


# ---- desktop Setup: turn phone access on and see how to reach it ---------------------------------

def addresses(port):
    """Where a phone can open BotTrack: this Mac's Tailscale address, and its Wi-Fi address."""
    tailscale, lan = [], []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    for ip in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out):
        addr = ipaddress.ip_address(ip)
        if any(addr in net for net in TAILNET):
            tailscale.append(ip)
        elif addr.is_private and not addr.is_loopback and not addr.is_link_local:
            lan.append(ip)
    if not lan:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outgoing address
                lan.append(s.getsockname()[0])
        except OSError:
            pass
    url = "http://{}:%s/m/" % port
    return [url.format(ip) for ip in tailscale], [url.format(ip) for ip in lan if ip != "127.0.0.1"]


def setup_context():
    s = settings()
    port = current_app.config.get("PORT") or os.environ.get("PORT", 8765)
    tailscale, lan = addresses(port) if s["on"] else ([], [])
    bound = current_app.config.get("BIND_HOST", "127.0.0.1")
    return {"phone": s, "phone_tailscale": tailscale, "phone_lan": lan,
            "phone_needs_restart": s["on"] != (bound != "127.0.0.1")}


def _local_form_only():
    """The Setup forms change who can sign in, so besides coming from this Mac they need the page's
    token: a web page open in a browser on this Mac can't post them on its own."""
    if not from_this_mac() or not token_ok():
        abort(404)


@bp.post("/setup")
def save_setup():
    _local_form_only()
    s = settings()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    on = bool(request.form.get("on"))
    updates = {"PHONE_ALLOW_LAN": "1" if request.form.get("allow_lan") else "0",
               "PHONE_LOCKOUT": "0" if request.form.get("no_lockout") else "1"}
    failures.clear()  # saving Setup also lifts any current lockout
    if on:
        if "@" not in email:
            flash("Enter the email you'll sign in with on your phone.")
            return redirect(url_for("setup") + "#phone")
        if password and len(password) < MIN_PASSWORD:
            flash(f"Use a password of at least {MIN_PASSWORD} characters.")
            return redirect(url_for("setup") + "#phone")
        if not password and not s["password_hash"]:
            flash("Choose a password for signing in on your phone.")
            return redirect(url_for("setup") + "#phone")
        updates["PHONE_EMAIL"] = email
        if not os.environ.get("PHONE_SECRET_KEY"):
            updates["PHONE_SECRET_KEY"] = current_app.secret_key
        if password:
            updates["PHONE_PASSWORD_HASH"] = generate_password_hash(password)
    updates["PHONE_ACCESS"] = "1" if on else "0"
    save_env(updates)
    logger.info("phone access %s (wifi %s)", "on" if on else "off", updates["PHONE_ALLOW_LAN"])
    flash("Phone access saved." + (" Signed out every phone, since the password changed." if password and s["password_hash"] else ""))
    return redirect(url_for("setup") + "#phone")


@bp.post("/setup/restart")
def restart():
    _local_form_only()
    updater.restart_soon()
    return render_template("restarting.html", what="Restarting BotTrack")


# ---- phone pages ---------------------------------------------------------------------------------

def con():
    return db.connect(current_app.config.get("DB_PATH"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    s = settings()
    if request.method == "GET":
        return redirect("/") if signed_in() or from_this_mac() else render_template("mobile/login.html")
    ip = request.remote_addr
    if _locked(ip):
        logger.warning("sign-in locked for %s", ip)
        flash("Too many tries. Wait 15 minutes and try again.")
        return render_template("mobile/login.html"), 429
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    ok = bool(s["password_hash"]) and hmac.compare_digest(email, s["email"]) and check_password_hash(s["password_hash"], password)
    if not ok:
        failures.setdefault(ip, []).append(time.time())
        logger.warning("failed phone sign-in from %s", ip)
        flash("That email and password don't match what's set in BotTrack on your Mac.")
        return render_template("mobile/login.html", email=email), 401
    failures.pop(ip, None)
    session.clear()
    session.permanent = True
    session.update(phone=s["email"], phone_v=_session_version(s["password_hash"]))
    logger.info("phone signed in from %s", ip)
    return redirect("/")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("mobile.login"))


@bp.get("/manifest.webmanifest")
def manifest():
    icon = url_for("static", filename="icon.png")
    return jsonify(name="BotTrack", short_name="BotTrack", start_url="/", scope="/",
                   display="standalone", background_color="#132048", theme_color="#0b1436",
                   icons=[{"src": icon, "sizes": "1024x1024", "type": "image/png", "purpose": "any"}]), \
        200, {"Content-Type": "application/manifest+json"}


@bp.get("/")
def home():
    return redirect("/")


@bp.get("/app")
def app_page():
    return render_template("mobile/app.html", version=updater.installed_version(),
                           can_update=updater.can_self_update())


@bp.post("/update/check")
def update_check():
    s = updater.check()
    flash(f"Couldn't check for updates: {s['error']}" if s["error"] else
          "A new version is ready." if s["available"] else "BotTrack is up to date.")
    return redirect(url_for("mobile.app_page"))


@bp.post("/update")
def update_install():
    """Update BotTrack on the Mac from the phone. The phone pages come from the Mac, so this updates
    them too; an Android app that's out of date is offered its new version once BotTrack is back."""
    if not updater.can_self_update():
        flash("This copy of BotTrack runs from a developer checkout, so it doesn't update itself.")
        return redirect(url_for("mobile.app_page"))
    if getattr(current_app, "sync_state", {}).get("running"):
        flash("BotTrack is syncing your inboxes. Try again when it finishes.")
        return redirect(url_for("mobile.app_page"))
    try:
        updater.install()
    except Exception as e:
        logger.exception("update from a phone failed")
        flash(f"The update failed and nothing was changed: {e}")
        return redirect(url_for("mobile.app_page"))
    logger.info("update started from a phone")
    updater.restart_soon()
    return render_template("mobile/updating.html")


# The earlier phone-only pages now open the real ones, which fit a phone screen.
OLD_PAGES = {"orders": "/orders", "expenses": "/expenses", "profiles": "/profiles"}


@bp.get("/<page>")
def old_page(page):
    if page not in OLD_PAGES:
        abort(404)
    return redirect(OLD_PAGES[page])
