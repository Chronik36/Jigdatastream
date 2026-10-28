"""Phone access: the BotTrack pages on a phone, in the Android app or added to the home screen.

Phones reach the Mac through its phone link (a free Cloudflare tunnel, see jigstream/tunnel.py)
or, if allowed, the same Wi-Fi. Every request that doesn't come from BotTrack's own window must
be from a paired phone: one that scanned the QR code on the Setup page, or signed in with the
optional email and password. Setup, Logs, bank linking and full card numbers stay on the Mac.
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
from urllib.parse import quote, urlparse

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, tunnel, updater
from .config import save_env

logger = logging.getLogger("jigstream.mobile")
bp = Blueprint("mobile", __name__, url_prefix="/m")

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
FORWARDED = ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Cf-Connecting-Ip")
PUBLIC = {"mobile.login", "mobile.pair", "mobile.manifest", "static"}  # reachable before pairing
MIN_PASSWORD = 10
LOCK_AFTER, LOCK_WINDOW = 5, 15 * 60  # failed sign-ins per address, and for how long they count
# Pages a phone never opens, even signed in: they hold inbox passwords, API keys, bank links and logs,
# show full card numbers, or export cards. Everything else in BotTrack works from a phone.
PHONE_BLOCKED = ("/setup", "/logs", "/bank/plaid", "/update/rollback", "/profiles/export", "/m/api/link")
PHONE_BLOCKED_ENDPOINTS = {"cards_reveal", "storeaccounts.reveal"}
# Inside a blocked path but fine from a phone: sending the (scrubbed) logs, without browsing them.
PHONE_ALLOWED = {"send_logs_page", "send_logs"}
failures = {}  # ip -> [times of recent failed sign-ins]


def settings():
    env = os.environ.get
    return {"on": env("PHONE_ACCESS") == "1", "email": env("PHONE_EMAIL", ""),
            "password_hash": env("PHONE_PASSWORD_HASH", ""), "allow_lan": env("PHONE_ALLOW_LAN") == "1",
            "lockout": env("PHONE_LOCKOUT", "1") != "0", "pair_key": env("PHONE_PAIR_KEY", ""),
            "pair_topic": env("PHONE_PAIR_TOPIC", "")}


def from_this_mac():
    """BotTrack's own window: a direct loopback request for 127.0.0.1/localhost, not something a
    proxy on this Mac (like the phone link's cloudflared) passed along from another device."""
    host = request.host.rsplit(":", 1)[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
    return (_is_loopback(request.remote_addr) and host in LOCAL_HOSTS
            and not any(h in request.headers for h in FORWARDED))


def _is_loopback(addr):
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def network_allowed(addr, allow_lan):
    """The phone link always; same-network devices only when allowed; nothing else."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback:
        return True  # the phone link (cloudflared on this Mac) passing a phone's request along
    return allow_lan and ip.is_private and not ip.is_link_local


def via_link():
    return _is_loopback(request.remote_addr) and not from_this_mac()


def client_ip():
    """The phone's own address. Everything through the phone link arrives from 127.0.0.1, so
    lockouts go by the address Cloudflare saw instead."""
    if via_link() and request.headers.get("Cf-Connecting-Ip"):
        return request.headers["Cf-Connecting-Ip"]
    return request.remote_addr


def _locked(ip):
    if not settings()["lockout"] and not via_link():
        return False  # switched off on Setup for testing on Wi-Fi; never for the internet-facing link
    now = time.time()
    failures[ip] = [t for t in failures.get(ip, []) if now - t < LOCK_WINDOW]
    return len(failures[ip]) >= LOCK_AFTER


def _session_version(s):
    # Changing the password, or Forget all phones (a new pairing key), changes this and signs every phone out.
    return hmac.new(current_app.secret_key.encode(), f"{s['password_hash']}|{s['pair_key']}".encode(),
                    "sha256").hexdigest()[:16]


def signed_in():
    s = settings()
    return bool(s["password_hash"] or s["pair_key"]) and bool(session.get("phone")) \
        and session.get("phone_v") == _session_version(s)


def _sign_in(s, who):
    session.clear()
    session.permanent = True
    session.update(phone=who, phone_v=_session_version(s))


def new_pairing():
    """A fresh QR code: a new key that pairs phones, and a new private topic for the link address."""
    return {"PHONE_PAIR_KEY": secrets.token_urlsafe(24), "PHONE_PAIR_TOPIC": "bottrack-" + secrets.token_hex(16)}


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
                      PERMANENT_SESSION_LIFETIME=timedelta(days=365))
    app.jinja_env.globals["csrf_token"] = csrf_token
    if os.environ.get("PHONE_ACCESS") == "1" and not os.environ.get("PHONE_PAIR_KEY"):
        save_env(new_pairing())  # phone access turned on before pairing by QR code existed
    app.register_blueprint(bp)
    app.after_request(secure_headers)
    try:
        watch_changes(db.connect(app.config.get("DB_PATH")))
    except Exception:
        logger.exception("couldn't set up live updates")

    @app.context_processor
    def phone_flags():
        up, app_version = updater.state, android_app_version()
        return {"on_phone": not from_this_mac(), "update": up, "app_version": app_version,
                "phone_password": bool(os.environ.get("PHONE_PASSWORD_HASH")),
                "apk_update": bool(app_version and up.get("apk_url") and up.get("apk_version", 0) > app_version),
                # Version 5 and later download and install their own update; older ones get the file in the browser.
                "apk_install_url": ("bottrack://update?url=" + quote(up.get("apk_url") or "", safe="")
                                    if app_version >= 5 else up.get("apk_url") or "")}

    @app.before_request
    def guard():
        if from_this_mac():
            if request.method not in ("GET", "HEAD", "OPTIONS") and not same_site_post():
                logger.warning("blocked a %s to %s sent by another website (%s)", request.method, request.path,
                               request.headers.get("Origin") or request.headers.get("Sec-Fetch-Site"))
                abort(403)
            return None
        if request.path.startswith("/sync/peer/") and _is_loopback(request.remote_addr):
            return None  # a linked computer, through the link; each request is sealed with that computer's key
        s = settings()
        if not s["on"] or not network_allowed(request.remote_addr, s["allow_lan"]):
            logger.warning("blocked %s %s from %s", request.method, request.path, request.remote_addr)
            abort(404)
        if request.endpoint in PUBLIC:
            return None
        if not signed_in():
            return redirect(url_for("mobile.login"))
        if (request.path.startswith(PHONE_BLOCKED) and request.endpoint not in PHONE_ALLOWED) \
                or request.endpoint in PHONE_BLOCKED_ENDPOINTS:
            logger.info("phone asked for Mac-only %s", request.path)
            if request.method == "GET" and request.endpoint not in PHONE_BLOCKED_ENDPOINTS:
                flash("That part of BotTrack only opens on your Mac.")
                return redirect("/")
            abort(403)
        if request.method == "POST" and not token_ok():
            abort(400)
        return None


def same_site_post():
    """A web page you visit in your browser can't make BotTrack act for it: the browser can reach 127.0.0.1 too,
    so a change from the Mac only counts when it came from BotTrack's own pages (or a local program, which sends
    neither header)."""
    origin = request.headers.get("Origin")
    if origin is None:
        return request.headers.get("Sec-Fetch-Site") != "cross-site"
    return (urlparse(origin).hostname or "") in LOCAL_HOSTS


def back_url():
    """The page this request came from on BotTrack itself (path and query), or None. Compared by host only, since
    through the phone link the page is https while BotTrack itself sees http."""
    from urllib.parse import urlparse
    ref = urlparse(request.referrer or "")
    if not ref.path or (ref.netloc and ref.netloc != request.host):
        return None
    return ref.path + (f"?{ref.query}" if ref.query else "")


def secure_headers(resp):
    """No other site may show BotTrack inside a frame, guess file types, or see BotTrack's links in referrers."""
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


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


# A new value every time BotTrack starts, so a phone waiting on an update knows the Mac has restarted.
BOOT = secrets.token_hex(6)


@bp.get("/api/changes")
def changes():
    row = con().execute("SELECT n FROM change_count WHERE id = 1").fetchone()
    return jsonify(n=row[0] if row else 0, boot=BOOT), 200, {"Cache-Control": "no-store"}


def android_app_version():
    """The Android app adds "BotTrackApp/<versionCode>" to its user agent."""
    m = re.search(r"BotTrackApp/(\d+)", request.headers.get("User-Agent", ""))
    return int(m.group(1)) if m else (1 if "BotTrackApp" in request.headers.get("User-Agent", "") else 0)


# ---- desktop Setup: turn phone access on and see how to reach it ---------------------------------

def wifi_addresses(port):
    """Where a phone on the same Wi-Fi can open BotTrack, when that's allowed."""
    found = []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    for ip in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out):
        addr = ipaddress.ip_address(ip)
        if addr.is_private and not addr.is_loopback and not addr.is_link_local:
            found.append(ip)
    if not found:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outgoing address
                found.append(sock.getsockname()[0])
        except OSError:
            pass
    return ["http://%s:%s/" % (ip, port) for ip in found if ip != "127.0.0.1"]


