import logging
import os
import threading
from urllib.parse import quote

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, url_for

from . import analysis, db, discord, logs, parsers, profiles
from .config import Config, format_accounts, save_env
from .llm import installed_models
from .mail import check_login
from .orderstore import Store
from .sync import run_sync, summary

logger = logging.getLogger("jigstream.app")

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
        config = Config.from_env()
        config.full_history = full
        parts = []
        try:
            if config.discord_bot_token and config.discord_channel_ids:
                progress("Reading Discord channels...")
                try:
                    d = discord.sync_channels(con(), discord.DiscordClient(config.discord_bot_token),
                                              config.discord_channel_ids, config.first_sync_days, log=progress)
                    parts.append(f"Discord: {d['checkouts']} new checkouts from {d['messages']} messages"
                                 + (f", {len(d['failed'])} channel(s) failed (see Setup)" if d["failed"] else "") + ".")
                except Exception as e:
                    logger.exception("Discord sync failed")
                    parts.append(f"Discord sync failed: {e}.")
            if config.accounts:
                try:
                    counts = run_sync(config, Store(con()), log=lambda *a, **k: None, progress=progress)
                    parts.append(f"Gmail{' (whole mailbox)' if full else ''}: {summary(counts)}")
                except Exception as e:
                    logger.exception("Gmail sync failed")
                    parts.append(f"Gmail sync failed: {e}")
            sync_state["last"] = "Last sync. " + " ".join(parts)
        finally:
            sync_state["running"] = False

    @app.template_filter("money")
    def money(amount, currency="USD"):
        if amount is None:
            return "-"
        return ("$" if currency in (None, "USD") else f"{currency} ") + f"{amount:,.2f}"

    @app.template_filter("track_url")
    def track_url(order):
        # Prefer the carrier link from the email itself, else build the carrier's public page.
        if order.get("tracking_url"):
            return order["tracking_url"]
        url = TRACK_URLS.get(order["carrier"] or "")
        return url.format(quote(order["tracking_number"])) if url and order["tracking_number"] else None

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
                               accounts=store.accounts(), configured=bool(Config.from_env().accounts))

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

    def start_sync(full=False):
        """Start a background sync unless one is running. run.py also calls this on a timer."""
        config = Config.from_env()
        has_discord = bool(config.discord_bot_token and config.discord_channel_ids)
        if sync_state["running"] or not (config.accounts or has_discord):
            return False
        sync_state["running"] = True
        sync_state["last"] = "Starting Gmail sync..."
        threading.Thread(target=background_sync, args=(full,), daemon=True).start()
        return True

    app.start_sync = start_sync

    @app.post("/sync/gmail")
    def sync():
        full = request.form.get("full") == "1"
        config = Config.from_env()
        if not config.accounts and not (config.discord_bot_token and config.discord_channel_ids):
            flash("Add a Gmail inbox or Discord channels first.")
            return redirect(url_for("setup"))
        if start_sync(full):
            flash("Whole-mailbox backfill started. It can take hours; already processed mail is skipped, "
                  "so it is safe to stop and run again." if full else
                  "Gmail sync started. Refresh to see progress.")
        return redirect(url_for("orders"))

    @app.get("/setup")
    def setup():
        config = Config.from_env()
        models = installed_models(config.llm_base_url)
        bot, bot_id, channels, discord_error = None, None, [], None
        if config.discord_bot_token:
            try:
                client = discord.DiscordClient(config.discord_bot_token)
                me = client.me()
                bot, bot_id = me.get("username"), me.get("id")
                channels = client.channels()
            except discord.DiscordError as e:
                discord_error = str(e)
        return render_template("setup.html", accounts=[a for a, _ in config.accounts], config=config,
                               models=models, synced={a["address"]: a for a in Store(con()).accounts()},
                               bot=bot, bot_id=bot_id, channels=channels, discord_error=discord_error,
                               chosen=set(config.discord_channel_ids), channel_states=discord.channel_states(con()))

    @app.post("/setup/discord/token")
    def set_discord_token():
        token = request.form.get("token", "").strip()
        if token.lower().startswith("bot "):
            token = token[4:].strip()
        try:
            name = discord.DiscordClient(token).me().get("username")
        except discord.DiscordError as e:
            flash(str(e))
            return redirect(url_for("setup") + "#discord")
        save_env({"DISCORD_BOT_TOKEN": token})
        logger.info("Discord bot connected: %s", name)
        flash(f"Connected as {name}. Now tick the channels your bots post checkouts to.")
        return redirect(url_for("setup") + "#discord")

    @app.post("/setup/discord/channels")
    def set_discord_channels():
        ids = [i for i in request.form.getlist("channel") if i.isdigit()]
        save_env({"DISCORD_CHANNEL_IDS": ",".join(ids)})
        logger.info("Discord channels set: %s", ids)
        flash(f"Reading {len(ids)} channel(s). The first pull covers the last year of messages.")
        start_sync()
        return redirect(url_for("setup") + "#discord")

    @app.post("/setup/discord/remove")
    def remove_discord():
        save_env({"DISCORD_BOT_TOKEN": "", "DISCORD_CHANNEL_IDS": ""})
        logger.info("Discord disconnected")
        flash("Discord disconnected. Checkouts already read stay on the dashboard.")
        return redirect(url_for("setup"))

    @app.post("/setup/inbox")
    def add_inbox():
        config = Config.from_env()
        address = request.form.get("address", "").strip()
        password = request.form.get("password", "").replace(" ", "").strip()
        if "@" not in address or not password:
            flash("Enter the Gmail address and its app password.")
            return redirect(url_for("setup"))
        error = check_login(config, address, password)
        if error:
            flash(f"{address}: {error}")
            return redirect(url_for("setup"))
        accounts = [a for a in config.accounts if a[0].lower() != address.lower()] + [(address, password)]
        save_env({"GMAIL_ACCOUNTS": format_accounts(accounts)})
        logger.info("inbox added: %s", address)
        os.environ.pop("GMAIL_ADDRESS", None)
        os.environ.pop("GMAIL_APP_PASSWORD", None)
        flash(f"Added {address}. The first sync reads the last year of mail and runs in the background.")
        start_sync()
        return redirect(url_for("orders"))

    @app.post("/setup/inbox/remove")
    def remove_inbox():
        address = request.form.get("address", "")
        accounts = [a for a in Config.from_env().accounts if a[0] != address]
        save_env({"GMAIL_ACCOUNTS": format_accounts(accounts)})
        logger.info("inbox removed: %s", address)
        flash(f"Removed {address}. Orders already found from it stay on the dashboard.")
        return redirect(url_for("setup"))

    @app.post("/setup/model")
    def set_model():
        model = request.form.get("model", "").strip()
        if model:
            save_env({"LLM_MODEL": model})
            logger.info("model set to %s", model)
            flash(f"Using {model} for new emails.")
        return redirect(url_for("setup"))

    @app.get("/logs")
    def show_logs():
        files = logs.log_files()
        name = request.args.get("file", "app.log")
        if name not in files:
            name = files[0] if files else "app.log"
        return render_template("logs.html", files=files, name=name, text=logs.tail(name, 400))

    @app.get("/logs/download")
    def download_logs():
        return Response(logs.zip_logs(), mimetype="application/zip",
                        headers={"Content-Disposition": "attachment; filename=order-tracker-logs.zip"})

    return app
