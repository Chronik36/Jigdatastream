from flask import Flask, redirect, render_template, request, url_for, flash

from . import analysis, db, emails, parsers, profiles, tracker


def create_app(db_path=None):
    app = Flask(__name__)
    app.secret_key = "local-only"

    def con():
        return db.connect(db_path)

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

    @app.get("/tracker")
    def order_tracker():
        c = con()
        return render_template("tracker.html", rows=tracker.link(c), stats=tracker.retailer_stats(c))

    @app.get("/orders")
    def orders():
        return render_template("orders.html", rows=con().execute("SELECT * FROM orders ORDER BY ts DESC LIMIT 500").fetchall())

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
        try:
            flash(f"Added {emails.sync_gmail(con())} order emails")
        except Exception as e:
            flash(f"Gmail sync failed: {e}")
        return redirect(url_for("orders"))

    return app