def link_info():
    """The phone link's state, and what the QR code on Setup holds: the link plus the pairing key and
    the private topic the Android app checks when the link changes."""
    s, t = settings(), tunnel.state
    pair = f"{t['url']}/m/pair?k={s['pair_key']}&t={s['pair_topic']}" if s["on"] and t["url"] and s["pair_key"] else ""
    return {"status": t["status"] if s["on"] else "off", "url": t["url"], "error": t["error"], "pair_url": pair}


def setup_context():
    s = settings()
    port = current_app.config.get("PORT") or os.environ.get("PORT", 8765)
    bound = current_app.config.get("BIND_HOST", "127.0.0.1")
    return {"phone": s, "phone_link": link_info(), "phone_lan": wifi_addresses(port) if s["on"] and s["allow_lan"] else [],
            "phone_needs_restart": (s["on"] and s["allow_lan"]) != (bound != "127.0.0.1")}


def _link_on(on):
    """Start or stop the phone link right away (only in the Mac app, which sets PHONE_LINK)."""
    if not current_app.config.get("PHONE_LINK"):
        return
    from . import peersync
    if on:
        tunnel.ensure(current_app.config.get("PORT") or 8765)
    elif not (os.environ.get("SYNC_LINK") == "1" and peersync.link_wanted(current_app.config.get("DB_PATH"))):
        tunnel.stop()  # still needed by a linked computer otherwise


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
        # The email and password are optional: scanning the QR code is how phones normally pair.
        if (password or email) and "@" not in email:
            flash("Enter the email you'll sign in with, or leave both blank and pair by QR code.")
            return redirect(url_for("setup") + "#phone")
        if password and len(password) < MIN_PASSWORD:
            flash(f"Use a password of at least {MIN_PASSWORD} characters.")
            return redirect(url_for("setup") + "#phone")
        updates["PHONE_EMAIL"] = email
        if not email:
            updates["PHONE_PASSWORD_HASH"] = ""
        if not os.environ.get("PHONE_SECRET_KEY"):
            updates["PHONE_SECRET_KEY"] = current_app.secret_key
        if not s["pair_key"] or not s["pair_topic"]:
            updates.update(new_pairing())
        if password:
            updates["PHONE_PASSWORD_HASH"] = generate_password_hash(password)
    updates["PHONE_ACCESS"] = "1" if on else "0"
    save_env(updates)
    _link_on(on)
    logger.info("phone access %s (wifi %s)", "on" if on else "off", updates["PHONE_ALLOW_LAN"])
    flash("Phone access saved." + (" Signed out every phone, since the password changed." if password and s["password_hash"] else ""))
    return redirect(url_for("setup") + "#phone")


