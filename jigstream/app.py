import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, url_for

from . import address as A
from . import (analysis, bankpages, cardcheck, browsertrack, carriers, db, discord, drop, emailbook, engines, expensepages, export,
               inbox, keywords, logs, manual, mobile, overview, parsers, products, profilebook, profilematch, profiles, recurring,
               stock, stuck, tasks, training, updater, wallet)
from .config import Config, format_accounts, save_env
from .extract import STATUSES, carrier_for
from .llm import from_config as llm_for, installed_models
from .mail import check_login
from .orderstore import Store
from .sync import run_sync, summary

logger = logging.getLogger("jigstream.app")

# Statuses offered in the Orders dropdown, with how each status is shown.
STATUS_CHOICES = ["ordered", "in_transit", "out_for_delivery", "delivered", "cancelled", "returned", "not_an_order"]
STATUS_LABELS = {"shipped": "In transit", "in_transit": "In transit", "out_for_delivery": "Out for delivery",
                 "not_an_order": "Not an order (remove)"}
# Any carrier, when the email didn't say which: a free page that detects the carrier from the number.
ANY_CARRIER_URL = "https://parcelsapp.com/en/tracking/{}"

TRACK_URLS = {
    "UPS": "https://www.ups.com/track?tracknum={}",
    "USPS": "https://tools.usps.com/go/TrackConfirmAction?tLabels={}",
    "FedEx": "https://www.fedex.com/fedextrack/?trknbr={}",
    "DHL": "https://www.dhl.com/us-en/home/tracking/tracking-express.html?tracking-id={}",
    "OnTrac": "https://www.ontrac.com/tracking/?number={}",
}


class StopSync(Exception):
    """Someone pressed Stop sync (or an update is installing): skip the steps after reading emails."""


