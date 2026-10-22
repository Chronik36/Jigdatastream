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

from . import db, export, updater
from .config import save_env
from .extract import BILLING_PERIODS, EXPENSE_CATEGORIES
from .orderstore import Store

logger = logging.getLogger("jigstream.mobile")
bp = Blueprint("mobile", __name__, url_prefix="/m")

TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
FORWARDED = ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Tailscale-User-Login")
PUBLIC = {"mobile.login", "mobile.manifest", "static"}  # reachable before signing in
MIN_PASSWORD = 10
LOCK_AFTER, LOCK_WINDOW = 5, 15 * 60  # failed sign-ins per address, and for how long they count
STATUS_CHOICES = ["ordered", "processing", "shipped", "in_transit", "out_for_delivery", "delivered",
                  "exception", "cancelled", "returned", "refunded"]
PROFILE_FIELDS = ("name", "bot", "first_name", "last_name", "phone", "email",
                  "ship_address1", "ship_address2", "ship_city", "ship_state", "ship_zip", "ship_country",
                  "bill_address1", "bill_address2", "bill_city", "bill_state", "bill_zip", "bill_country")
failures = {}  # ip -> [times of recent failed sign-ins]


def settings():
    env = os.environ.get
    return {"on": env("PHONE_ACCESS") == "1", "email": env("PHONE_EMAIL", ""),
            "password_hash": env("PHONE_PASSWORD_HASH", ""), "allow_lan": env("PHONE_ALLOW_LAN") == "1"}


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
    return bool(expected) and hmac.compare_digest(request.form.get("csrf", ""), expected)


def init_app(app):
    """Guard every request and give the app a real secret key (sessions now leave the Mac)."""
    # Saved to .env when phone access is turned on, so phones stay signed in across restarts.
    app.secret_key = os.environ.get("PHONE_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.register_blueprint(bp)

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
        if not request.path.startswith("/m/"):
            return redirect(url_for("mobile.orders"))  # desktop pages stay on the Mac
        if request.method == "POST" and not token_ok():
            abort(400)
        return None


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
    updates = {"PHONE_ALLOW_LAN": "1" if request.form.get("allow_lan") else "0"}
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
        return redirect(url_for("mobile.orders")) if signed_in() or from_this_mac() else render_template("mobile/login.html")
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
    return redirect(url_for("mobile.orders"))


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("mobile.login"))


@bp.get("/manifest.webmanifest")
def manifest():
    icon = url_for("static", filename="icon.png")
    return jsonify(name="BotTrack", short_name="BotTrack", start_url=url_for("mobile.orders"), scope="/m/",
                   display="standalone", background_color="#132048", theme_color="#0b1436",
                   icons=[{"src": icon, "sizes": "1024x1024", "type": "image/png", "purpose": "any"}]), \
        200, {"Content-Type": "application/manifest+json"}


@bp.get("/")
def home():
    return redirect(url_for("mobile.orders"))


def _text(name):
    return request.form.get(name, "").strip() or None


def _amount(name):
    raw = (request.form.get(name) or "").replace("$", "").replace(",", "").strip()
    try:
        return round(float(raw), 2) if raw else None
    except ValueError:
        return None


def _items(text):
    """One item per line; a trailing "x2" is the quantity."""
    items = []
    for line in (text or "").splitlines():
        m = re.match(r"^(.*?)(?:\s+x\s*(\d+))?\s*$", line.strip(), re.I)
        if m and m.group(1):
            items.append({"name": m.group(1), "quantity": int(m.group(2) or 1)})
    return items


def _order_form():
    status = request.form.get("shipping_status", "")
    return {"retailer": _text("retailer"), "order_number": _text("order_number"), "order_date": _text("order_date"),
            "items": _items(request.form.get("items")), "total": _amount("total"),
            "currency": (_text("currency") or "USD").upper()[:3], "shipping_address": _text("shipping_address"),
            "tracking_number": _text("tracking_number"), "carrier": _text("carrier"),
            "estimated_delivery": _text("estimated_delivery"),
            "shipping_status": status if status in STATUS_CHOICES else None}