@bp.post("/setup/forget")
def forget_phones():
    """A new QR code: every paired phone is signed out and has to scan again."""
    _local_form_only()
    save_env(new_pairing())
    if tunnel.state["url"]:
        tunnel.announce(tunnel.state["url"])  # to the new topic
    logger.info("forgot all phones")
    flash("Every phone is signed out. Scan the new QR code to pair a phone again.")
    return redirect(url_for("setup") + "#phone")


@bp.get("/api/link")
def link_status():
    """Setup polls this until the phone link is up, then shows its QR code."""
    if not from_this_mac():
        abort(404)
    return jsonify(link_info()), 200, {"Cache-Control": "no-store"}


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
    ip = client_ip()
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
    _sign_in(s, s["email"])
    logger.info("phone signed in from %s", ip)
    return redirect("/")


@bp.get("/pair")
def pair():
    """Opened by scanning the QR code on the Mac's Setup page: pairs this phone with no typing."""
    s, ip = settings(), client_ip()
    if _locked(ip):
        logger.warning("pairing locked for %s", ip)
        flash("Too many tries. Wait 15 minutes and try again.")
        return render_template("mobile/login.html"), 429
    key = request.args.get("k", "")
    if not (s["pair_key"] and hmac.compare_digest(key, s["pair_key"])):
        failures.setdefault(ip, []).append(time.time())
        logger.warning("old or wrong pairing code from %s", ip)
        flash("That QR code is out of date. Scan the one on your Mac's Setup page, Phone access.")
        return render_template("mobile/login.html"), 401
    if not signed_in():
        _sign_in(s, "paired")
        logger.info("phone paired from %s", ip)
    return redirect("/")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("mobile.login"))


