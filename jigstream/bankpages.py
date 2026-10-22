"""The Bank page: statement file imports, Plaid linking, and matching charges to orders and receipts."""
import logging
import os
import threading

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from . import bank, db, plaid
from .extract import EXPENSE_CATEGORIES

logger = logging.getLogger("jigstream.bankpages")
pages = Blueprint("bank", __name__)
SHOW = ["all", "unmatched", "matched", "ignored", "refunds"]


def register(app, db_path=None):
    def con():
        return bank.ensure(plaid.ensure(db.connect(db_path)))

    app.config["BANK_DB"] = con
    app.register_blueprint(pages)

    @app.context_processor
    def bank_helpers():
        def bank_paid(kind):
            """{id: charge} for the orders or receipts a card charge was matched to."""
            return {r["match_id"]: r for r in con().execute(
                "SELECT t.match_id, t.date, t.amount, a.name AS account_name FROM bank_transactions t"
                " LEFT JOIN bank_accounts a ON a.id = t.account_id WHERE t.match_kind = ?", (kind,))}
        return {"bank_paid": bank_paid}

    # The Mac app and run.py call start_sync every few hours; linked banks sync on the same beat.
    email_sync = app.start_sync

    def start_sync(full=False):
        started = email_sync(full)
        try:
            c = con()
            if plaid.due(c, float(os.environ.get("PLAID_SYNC_HOURS", 6))):
                threading.Thread(target=plaid.run, args=(c,), daemon=True).start()
        except Exception:
            logger.exception("scheduled bank sync failed to start")
        return started

    app.start_sync = start_sync


def _con():
    from flask import current_app
    return current_app.config["BANK_DB"]()


def _back():
    return redirect(request.form.get("back") or request.referrer or url_for("bank.bank_page"))


@pages.get("/bank")
def bank_page():
    c = _con()
    show = request.args.get("show", "all")
    show = show if show in SHOW else "all"
    account = request.args.get("account", type=int)
    return render_template("bank.html", rows=bank.transactions(c, show, account), stats=bank.stats(c), show=show,
                           shows=SHOW, account=account, accounts=bank.accounts(c), plaid=plaid.settings(c),
                           items=plaid.items(c), plaid_state=plaid.state, categories=EXPENSE_CATEGORIES)


@pages.post("/bank/import")
def bank_import():
    c = _con()
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("Choose one or more statement files (CSV, OFX or QFX) first.")
        return redirect(url_for("bank.bank_page"))
    notes = []
    for f in files:
        try:
            name, added, skipped, matched = bank.import_file(
                c, f.filename, f.read(), request.form.get("account", ""), request.form.get("charges", "auto"))
            notes.append(f"{f.filename}: {added} new into {name}" + (f", {skipped} already there" if skipped else "")
                         + f", {matched} matched")
        except bank.ImportProblem as e:
            notes.append(f"{f.filename}: {e}")
            logger.warning("statement import of %s failed: %s", f.filename, e)
        except Exception as e:
            logger.exception("statement import of %s failed", f.filename)
            notes.append(f"{f.filename}: couldn't read it ({e.__class__.__name__}; see Logs)")
    flash(". ".join(notes) + ".")
    return redirect(url_for("bank.bank_page"))


@pages.post("/bank/rematch")
def bank_rematch():
    n = bank.auto_match(_con())
    flash(f"Matched {n} more charge(s)." if n else "No new matches. Unmatched charges may not have an email "
          "receipt yet; sync your inboxes or match them by hand.")
    return _back()


@pages.get("/bank/<int:tx_id>/candidates")
def bank_candidates(tx_id):
    c = _con()
    tx = c.execute("SELECT * FROM bank_transactions WHERE id = ?", (tx_id,)).fetchone()
    if not tx:
        return jsonify({"error": "not found"}), 404
    q = request.args.get("q", "").strip()
    if q:
        found = bank.search_targets(c, q)
    else:
        found = [{"kind": k, "id": r["id"], "name": r.get("retailer") or r.get("vendor"),
                  "ref": r.get("order_number") or r.get("description"), "day": r["day"], "amount": r["amount"],
                  "named": named} for k, r, named, _ in bank.candidates(c, tx, include_claimed=True)]
    return jsonify({"description": tx["description"], "amount": tx["amount"], "date": tx["date"], "options": found})


