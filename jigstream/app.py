import threading

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from . import analysis, db, parsers, profiles
from .config import Config
from .orderstore import Store
from .sync import run_sync, summary

TRACK_URLS = {
    "UPS": "https://www.ups.com/track?tracknum={}",
    "USPS": "https://tools.usps.com/go/TrackConfirmAction?tLabels={}",
    "FedEx": "https://www.fedex.com/fedextrack/?trknbr={}",
    "DHL": "https://www.dhl.com/us-en/home/tracking/tracking-express.html?tracking-id={}",
    "OnTrac": "https://www.ontrac.com/tracking/?number={}",
}


def create_app(db_path=None):
    app = Flask(__name__)
    app.secret_key = "local-only"

    def con():
        return db.connect(db_path)

    # Email sync calls the local model once per email, so it runs in the background.
    sync_state = {"running": False, "last": None}

    def progress(text):
        sync_state["last"] = text

    def background_sync(full):
        try:
            config = Config.from_env()
            config.full_history = full
            counts = run_sync(config, Store(con()), log=lambda *a, **k: None, progress=progress)
            sync_state["last"] = f"Last Gmail sync{' (whole mailbox)' if full else ''}: {summary(counts)}"
        except Exception as e:
            sync_state["last"] = f"Gmail sync failed: {e}"
        finally:
            sync_state["running"] = False

    @app.template_filter("money")
    def money(amount, currency="USD"):
        if amount is None:
            return "-"
        return ("$" if currency in (None, "USD") else f"{currency} ") + f"{amount:,.2f}"

    @app.template_filter("track_url")
    def track_url(order):
        url = TRACK_URLS.get(order["carrier"] or "")
        return url.format(order["tracking_number"]) if url and order["tracking_number"] else None

    @app.get("/")
    def dashboard():
        c = con()
        tot = c.execute("SELECT COUNT(*) n, SUM(status='success') s, SUM(status!='success') f FROM checkouts").fetchone()
        report, unmatched = analysis.failure_report(c)
        return render_template("dashboard.html", tot=tot, sites=analysis.by_dimension(c, "site"),
                               bots=analysis.by_dimension(c, "bot"), report=report, unmatched=unmatched,
                               cancelled=analysis.cancelled_after_success(c))

    @app.get("/checkouts")
    def checkouts():
        status = request.args.get("status")
        q = "SELECT * FROM checkouts" + (" WHERE status=?" if status else "") + " ORDER BY ts DESC LIMIT 500"
        rows = con().execute(q, (status,) if status else ()).fetchall()
        return render_template("checkouts.html", rows=rows)

    @app.get("/orders")
    def orders():
        store = Store(con())
        return render_template("orders.html", rows=store.orders(), stats=store.stats(), sync=sync_state,
                               accounts=store.accounts())

    @app.get("/expenses")
    def expenses():
        store = Store(con())
        return render_template("expenses.html", rows=store.expenses(), stats=store.stats(), sync=sync_state)

    @app.get("/api/orders")
    def api_orders():
        return jsonify(Store(con()).orders())

    @app.get("/api/expenses")
    def api_expenses():
        return jsonify(Store(con()).expenses())

    @app.get("/profiles")
    def profile_list():
        c = con()
        rows = [(p, analysis.profile_issues(p)) for p in c.execute("SELECT * FROM profiles")]
        return render_template("profiles.html", rows=rows)

    @app.post("/upload/<kind>")
    def upload(kind):
        c = con()
        text = b"".join(f.read() for f in request.files.getlist("files")).decode("utf-8", "replace") \
            if kind == "profiles" else None
        try:
            if kind == "webhooks":
                n = sum(parsers.ingest(c, f.read().decode("utf-8", "replace")) for f in request.files.getlist("files"))
            elif kind == "profiles":
                n = profiles.ingest(c, text, request.form.get("bot", ""))
            else:
                return "unknown", 404
            flash(f"Imported {n} {kind} records")
        except Exception as e:  # surface parse problems in the UI
            flash(f"Import failed: {e}")
        return redirect(url_for("dashboard"))

    @app.post("/sync/gmail")
    def sync():
        if not sync_state["running"]:
            full = request.form.get("full") == "1"
            sync_state["running"] = True
            sync_state["last"] = "Starting Gmail sync..."
            threading.Thread(target=background_sync, args=(full,), daemon=True).start()
            flash("Whole-mailbox backfill started. It can take hours; already processed mail is skipped, "
                  "so it is safe to stop and run again." if full else
                  "Gmail sync started. Refresh to see progress.")
        return redirect(url_for("orders"))

    return app