@bp.get("/manifest.webmanifest")
def manifest():
    icon = url_for("static", filename="icon.png")
    # On an iPhone the home screen app keeps its own cookies, so it opens through the pairing link.
    s = settings()
    start = f"/m/pair?k={s['pair_key']}" if signed_in() and s["pair_key"] else "/"
    light = os.environ.get("UI_THEME") == "light"
    return jsonify(name="BotTrack", short_name="BotTrack", start_url=start, scope="/",
                   display="standalone", background_color="#f3f6fc" if light else "#132048",
                   theme_color="#ffffff" if light else "#0b1436",
                   icons=[{"src": icon, "sizes": "1024x1024", "type": "image/png", "purpose": "any"}]), \
        200, {"Content-Type": "application/manifest+json"}


@bp.get("/")
def home():
    return redirect("/")


@bp.get("/app")
def app_page():
    from . import notify
    return render_template("mobile/app.html", version=updater.installed_version(),
                           can_update=updater.can_self_update(), alerts_off=notify.why_off())


@bp.post("/alerts/test")
def alerts_test():
    from . import notify
    problem = notify.test()
    flash(problem or "Test alert sent. It should show on this phone within a few seconds. If it doesn't, see the steps under Shipping alerts.")
    return redirect(url_for("mobile.app_page") + "#alerts")


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
    if hasattr(current_app, "pause_sync_for_update"):
        current_app.pause_sync_for_update()  # a running sync carries on after the restart
    try:
        updater.install()
    except Exception as e:
        logger.exception("update from a phone failed")
        if hasattr(current_app, "start_sync"):
            current_app.start_sync()
        flash(f"The update failed and nothing was changed: {e}")
        return redirect(url_for("mobile.app_page"))
    logger.info("update started from a phone")
    updater.restart_soon()
    return render_template("mobile/updating.html", boot=BOOT)


# The earlier phone-only pages now open the real ones, which fit a phone screen.
OLD_PAGES = {"orders": "/orders", "expenses": "/expenses", "profiles": "/profiles"}


@bp.get("/<page>")
def old_page(page):
    if page not in OLD_PAGES:
        abort(404)
    return redirect(OLD_PAGES[page])