@pages.post("/bank/<int:tx_id>/match")
def bank_match(tx_id):
    c = _con()
    action = request.form.get("action")
    if action == "match":
        kind, _, target = request.form.get("target", "").partition(":")
        if kind in ("order", "expense") and target.isdigit():
            bank.set_match(c, tx_id, kind, int(target))
            logger.info("bank charge %s matched to %s %s by hand", tx_id, kind, target)
    elif action == "unmatch":
        bank.set_match(c, tx_id, None)
    elif action == "ignore":
        bank.set_match(c, tx_id, None, how="ignored")
    elif action == "auto":
        bank.reset_match(c, tx_id)
    elif action == "expense":
        category = request.form.get("category")
        bank.add_as_expense(c, tx_id, request.form.get("vendor", "").strip(),
                            category if category in EXPENSE_CATEGORIES else "other")
        flash("Added to your botting expenses.")
    return _back()


@pages.post("/bank/accounts/<int:account_id>/delete")
def bank_account_delete(account_id):
    c = _con()
    row = c.execute("SELECT name, source FROM bank_accounts WHERE id = ?", (account_id,)).fetchone()
    if row and row["source"] == "plaid":
        flash("That account syncs through Plaid. Unlink the bank instead.")
    elif row:
        bank.delete_account(c, account_id)
        logger.info("bank account %s removed with its transactions", account_id)
        flash(f"Removed {row['name']} and its transactions.")
    return redirect(url_for("bank.bank_page"))


# ---------- Plaid ----------

@pages.get("/bank/plaid")
def plaid_page():
    c = _con()
    return render_template("plaid.html", plaid=plaid.settings(c), items=plaid.items(c), plaid_state=plaid.state)


@pages.post("/bank/plaid/keys")
def plaid_keys():
    c = _con()
    if request.form.get("action") == "remove":
        plaid.remove_keys(c)
        logger.info("Plaid keys removed")
        flash("Plaid keys removed. Transactions already synced stay here.")
        return redirect(url_for("bank.plaid_page"))
    client_id, secret = request.form.get("client_id", "").strip(), request.form.get("secret", "").strip()
    env = request.form.get("env", "sandbox")
    if not client_id or not secret:
        flash("Paste both the client_id and the secret.")
        return redirect(url_for("bank.plaid_page"))
    try:
        plaid.check_keys(client_id, secret, env if env in plaid.HOSTS else "sandbox")
    except plaid.PlaidError as e:
        logger.warning("Plaid rejected the keys: %s", e)
        flash(f"Plaid didn't accept those keys: {e}")
        return redirect(url_for("bank.plaid_page"))
    plaid.save_keys(c, client_id, secret, env)
    logger.info("Plaid keys saved (%s)", env)
    flash("Plaid keys work. Now press Link a bank.")
    return redirect(url_for("bank.plaid_page"))


@pages.post("/bank/plaid/link-token")
def plaid_link_token():
    try:
        return jsonify({"link_token": plaid.link_token(_con(), request.form.get("item") or None)})
    except plaid.PlaidError as e:
        logger.warning("Plaid link token failed: %s", e)
        return jsonify({"error": str(e)}), 400


@pages.post("/bank/plaid/exchange")
def plaid_exchange():
    data = request.get_json(silent=True) or {}
    c = _con()
    try:
        plaid.exchange(c, data.get("public_token", ""), (data.get("institution") or "").strip()[:80])
    except plaid.PlaidError as e:
        logger.warning("Plaid link failed: %s", e)
        return jsonify({"error": str(e)}), 400
    threading.Thread(target=plaid.run, args=(_con(),), daemon=True).start()  # the first pull can take a while
    return jsonify({"ok": True})


@pages.post("/bank/plaid/sync")
def plaid_sync():
    c = _con()
    if not plaid.items(c):
        flash("Link a bank first.")
    elif plaid.run(c) is None:
        flash("A bank sync is already running.")
    else:
        flash(plaid.state["last"])
    return _back()


@pages.post("/bank/plaid/items/<item_id>/remove")
def plaid_remove(item_id):
    plaid.remove_item(_con(), item_id, keep_transactions=request.form.get("transactions") != "delete")
    flash("Bank unlinked.")
    return redirect(url_for("bank.plaid_page"))
