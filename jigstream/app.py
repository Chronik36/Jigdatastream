import threading

from flask import Flask, jsonify, redirect, render_template, request, url_for, flash

from . import analysis, db, discord, emails, parsers, llm, profiles, tracker, updater


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
        return render_template("dashboard.html", active="dashboard", tot=tot, sites=analysis.by_dimension(c, "site"),
                               bots=analysis.by_dimension(c, "bot"), report=report, unmatched=unmatched,
                               cancelled=analysis.cancelled_after_success(c))

    @app.get("/checkouts")
    def checkouts():
        status = request.args.get("status")
        q = "SELECT * FROM checkouts" + (" WHERE status=?" if status else "") + " ORDER BY ts DESC LIMIT 500"
        rows = con().execute(q, (status,) if status else ()).fetchall()
        return render_template("checkouts.html", active="checkouts", rows=rows)

    @app.get("/import")
    def import_page():
        return render_template("import.html", active="import", channels=discord._state(con(), "discord_channels"))

    @app.get("/tracker")
    def order_tracker():
        c = con()
        return render_template("tracker.html", active="tracker", rows=tracker.link(c), stats=tracker.retailer_stats(c),
                               profile_cancels=tracker.cancel_by_profile(c))

    @app.get("/orders")
    def orders():
        return render_template("orders.html", active="orders", rows=con().execute("SELECT * FROM orders ORDER BY ts DESC LIMIT 500").fetchall())

    @app.get("/profiles")
    def profile_list():
        c = con()
        rows = [(p, analysis.profile_issues(p)) for p in c.execute("SELECT * FROM profiles")]
        return render_template("profiles.html", active="profiles", rows=rows)

    @app.post("/upload/<kind>")
    def upload(kind):
        c = con()
        text = b"".join(f.read() for f in request.files.getlist("files")).decode("utf-8", "replace") \
            if kind == "profiles" else None
        try:
            if kind == "webhooks":
                n = sum(parsers.ingest(c, f.read().decode("utf-8", "replace"), use_llm=bool(request.form.get("ai"))) for f in request.files.getlist("files"))
            elif kind == "profiles":
                n = profiles.ingest(c, text, request.form.get("bot", ""))
            else:
                return "unknown", 404
            flash(f"Imported {n} {kind} records")
        except Exception as e:  # surface parse problems in the UI
            flash(f"Import failed: {e}")
        return redirect(url_for("dashboard"))

    @app.post("/sync/discord")
    def sync_discord():
        c = con()
        raw = request.form.get("channels")
        ids = [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()] if raw is not None else None
        try:
            added, scanned = discord.sync(c, ids, use_llm=bool(request.form.get("ai")))
            flash(f"Discord: read {scanned} messages, imported {added} new checkouts")
        except Exception as e:
            flash(f"Discord sync failed: {e}")
        return redirect(url_for("checkouts"))

    @app.post("/sync/gmail")
    def sync():
        try:
            flash(f"Added {emails.sync_gmail(con(), use_llm=bool(request.form.get("ai")))} order emails")
        except Exception as e:
            flash(f"Gmail sync failed: {e}")
        return redirect(url_for("orders"))

    @app.get("/ai/status")
    def ai_status():
        ok, msg = llm.available()
        return jsonify(ok=ok, message=msg)

    @app.post("/explain/<path:name>")
    def explain(name):
        c = con()
        p = c.execute("SELECT * FROM profiles WHERE name=?", (name,)).fetchone()
        if not p:
            return jsonify(ok=False, text="Unknown profile"), 404
        ok, msg = llm.available()
        if not ok:
            return jsonify(ok=False, text=msg)
        reasons = [r["reason"] for r in c.execute(
            "SELECT reason FROM checkouts WHERE profile=? AND status!='success' AND reason!='' ORDER BY ts DESC LIMIT 15", (name,))]
        try:
            return jsonify(ok=True, text=llm.explain_failures(dict(p), analysis.profile_issues(p), reasons))
        except Exception as e:
            return jsonify(ok=False, text=f"AI request failed: {e}")

    @app.get("/update/status")
    def update_status():
        return jsonify(updater.status())

    @app.post("/update/apply")
    def update_apply():
        res = updater.apply()
        if res.get("updated"):
            threading.Timer(1.0, updater.restart).start()  # let the response finish, then relaunch
        return jsonify(res)

    return app