@bp.get("/orders")
def orders():
    rows = Store(con()).orders()
    q, status = request.args.get("q", "").strip().lower(), request.args.get("status", "")
    groups = {"in_transit": {"shipped", "in_transit", "out_for_delivery"}}
    if status:
        rows = [o for o in rows if (o["shipping_status"] or "unknown") in groups.get(status, {status})]
    if q:
        rows = [o for o in rows if q in " ".join(str(o.get(k) or "") for k in
                ("retailer", "order_number", "tracking_number", "shipping_address", "carrier")).lower()
                or any(q in (i.get("name") or "").lower() for i in o["items"])]
    return render_template("mobile/orders.html", rows=rows[:300], q=q, status=status, tab="orders")


@bp.route("/orders/new", methods=["GET", "POST"])
def order_new():
    if request.method == "GET":
        return render_template("mobile/order_edit.html", o=None, statuses=STATUS_CHOICES, tab="orders")
    f = _order_form()
    if not f["retailer"]:
        flash("Enter the store.")
        return render_template("mobile/order_edit.html", o=f, statuses=STATUS_CHOICES, tab="orders"), 400
    order_id = Store(con()).add_order(f)
    if not order_id:
        flash(f"There's already a {f['retailer']} order {f['order_number']}.")
        return render_template("mobile/order_edit.html", o=f, statuses=STATUS_CHOICES, tab="orders"), 400
    logger.info("order %s added from a phone", order_id)
    flash(f"Added {f['retailer']} {f['order_number'] or ''}.")
    return redirect(url_for("mobile.orders"))


@bp.route("/orders/<int:order_id>", methods=["GET", "POST"])
def order_edit(order_id):
    store = Store(con())
    o = store.order(order_id)
    if not o:
        abort(404)
    if request.method == "GET":
        return render_template("mobile/order_edit.html", o=o, statuses=STATUS_CHOICES, tab="orders")
    f = _order_form()
    f["retailer"] = f["retailer"] or o["retailer"]
    store.update_order(order_id, f)
    logger.info("order %s edited from a phone", order_id)
    flash(f"Saved {f['retailer']} {f['order_number'] or ''}.")
    return redirect(url_for("mobile.orders"))


@bp.post("/orders/<int:order_id>/delete")
def order_delete(order_id):
    Store(con()).delete_order(order_id)
    logger.info("order %s deleted from a phone", order_id)
    flash("Order deleted.")
    return redirect(url_for("mobile.orders"))


def _expense_form():
    category, period = request.form.get("category", ""), request.form.get("billing_period", "")
    return {"vendor": _text("vendor"), "category": category if category in EXPENSE_CATEGORIES else "other",
            "description": _text("description"), "amount": _amount("amount"),
            "currency": (_text("currency") or "USD").upper()[:3], "date": _text("date"),
            "order_number": _text("order_number"), "billing_period": period if period in BILLING_PERIODS else None}


def _expense_page(e, code=200):
    return render_template("mobile/expense_edit.html", e=e, categories=EXPENSE_CATEGORIES,
                           periods=BILLING_PERIODS, tab="expenses"), code


@bp.get("/expenses")
def expenses():
    store = Store(con())
    rows, q = store.expenses(), request.args.get("q", "").strip().lower()
    if q:
        rows = [e for e in rows if q in f"{e['vendor']} {e['description']} {e['category']}".lower()]
    return render_template("mobile/expenses.html", rows=rows[:300], q=q, total=store.stats()["expense_total"],
                           tab="expenses")