def create_app(db_path=None):
    app = Flask(__name__)
    app.secret_key = "local-only"
    app.config["DB_PATH"] = db_path
    mobile.init_app(app)
    app.register_blueprint(stock.bp)
    app.register_blueprint(manual.bp)
    app.register_blueprint(keywords.bp)
    app.register_blueprint(products.bp)

    @app.context_processor
    def nav_flags():
        return {"update": updater.state,
                "ai": app.config.get("AI_STATE"), "syncing": app.config["SYNC_STATE"]}

    def con():
        return db.connect(db_path)

    # Email sync calls the local model once per email, so it runs in the background.
    sync_state = {"running": False, "last": None, "full": False, "stop": threading.Event()}
    app.config["SYNC_STATE"] = sync_state
    # An update stops a running sync and leaves this note, so the sync carries on after the restart.
    resume_note = Path(db_path or db.DB_PATH).with_name("resume-sync")

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
                    counts = run_sync(config, Store(con()), log=lambda *a, **k: None, progress=progress,
                                      stop=sync_state["stop"].is_set)
                    parts.append(f"Gmail{' (whole mailbox)' if full else ''}"
                                 f"{' stopped part way' if counts.get('stopped') else ''}: {summary(counts)}")
                    if counts.get("stopped"):
                        raise StopSync
                    try:
                        marked, merged = Store(con()).repair_cancels()
                        if marked or merged:
                            logger.info("cancel emails: %d order(s) marked cancelled, %d merged", marked, merged)
                    except Exception:
                        logger.exception("checking cancel emails failed")
                    profilematch.match_orders(con())
                    progress("Finding missing expense amounts...")
                    try:
                        inbox.fill_missing_amounts(config, Store(con()))
                    except Exception:
                        logger.exception("filling missing expense amounts failed")
                    try:
                        recurring.refresh(con())  # new receipts move renewal dates forward
                    except Exception:
                        logger.exception("updating recurring expenses failed")
                except StopSync:
                    pass
                except Exception as e:
                    logger.exception("Gmail sync failed")
                    parts.append(f"Gmail sync failed: {e}")
            stopped = sync_state["stop"].is_set()
            sync_state["last"] = ("Sync stopped. Press Sync now to carry on where it left off. " if stopped
                                  else "Last sync. ") + " ".join(parts)
        finally:
            sync_state["running"] = False

    app.add_template_filter(parsers.tidy, "tidy")  # bot text: entities decoded, markdown links reduced to text

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
        number = (order["tracking_number"] or "").strip()
        if not number:
            return None
        given = (order["carrier"] or "").lower()
        carrier = next((c for c in TRACK_URLS if c.lower() in given), None) or carrier_for(number)
        return TRACK_URLS.get(carrier or "", ANY_CARRIER_URL).format(quote(number))

    @app.template_filter("status_label")
    def status_label(status):
        status = status or "unknown"
        return STATUS_LABELS.get(status, status.replace("_", " ").capitalize())

    def status_counts(by_status):
        counts = {}
        for s, n in by_status.items():
            counts[status_group(s)] = counts.get(status_group(s), 0) + n
        return counts

    @app.template_filter("status_group")
    def status_group(status):
        return "in_transit" if status == "shipped" else (status or "unknown")

    @app.get("/")
    def dashboard():
        c = Store(con()).db
        years = overview.years(c)
        year = request.args.get("year") if request.args.get("year") in years else years[0]
        return render_template("dashboard.html", s=overview.summary(c, year), years=years)

    @app.get("/imports")
    def imports_page():
        report, unmatched = analysis.failure_report(con())
        return render_template("imports.html", report=report, unmatched=unmatched)

    @app.get("/checkouts")
    def checkouts():
        status = request.args.get("status")
        q = "SELECT * FROM checkouts" + (" WHERE status=?" if status else "") + " ORDER BY ts DESC LIMIT 500"
        c = con()
        rows = c.execute(q, (status,) if status else ()).fetchall()
        matched = stuck.cross_reference(Store(c).db)
        emails = {r["id"]: r for r in matched}
        history = stuck.decline_history(c, matched) if status in (None, "decline") else {}
        return render_template("checkouts.html", rows=rows, emails=emails, history=history)

    @app.get("/stuck")
    def stuck_page():
        rows = stuck.cross_reference(Store(con()).db)
        show = request.args.get("show", "stuck")
        keep = {"stuck": ("emailed", "no_email"), "emailed": ("emailed",), "no_email": ("no_email",),
                "cancelled": ("cancelled",)}.get(show)
        return render_template("stuck.html", rows=[r for r in rows if keep is None or r["state"] in keep],
                               counts=stuck.counts(rows), show=show)

    @app.get("/drop")
    def drop_page():
        a = request.args
        site, day, sort = a.get("site") or None, a.get("day") or None, a.get("sort")
        sort = sort if sort in drop.SORTS else "proxy"
        by, name = (a.get("by"), a.get("name")) if a.get("by") in drop.GROUPS and a.get("name") else (None, None)
        here = {"site": site, "day": day, "by": by, "name": name, "sort": sort}

        def link(**change):  # this page's address with some of its choices changed (None drops one)
            return url_for("drop_page", **{k: v for k, v in dict(here, **change).items() if v})
        return render_template("drop.html", r=drop.report(Store(con()).db, site, sort, day, by, name), site=site,
                               day=day, sort=sort, by=by, name=name, link=link)

    @app.get("/drop/cards")
    def card_check_page():
        return render_template("cardcheck.html", r=cardcheck.report(Store(con()).db), minutes=cardcheck.LINK_MINUTES)

    @app.post("/drop/<int:checkout_id>/verdict")
    def drop_verdict(checkout_id):
        verdict = request.form.get("verdict", "")
        store = Store(con())
        row = next((c for c in stuck.cross_reference(store.db) if c["id"] == checkout_id), None)
        back = request.referrer if (request.referrer or "").startswith(request.host_url) else url_for("drop_page")
        if row is None or verdict not in ("good", "cancelled", "auto"):
            flash("Couldn't save that.")
            return redirect(back)
        drop.set_verdict(store.db, checkout_id, verdict)
        o, said = row["order"], f"{row['site'] or 'That'} checkout"
        logger.info("checkout %s marked %s by hand", checkout_id, verdict)
        # When the order email said otherwise, correct the order too, so the AI learns from its emails.
        cancelled = o and (o["shipping_status"] or "") in stuck.CANCELLED
        if o and verdict == "cancelled" and not cancelled:
            relabel(store, o, "cancelled")
            flash(f"{said} marked cancelled. Its order is set to cancelled too, and its emails become training examples.")
        elif o and verdict == "good" and cancelled:
            relabel(store, o, "ordered")
            flash(f"{said} marked good. Its order is set back to ordered, and its emails become training examples.")
        else:
            flash(f"{said} marked {'automatic' if verdict == 'auto' else verdict}.")
        return redirect(back)

    @app.get("/orders")
    def orders():
        store = Store(con())
        profilematch.match_orders(store.db)
        config = Config.from_env()
        rows = store.orders()
        stats = store.stats(orders=rows)
        return render_template("orders.html", rows=rows, stats=stats, sync=sync_state,
                               profile_names=[r[0] for r in store.db.execute("SELECT name FROM profiles ORDER BY name")],
                               profile_counts=_profile_counts(rows),
                               accounts=store.accounts(), configured=bool(config.accounts),
                               training=config.training_mode, labels=store.labels(),
                               label_choices=STATUS_CHOICES, status_counts=status_counts(stats["by_status"]))

    def _profile_counts(rows):
        counts = {}
        for o in rows:
            key = o["profile_name"] or ("?" if o["profile_candidates"] else "")
            counts[key] = counts.get(key, 0) + 1
        return counts

    @app.post("/orders/<int:order_id>/profile")
    def order_profile(order_id):
        choice = request.form.get("profile", "")
        c = con()
        # "__auto__" hands the order back to matching; "" means it belongs to none of your profiles.
        profilematch.set_profile(c, order_id, None if choice == "__auto__" else choice)
        if choice == "__auto__":
            profilematch.match_orders(c)
        logger.info("order %s profile set to %r by hand", order_id, choice)
        return redirect(request.form.get("back") or url_for("orders") + f"#order-{order_id}")

    @app.get("/profile-orders")
    def profile_orders():
        store = Store(con())
        profilematch.match_orders(store.db)
        rows = store.orders()
        hist = profilematch.history(store.db, rows)
        pick = request.args.get("profile")
        used = [h for h in hist.values() if h["orders"] or h["checkouts"]]
        used.sort(key=lambda h: (-len(h["orders"]), h["profile"]["name"].lower()))
        return render_template("profile_orders.html", hist=hist, used=used, pick=hist.get(pick), pick_name=pick,
                               unmatched=[o for o in rows if not o["profile_name"]], total_profiles=len(hist))

    def background_rescan(label_id, order):
        config = Config.from_env()
        store = Store(con())
        try:
            llm = llm_for(config)
            training.rescan(config, store, llm, label_id, order)
        except Exception as e:
            logger.exception("rescan failed")
            store.set_rescan_state(label_id, f"Re-scan failed: {e}")

    def relabel(store, order, status):
        """Set an order's status by hand, then find its emails in the inboxes to learn from (training.rescan)."""
        label_id = store.set_status_manually(order["id"], status)
        logger.info("order %s (%s %s) set to %s by hand", order["id"], order["retailer"], order["order_number"], status)
        threading.Thread(target=background_rescan, args=(label_id, order), daemon=True).start()

    @app.post("/orders/<int:order_id>/label")
    def label_order(order_id):
        status = request.form.get("status", "")
        store = Store(con())
        order = store.order(order_id)
        if order and status == "not_an_order":
            mails = store.dismiss_order(order_id)
            logger.info("order %s (%s %s) marked not an order; %d email(s)", order_id, order["retailer"],
                        order["order_number"], len(mails))
            config = Config.from_env()
            if mails and config.accounts:  # the AI re-reads them and learns they're neither order nor expense
                llm = llm_for(config)
                threading.Thread(target=inbox.label_emails, args=(config, Store(con()), llm, mails, "other"),
                                 daemon=True).start()
            flash(f"Removed {order['retailer'] or 'that order'} {order['order_number'] or ''}. Its emails are saved as "
                  "\"not an order\" examples so the AI learns to skip emails like it.")
            return redirect(url_for("orders"))
        if not order or status not in STATUSES:
            flash("Couldn't save that status.")
            return redirect(url_for("orders"))
        relabel(store, order, status)
        flash(f"{order['retailer']} {order['order_number'] or ''} set to {status}. Searching your inboxes for its "
              "emails to learn from; refresh in a minute.")
        return redirect(url_for("orders") + f"#order-{order_id}")

    @app.get("/training/export")
    def training_export():
        return Response(training.export_jsonl(Store(con())), mimetype="application/x-ndjson",
                        headers={"Content-Disposition": "attachment; filename=order-tracker-training.jsonl"})

    @app.post("/training/<int:example_id>/delete")
    def training_delete(example_id):
        c = con()
        c.execute("DELETE FROM training_examples WHERE id = ?", (example_id,))
        c.commit()
        return redirect(url_for("email_training"))

    @app.post("/setup/training")
    def set_training():
        save_env({"TRAINING_MODE": "1" if request.form.get("training") else "0",
                  "USE_TRAINING_EXAMPLES": "1" if request.form.get("use_examples") else "0"})
        flash("Training settings saved.")
        return redirect(url_for("setup") + "#training")

    @app.get("/api/orders")
    def api_orders():
        return jsonify(Store(con()).orders())

    @app.get("/api/expenses")
    def api_expenses():
        return jsonify(Store(con()).expenses())

    @app.get("/profiles")
    def profile_list():
        c = con()
        group = request.args.get("group")
        q = ("SELECT p.*, c.brand AS linked_brand, c.last4 AS linked_last4, c.exp AS linked_exp, c.name AS linked_name"
             " FROM profiles p LEFT JOIN cards c ON c.id = p.card_id"
             + (" WHERE COALESCE(p.group_name, '') = ?" if group is not None else "") + " ORDER BY p.name")
        rows = [(p, analysis.profile_issues(p), export.same_address(p))
                for p in c.execute(q, (group,) if group is not None else ())]
        groups, ungrouped = profilebook.groups(c)
        profilematch.match_orders(Store(c).db)
        order_counts = dict(c.execute("SELECT profile_name, COUNT(*) FROM tracked_orders WHERE profile_name IS NOT NULL"
                                      " GROUP BY profile_name").fetchall())
        return render_template("profiles.html", order_counts=order_counts, email_groups=emailbook.groups(c)[0], rows=rows, bots=sorted(export.templates(c)), groups=groups,
                               ungrouped=ungrouped, group=group, total=c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0],
                               fields=profilebook.DISTRIBUTE_FIELDS, addresses=profilebook.saved_addresses(c),
                               states=sorted(A.STATE_NAMES.items()), profiles_json=[dict(p) for p, _, _ in rows],
                               cards=wallet.cards(c))

    def _back_to_profiles():
        return redirect(request.form.get("back") or request.referrer or url_for("profile_list"))

    @app.post("/profiles/save")
    def profile_save():
        c = con()
        name, error = profilebook.save_profile(c, request.form, request.form.get("original") or None)
        flash(error or f"Saved {name}.")
        logger.info("profile %s %s", name, "edited" if request.form.get("original") else "created")
        return _back_to_profiles()

    @app.get("/profiles/<path:name>/edit")
    def profile_edit(name):
        return redirect(url_for("profile_list", edit=name))

    @app.post("/profiles/<path:name>/delete")
    def profile_delete(name):
        c = con()
        c.execute("DELETE FROM profiles WHERE name=?", (name,))
        c.commit()
        flash(f"Deleted {name}.")
        return _back_to_profiles()

    @app.post("/profiles/bulk")
    def profile_bulk():
        """Actions on the ticked profiles: move to a group, copy, delete, Mass Distribute."""
        c = con()
        names = request.form.getlist("names")
        action = request.form.get("action")
        if not names:
            flash("Tick one or more profiles first.")
        elif action == "move":
            group = request.form.get("new_group", "").strip() or request.form.get("group_name", "")
            profilebook.move(c, names, group)
            flash(f"Moved {len(names)} profile(s) to {group or 'no group'}.")
        elif action == "duplicate":
            count = max(1, min(100, int(request.form.get("count") or 1)))
            made = [n for name in names for n in profilebook.duplicate(c, name, count)]
            flash(f"Made {len(made)} copies.")
        elif action == "delete":
            c.executemany("DELETE FROM profiles WHERE name=?", [(n,) for n in names])
            c.commit()
            flash(f"Deleted {len(names)} profile(s).")
        elif action == "distribute":
            n, error = profilebook.distribute(c, names, request.form.get("field", ""),
                                              request.form.get("values", "").splitlines(),
                                              bool(request.form.get("billing")), bool(request.form.get("shipping")))
            flash(error or f"Updated {n} profile(s).")
        return _back_to_profiles()

    @app.post("/profiles/groups")
    def profile_groups():
        c = con()
        action, name = request.form.get("action"), request.form.get("name", "")
        if action == "add":
            profilebook.add_group(c, name)
        elif action == "rename":
            profilebook.rename_group(c, name, request.form.get("new_name", ""))
            return redirect(url_for("profile_list", group=request.form.get("new_name", "").strip()))
        elif action == "delete":
            profilebook.delete_group(c, name)
            return redirect(url_for("profile_list"))
        return redirect(url_for("profile_list", group=name.strip()) if action == "add" else url_for("profile_list"))

    @app.get("/cards")
    def cards_page():
        c = con()
        group = request.args.get("group")
        groups, ungrouped = wallet.groups(c)
        return render_template("cards.html", cards=wallet.cards(c, group), groups=groups, ungrouped=ungrouped,
                               group=group, total=c.execute("SELECT COUNT(*) FROM cards").fetchone()[0],
                               providers=wallet.PROVIDERS, types=wallet.CARD_TYPES,
                               profiles=[r[0] for r in c.execute("SELECT name FROM profiles ORDER BY name")])

    @app.post("/cards/add")
    def cards_add():
        c = con()
        parsed, problems = wallet.parse_lines(request.form.get("lines", ""))
        limit = request.form.get("spend_limit", "").replace("$", "").replace(",", "").strip()
        group = request.form.get("new_group", "").strip() or request.form.get("group_name", "").strip()
        added, dup = wallet.add_cards(c, parsed, request.form.get("provider") or "Other",
                                      request.form.get("card_type") or "Standard",
                                      float(limit) if limit.replace(".", "", 1).isdigit() else None, group or None)
        msg = f"Added {added} card(s)." + (f" {dup} were already saved." if dup else "")
        flash(msg + (" Skipped: " + "; ".join(problems[:5]) if problems else ""))
        return redirect(request.referrer or url_for("cards_page"))

    @app.get("/cards/<int:card_id>/reveal")
    def cards_reveal(card_id):
        details = wallet.reveal(con(), card_id)
        logger.info("card %s shown", card_id)  # never the number itself
        return jsonify(details or {"error": "not found"}), 200 if details else 404

    @app.post("/cards/bulk")
    def cards_bulk():
        c = con()
        ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
        action = request.form.get("action")
        if action == "link":
            wallet.link(c, ids[0], request.form.get("profile") or None) if ids else None
            flash("Card linked." if request.form.get("profile") else "Card unlinked.")
        elif not ids:
            flash("Tick one or more cards first.")
        elif action == "delete":
            wallet.delete(c, ids)
            flash(f"Deleted {len(ids)} card(s).")
        elif action == "move":
            group = request.form.get("new_group", "").strip() or request.form.get("group_name", "")
            if group:
                c.execute("INSERT OR IGNORE INTO card_groups VALUES (?)", (group,))
            c.executemany("UPDATE cards SET group_name=? WHERE id=?", [(group or None, i) for i in ids])
            c.commit()
            flash(f"Moved {len(ids)} card(s).")
        elif action in ("active", "inactive"):
            wallet.set_status(c, ids, action)
            flash(f"Marked {len(ids)} card(s) {action}.")
        return redirect(request.referrer or url_for("cards_page"))

    @app.post("/cards/groups")
    def card_groups():
        c = con()
        action, name = request.form.get("action"), request.form.get("name", "").strip()
        if action == "add" and name:
            c.execute("INSERT OR IGNORE INTO card_groups VALUES (?)", (name,))
        elif action == "rename" and request.form.get("new_name", "").strip():
            new = request.form["new_name"].strip()
            c.execute("INSERT OR IGNORE INTO card_groups VALUES (?)", (new,))
            c.execute("UPDATE cards SET group_name=? WHERE group_name=?", (new, name))
            c.execute("DELETE FROM card_groups WHERE name=?", (name,))
            name = new
        elif action == "delete":
            c.execute("UPDATE cards SET group_name=NULL WHERE group_name=?", (name,))
            c.execute("DELETE FROM card_groups WHERE name=?", (name,))
            name = None
        c.commit()
        return redirect(url_for("cards_page", group=name) if name else url_for("cards_page"))

    @app.get("/emails")
    def emails_page():
        c = con()
        group = request.args.get("group")
        groups, ungrouped = emailbook.groups(c)
        rows = emailbook.emails(c, group)
        return render_template("emails.html", emails=rows, groups=groups, ungrouped=ungrouped, group=group,
                               total=c.execute("SELECT COUNT(*) FROM saved_emails").fetchone()[0],
                               in_use=sum(1 for r in rows if r["profile"]))

    @app.post("/emails/add")
    def emails_add():
        text = request.form.get("lines", "")
        upload = request.files.get("file")
        if upload and upload.filename:
            text += "\n" + upload.read().decode("utf-8", errors="replace")
        found = emailbook.parse(text)
        group = request.form.get("new_group", "").strip() or request.form.get("group_name", "").strip()
        if not found:
            flash("No email addresses found in what you pasted.")
            return redirect(request.referrer or url_for("emails_page"))
        added, dup = emailbook.add(con(), group, found)
        flash(f"Added {added} address(es)" + (f" to {group}." if group else ".") + (f" {dup} were already there." if dup else ""))
        return redirect(url_for("emails_page", group=group))

    @app.post("/emails/<int:email_id>/edit")
    def emails_edit(email_id):
        error = emailbook.update(con(), email_id, request.form.get("address", "").strip())
        flash(error or "Saved.")
        return redirect(request.referrer or url_for("emails_page"))

    @app.post("/emails/bulk")
    def emails_bulk():
        c = con()
        ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
        action = request.form.get("action")
        if not ids:
            flash("Tick one or more addresses first.")
        elif action == "delete":
            emailbook.delete(c, ids)
            flash(f"Deleted {len(ids)} address(es).")
        elif action == "move":
            emailbook.move(c, ids, request.form.get("new_group", "").strip() or request.form.get("group_name", ""))
            flash(f"Moved {len(ids)} address(es).")
        return redirect(request.referrer or url_for("emails_page"))

    @app.post("/emails/groups")
    def email_groups():
        c = con()
        action, name = request.form.get("action"), request.form.get("name", "").strip()
        if action == "add" and name:
            c.execute("INSERT OR IGNORE INTO email_groups VALUES (?)", (name,))
            c.commit()
        elif action == "rename" and request.form.get("new_name", "").strip():
            name = request.form["new_name"].strip()
            emailbook.rename_group(c, request.form.get("name", "").strip(), name)
        elif action == "delete":
            emailbook.delete_group(c, name, keep_emails=request.form.get("emails") != "delete")
            name = None
        return redirect(url_for("emails_page", group=name) if name else url_for("emails_page"))

    @app.get("/emails/export")
    def emails_export():
        group = request.args.get("group", "")
        text = "\n".join(emailbook.addresses(con(), group, request.args.get("unused") == "1"))
        if request.args.get("download") == "0":
            return Response(text, mimetype="text/plain")
        return Response(text + "\n", mimetype="text/plain", headers={
            "Content-Disposition": f'attachment; filename="{export.slug(group or "emails")}.txt"'})

    @app.post("/profiles/addresses")
    def profile_address_save():
        error = profilebook.save_address(con(), request.form)
        return jsonify({"error": error, "addresses": profilebook.saved_addresses(con())}), 400 if error else 200

    @app.post("/profiles/export")
    def profile_export():
        c = con()
        bot = request.form.get("bot", "")
        names = request.form.getlist("names")
        rows = c.execute("SELECT * FROM profiles ORDER BY name").fetchall()
        group = request.form.get("group")
        rows = [r for r in rows if r["name"] in names] if names else \
            [r for r in rows if group is None or (r["group_name"] or "") == group]
        fmt, template = export.templates(c).get(bot, ("json", export.GENERIC))
        texts = [f.read().decode("utf-8", "replace") for f in request.files.getlist("cards") if f.filename]
        cards = {r["name"]: {**wallet.reveal(c, r["card_id"]), "holder": r["card_holder"] or ""}
                 for r in rows if r["card_id"] and wallet.reveal(c, r["card_id"])}  # cards you linked
        cards.update(export.cards_from(texts))
        ext, text = export.export(rows, fmt, template, cards)
        logger.info("exported %d profiles for %s (%d card files used)", len(rows), bot or "generic", len(texts))
        return Response(text, mimetype="text/csv" if ext == "csv" else "application/json",
                        headers={"Content-Disposition": f"attachment; filename={export.slug(bot or 'generic')}-profiles.{ext}"})

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
        return redirect(url_for("imports_page"))

    def start_sync(full=False):
        """Start a background sync unless one is running. run.py also calls this on a timer."""
        config = Config.from_env()
        has_discord = bool(config.discord_bot_token and config.discord_channel_ids)
        ai = app.config.get("AI_STATE")
        if sync_state["running"] or not (config.accounts or has_discord) or (ai and not ai["ready"]) \
                or engines.compare_state["running"]:
            return False
        if resume_note.exists():  # carry on with the sync an update stopped
            full = full or resume_note.read_text().strip() == "full"
            resume_note.unlink(missing_ok=True)
        sync_state["running"] = True
        sync_state["full"] = full
        sync_state["stop"].clear()
        sync_state["last"] = "Starting Gmail sync..."
        threading.Thread(target=background_sync, args=(full,), daemon=True).start()
        return True

    def stop_sync(wait=0):
        """Ask a running sync to stop after the email it's reading. Waits up to `wait` seconds for it."""
        if not sync_state["running"]:
            return False
        sync_state["stop"].set()
        sync_state["last"] = "Stopping the sync after the email it's reading..."
        logger.info("sync stop requested")
        deadline = time.monotonic() + wait
        while sync_state["running"] and time.monotonic() < deadline:
            time.sleep(0.2)
        return True

    def pause_sync_for_update():
        """Updates restart BotTrack, so stop a running sync first and pick it up again after the restart.
        Emails already read are saved, so at most the one being read when it restarts is read again."""
        if not sync_state["running"]:
            return
        resume_note.write_text("full" if sync_state["full"] else "normal")
        stop_sync(wait=20)

    app.start_sync = start_sync
    app.stop_sync = stop_sync
    app.pause_sync_for_update = pause_sync_for_update
    app.sync_state = sync_state  # the phone's Update button checks it too

    @app.get("/inbox")
    def inbox_page():
        config = Config.from_env()
        filt = request.args.get("f", "Purchases" if "q" not in request.args else "")
        return render_template("inbox.html", q=request.args.get("q", ""), filt=filt, filters=inbox.QUICK_FILTERS,
                               labels=inbox.LABELS, configured=bool(config.accounts))

    @app.get("/inbox/rows")
    def inbox_rows():
        config = Config.from_env()
        filt = request.args.get("f", "Purchases" if "q" not in request.args else "")
        query = request.args.get("q", "").strip() or inbox.QUICK_FILTERS.get(filt) or ""
        store = Store(con())
        try:
            after = json.loads(request.args.get("after") or "null")
        except ValueError:
            after = None
        if filt == inbox.SKIPPED and not request.args.get("q", "").strip():
            rows, nxt = inbox.skipped(store, offset=after if isinstance(after, int) else 0)
            errors = []
        elif config.accounts:
            rows, errors, nxt = inbox.search(config, query, after=after if isinstance(after, dict) else None)
        else:
            rows, errors, nxt = [], [], None
        ids = [r["message_id"] for r in rows]
        decided, marked = {}, {}
        for i in range(0, len(ids), 500):  # only the emails on screen, not every email ever read
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            decided.update(store.db.execute("SELECT message_id, kind FROM emails WHERE message_id IN (%s)"
                                            % marks, chunk).fetchall())
            marked.update((r["message_id"], dict(r)) for r in store.db.execute(
                "SELECT * FROM inbox_labels WHERE message_id IN (%s)" % marks, chunk))
        return render_template("_inbox_rows.html", rows=rows, errors=errors, labels=inbox.LABELS, marked=marked,
                               decided=decided, configured=bool(config.accounts), more=nxt is not None,
                               next=json.dumps(nxt), first=after is None)

    @app.get("/inbox/view")
    def inbox_view():
        email = inbox.open_email(Config.from_env(), request.args.get("account", ""), request.args.get("id", ""))
        if not email:
            return jsonify({"error": "That email couldn't be found in Gmail."}), 404
        return jsonify({"subject": email.subject, "sender": email.sender, "date": email.date,
                        "body": email.body[:60000]})

    @app.post("/inbox/label")
    def inbox_label():
        label = request.form.get("label", "")
        picks = [json.loads(p) for p in request.form.getlist("pick")]
        if label not in inbox.LABELS or not picks:
            flash("Tick one or more emails and choose a label.")
            return redirect(request.referrer or url_for("inbox_page"))
        config = Config.from_env()
        store = Store(con())
        for row in picks:
            store.set_inbox_label(row, label, "queued")
        llm = llm_for(config)
        threading.Thread(target=inbox.label_emails, args=(config, Store(con()), llm, picks, label), daemon=True).start()
        flash(f"Labelling {len(picks)} email(s) as {inbox.LABELS[label][0]}. The AI re-reads each one and learns "
              "from your answer; refresh in a minute to see the result.")
        return redirect(request.referrer or url_for("inbox_page"))

    @app.get("/api/ai")
    def ai_status():
        return jsonify(app.config.get("AI_STATE") or {"ready": True})

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
        elif app.config.get("AI_STATE") and not app.config["AI_STATE"]["ready"]:
            flash("The AI model is still getting ready; syncing starts by itself when it is.")
        return redirect(url_for("orders"))

    @app.post("/sync/all")
    def sync_all():
        """Everything that comes in by itself, now: Gmail, Discord, linked banks and carrier tracking."""
        started = app.start_sync()
        extra = [name for name, run in app.config.get("SYNC_ALL", []) if run()]
        if carriers.available(os.environ):
            threading.Thread(target=carriers.run, args=(con(), dict(os.environ)), daemon=True).start()
            extra.append("package tracking")
        if started:
            flash("Syncing everything: email and Discord" + (", " + ", ".join(extra) if extra else "") + ".")
        elif sync_state["running"]:
            flash("A sync is already running" + ("; also started " + ", ".join(extra) if extra else "") + ".")
        elif app.config.get("AI_STATE") and not app.config["AI_STATE"]["ready"]:
            flash("The AI model is still getting ready; email syncing starts by itself when it is.")
        elif extra:
            flash("Started " + ", ".join(extra) + ".")
        else:
            flash("Nothing to sync yet. Add an inbox, Discord channels or a bank on Setup.")
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/sync/stop")
    def sync_stop():
        if stop_sync():
            flash("Stopping the sync after the email it's reading. Everything read so far is kept; "
                  "Sync now carries on from there.")
        return redirect(request.referrer or url_for("orders"))

    # The bot's name and channel list come from Discord's API; asking on every Setup visit made the page slow.
    discord_cache = {}

    def discord_info(token):
        hit = discord_cache.get(token)
        if hit and time.monotonic() - hit[0] < 300 and not request.args.get("refresh"):
            return hit[1]
        client = discord.DiscordClient(token)
        me = client.me()
        info = (me.get("username"), me.get("id"), client.channels())
        discord_cache.clear()
        discord_cache[token] = (time.monotonic(), info)
        return info

    @app.get("/setup")
    def setup():
        config = Config.from_env()
        models = installed_models(config.llm_base_url)
        bot, bot_id, channels, discord_error = None, None, [], None
        if config.discord_bot_token:
            try:
                bot, bot_id, channels = discord_info(config.discord_bot_token)
            except discord.DiscordError as e:
                discord_error = str(e)
        return render_template("setup.html", accounts=[a for a, _ in config.accounts], config=config,
                               models=models, synced={a["address"]: a for a in Store(con()).accounts()},
                               bot=bot, bot_id=bot_id, channels=channels, discord_error=discord_error,
                               chosen=set(config.discord_channel_ids), channel_states=discord.channel_states(con()),
                               version=updater.installed_version(), update_settings=updater.settings(),
                               can_update=updater.can_self_update(), backups=updater.backups(),
                               ollama_choices=engines.OLLAMA_CANDIDATES, mlx_choices=engines.mlx_choices(),
                               mlx_ok=engines.mlx_available(), compare=engines.compare_state,
                               best=engines.best(engines.compare_state["results"]),
                               labelled=len(Store(con()).examples()),
                               carrier_keys={c: bool(os.environ.get(k) and os.environ.get(v))
                                             for c, (k, v) in carriers.KEYS.items()},
                               tracking=carriers.state, page_reader=browsertrack._reader is not None,
                               page_tracking=os.environ.get("BROWSER_TRACKING", "1") != "0",
                               can_track=bool(carriers.available(os.environ)),
                               **mobile.setup_context())

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
        discord_cache.clear()
        logger.info("Discord channels set: %s", ids)
        flash(f"Reading {len(ids)} channel(s). The first pull covers the last year of messages.")
        start_sync()
        return redirect(url_for("setup") + "#discord")

    @app.post("/setup/discord/remove")
    def remove_discord():
        save_env({"DISCORD_BOT_TOKEN": "", "DISCORD_CHANNEL_IDS": ""})
        discord_cache.clear()
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

    @app.post("/setup/carriers")
    def set_carrier_keys():
        carrier = request.form.get("carrier")
        if carrier not in carriers.KEYS:
            return redirect(url_for("setup") + "#tracking")
        key_name, secret_name = carriers.KEYS[carrier]
        if request.form.get("action") == "remove":
            save_env({key_name: "", secret_name: ""})
            flash(f"Removed the {carrier} key.")
            return redirect(url_for("setup") + "#tracking")
        key, secret = request.form.get("key", "").strip(), request.form.get("secret", "").strip()
        if not key or not secret:
            flash(f"Paste both the {carrier} key and its secret.")
            return redirect(url_for("setup") + "#tracking")
        carriers._tokens.pop(carrier, None)
        try:
            carriers._token(carrier, {key_name: key, secret_name: secret})
        except carriers.CarrierError as e:
            flash(f"{carrier} didn't accept that key: {e}")
            return redirect(url_for("setup") + "#tracking")
        save_env({key_name: key, secret_name: secret})
        logger.info("%s tracking key saved", carrier)
        flash(f"{carrier} key works. Checking your open {carrier} packages now.")
        threading.Thread(target=carriers.run, args=(con(), dict(os.environ)), daemon=True).start()
        return redirect(url_for("setup") + "#tracking")

    @app.post("/setup/page-tracking")
    def set_page_tracking():
        on = request.form.get("on") == "1"
        save_env({"BROWSER_TRACKING": "1" if on else "0"})
        logger.info("key-free tracking turned %s", "on" if on else "off")
        flash("Key-free tracking is on." if on else "Key-free tracking is off.")
        return redirect(url_for("setup") + "#tracking")

    @app.post("/tracking/check")
    def check_tracking():
        if not carriers.available(os.environ):
            flash("Turn on key-free tracking or add a USPS, UPS or FedEx key on the Setup page first.")
            return redirect(url_for("setup") + "#tracking")
        threading.Thread(target=carriers.run, args=(con(), dict(os.environ)), daemon=True).start()
        flash("Checking every open package with its carrier. Refresh in a minute.")
        return redirect(request.referrer or url_for("orders"))

    @app.post("/setup/compare")
    def compare_models():
        config = Config.from_env()
        picks = [c.split("|", 1) for c in request.form.getlist("candidate") if "|" in c]
        if not picks:
            flash("Tick at least one model to compare.")
        elif sync_state["running"]:
            flash("A sync is running. Compare models once it finishes.")
        elif engines.start_compare(Store(con()), picks, Path("models").resolve(),
                                   (config.llm_engine, config.llm_model) if app.config.get("AI_STATE") else None):
            flash("Comparing models. Models not downloaded yet are fetched first, so this can take a while; "
                  "syncing pauses until it's done.")
        return redirect(url_for("setup") + "#compare")

    @app.get("/api/compare")
    def compare_status():
        return jsonify(engines.compare_state)

    @app.post("/setup/engine")
    def set_engine():
        engine, model = request.form.get("engine", "ollama"), request.form.get("model", "").strip()
        if engine not in ("ollama", "mlx") or not model:
            return redirect(url_for("setup"))
        save_env({"LLM_ENGINE": engine, "LLM_MODEL": model, "LLM_ADAPTER": ""})
        logger.info("default AI set to %s on %s", model, engine)
        if app.config.get("AI_STATE"):  # the Mac app restarts its engine with the new model
            updater.restart_soon()
            return render_template("restarting.html", what=f"Switched to {model}")
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

    def logs_about():
        c = Config.from_env()
        return logs.about(updater.installed_version(), c.llm_engine, c.llm_model)

    @app.get("/logs/send")
    def send_logs_page(note="", error=None):
        files = logs.log_files()
        sizes = {f: (logs.LOG_DIR / f).stat().st_size for f in files if (logs.LOG_DIR / f).exists()}
        return render_template("logs_send.html", files=files, sizes=sizes, about=logs_about(), note=note, error=error,
                               min_chars=logs.NOTE_MIN_CHARS, to=logs.developer_email(),
                               accounts=[a for a, _ in Config.from_env().accounts])

    @app.post("/logs/send")
    def send_logs():
        note = request.form.get("note", "")[:4000]
        error = logs.note_problem(note)
        if not error and request.form.get("consent") != "yes":
            error = "Tick the box to confirm what's being sent first."
        accounts = dict(Config.from_env().accounts)
        sender = request.form.get("sender") or next(iter(accounts), "")
        if not error and sender not in accounts:
            error = "Connect a Gmail inbox on Setup first: the logs are sent from it."
        if error:  # show the page again with what they typed
            return send_logs_page(note, error)
        try:
            if not mobile.from_this_mac():  # so the developer knows where it was seen
                note += f"\n\n(Sent from a phone: {request.headers.get('User-Agent', '')[:200]})"
            to = logs.send_logs(sender, accounts[sender], note, logs_about())
        except Exception as e:  # SMTP errors and the like; never include the password
            logger.warning("sending logs failed: %s", logs.redact(str(e)))
            return send_logs_page(note, f"Couldn't send the logs: {logs.redact(str(e))}. "
                                        "You can download them and send them yourself.")
        logger.info("logs sent to the developer from %s", sender)
        flash(f"Logs sent to {to}. Thanks, that helps fix things faster.")
        return redirect(url_for("show_logs") if mobile.from_this_mac() else url_for("mobile.app_page"))

    @app.post("/update/check")
    def update_check():
        s = updater.check()
        if s["error"]:
            flash(f"Couldn't check for updates: {s['error']}")
        elif s["available"]:
            flash("An update is available. Press Update now to install it.")
        else:
            flash("You're on the latest version.")
        return redirect(request.referrer or url_for("setup"))

    @app.post("/update/install")
    def update_install():
        if not updater.can_self_update():
            flash("This copy runs from a git checkout, so it doesn't update itself. Use git pull.")
            return redirect(url_for("setup"))
        pause_sync_for_update()
        try:
            updater.install()
        except Exception as e:
            logger.exception("update failed")
            start_sync()  # nothing changed, so carry on with the sync the update stopped
            flash(f"Update failed, nothing was changed: {e}")
            return redirect(url_for("setup"))
        updater.restart_soon()
        return render_template("restarting.html", what="Updated")

    @app.post("/update/rollback")
    def update_rollback():
        try:
            updater.rollback()
        except Exception as e:
            flash(f"Couldn't go back: {e}")
            return redirect(url_for("setup"))
        updater.restart_soon()
        return render_template("restarting.html", what="Restored the previous version")

    @app.post("/setup/github")
    def set_github_token():
        save_env({"GITHUB_TOKEN": request.form.get("token", "").strip()})
        updater.check()
        flash("GitHub token saved." if not updater.state["error"] else f"Saved, but the check failed: {updater.state['error']}")
        return redirect(url_for("setup") + "#updates")

    tasks.register(app, db_path)  # Training page: custom tasks, examples and local fine-tuning
    bankpages.register(app, db_path)  # Bank page: statement imports, Plaid, matching charges
    app.register_blueprint(expensepages.pages)  # Expenses: overview, ledger
    return app