@bp.route("/expenses/new", methods=["GET", "POST"])
def expense_new():
    if request.method == "GET":
        return _expense_page(None)
    f = _expense_form()
    if not f["vendor"] or f["amount"] is None:
        flash("Enter who you paid and how much.")
        return _expense_page(f, 400)
    Store(con()).add_expense(f)
    logger.info("expense added from a phone")
    flash(f"Added {f['vendor']}.")
    return redirect(url_for("mobile.expenses"))


@bp.route("/expenses/<int:expense_id>", methods=["GET", "POST"])
def expense_edit(expense_id):
    store = Store(con())
    e = store.expense(expense_id)
    if not e:
        abort(404)
    if request.method == "GET":
        return _expense_page(e)
    f = _expense_form()
    f["vendor"] = f["vendor"] or e["vendor"]
    store.update_expense(expense_id, f)
    logger.info("expense %s edited from a phone", expense_id)
    flash(f"Saved {f['vendor']}.")
    return redirect(url_for("mobile.expenses"))


@bp.post("/expenses/<int:expense_id>/delete")
def expense_delete(expense_id):
    Store(con()).delete_expense(expense_id)
    logger.info("expense %s deleted from a phone", expense_id)
    flash("Expense deleted.")
    return redirect(url_for("mobile.expenses"))


def _profile_form(fallback_name=None):
    f = {k: request.form.get(k, "").strip() for k in PROFILE_FIELDS}
    f["name"] = f["name"] or fallback_name
    if request.form.get("same"):
        for k in ("address1", "address2", "city", "state", "zip", "country"):
            f["ship_" + k] = f["bill_" + k]
    return f


@bp.get("/profiles")
def profiles():
    rows = con().execute("SELECT * FROM profiles ORDER BY name").fetchall()
    return render_template("mobile/profiles.html", rows=rows, tab="profiles")


@bp.route("/profiles/new", methods=["GET", "POST"])
def profile_new():
    if request.method == "GET":
        return render_template("mobile/profile_edit.html", p=None, same=True, tab="profiles")
    c, f = con(), _profile_form()
    if not f["name"] or c.execute("SELECT 1 FROM profiles WHERE name=?", (f["name"],)).fetchone():
        flash("Give the profile a name that isn't used yet.")
        return render_template("mobile/profile_edit.html", p=f, same=bool(request.form.get("same")), tab="profiles"), 400
    c.execute(f"INSERT INTO profiles ({', '.join(PROFILE_FIELDS)}) VALUES ({', '.join('?' * len(PROFILE_FIELDS))})",
              [f[k] for k in PROFILE_FIELDS])
    c.commit()
    logger.info("profile %s added from a phone", f["name"])
    flash(f"Added {f['name']}.")
    return redirect(url_for("mobile.profiles"))


@bp.route("/profiles/<path:name>/edit", methods=["GET", "POST"])
def profile_edit(name):
    c = con()
    p = c.execute("SELECT * FROM profiles WHERE name=?", (name,)).fetchone()
    if not p:
        abort(404)
    if request.method == "GET":
        return render_template("mobile/profile_edit.html", p=p, same=export.same_address(p), tab="profiles")
    f = _profile_form(name)
    if f["name"] != name and c.execute("SELECT 1 FROM profiles WHERE name=?", (f["name"],)).fetchone():
        flash(f"There's already a profile called {f['name']}.")
        return redirect(url_for("mobile.profile_edit", name=name))
    c.execute(f"UPDATE profiles SET {', '.join(k + '=?' for k in PROFILE_FIELDS)} WHERE name=?",
              [f[k] for k in PROFILE_FIELDS] + [name])
    c.commit()
    logger.info("profile %s edited from a phone", f["name"])
    flash(f"Saved {f['name']}.")
    return redirect(url_for("mobile.profiles"))


@bp.post("/profiles/<path:name>/delete")
def profile_delete(name):
    c = con()
    c.execute("DELETE FROM profiles WHERE name=?", (name,))
    c.commit()
    logger.info("profile %s deleted from a phone", name)
    flash(f"Deleted {name}.")
    return redirect(url_for("mobile.profiles"))
