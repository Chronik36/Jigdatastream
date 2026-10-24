"""Card locks page: the bank window (vcclock.py), the cards found in it, and locking or unlocking the picked ones."""
import threading

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

from . import db, vcclock, wallet

bp = Blueprint("cardlocks", __name__)


def _con():
    return db.connect(current_app.config.get("DB_PATH"))


def _saved(con, bank):
    return {r["last4"]: dict(r) for r in con.execute("SELECT id, last4, status, name FROM cards WHERE provider=?", (bank,))}


@bp.get("/cards/locks")
def page():
    s = vcclock._session if vcclock.available() else None
    bank = (s.bank if s else None) or request.args.get("bank") or "Capital One"
    return render_template("card_locks.html", available=vcclock.available(), banks=list(vcclock.BANKS), bank=bank,
                           s=s, open=bool(s and s.window), cards=(s.cards if s else []), saved=_saved(_con(), bank))


def _back():
    return redirect(url_for("cardlocks.page"))


@bp.post("/cards/locks/open")
def open_bank():
    bank = request.form.get("bank")
    if bank not in vcclock.BANKS:
        return _back()
    try:
        vcclock.session().open(bank)
        flash(f"Sign in to {bank} in the new window, open the page that lists your virtual cards, then press Scan.")
    except vcclock.Closed as e:
        flash(str(e))
    return _back()


@bp.post("/cards/locks/scan")
def scan():
    try:
        found = vcclock.session().scan()
        flash(f"Found {len(found)} card{'' if len(found) == 1 else 's'} on the page." if found else
              "No cards found on that page. Open the page that lists your virtual cards (with their last 4 digits) and scan again.")
    except vcclock.Closed as e:
        flash(str(e))
    return _back()


@bp.post("/cards/locks/run")
def run():
    want = request.form.get("want")
    picks = request.form.getlist("last4")
    if want not in ("lock", "unlock") or not picks:
        flash("Tick one or more cards first.")
        return _back()
    try:
        s = vcclock.session()
    except vcclock.Closed as e:
        flash(str(e))
        return _back()
    if s.job.get("running"):
        flash("Already working through cards.")
        return _back()
    path, bank = current_app.config.get("DB_PATH"), s.bank

    def done(job):  # keep BotTrack's own copy of each card's state in step with the bank
        con = db.connect(path)
        for r in job["results"]:
            if r["ok"]:
                con.execute("UPDATE cards SET status=? WHERE provider=? AND last4=?",
                            ("locked" if want == "lock" else "active", bank, r["last4"]))
        con.commit()
    threading.Thread(target=s.run, args=(picks, want, done), daemon=True).start()
    return _back()


@bp.post("/cards/locks/stop")
def stop():
    if vcclock.available() and vcclock._session:
        vcclock._session.stop.set()
    return _back()


@bp.post("/cards/locks/teach")
def teach():
    want = request.form.get("want", "lock")
    try:
        vcclock.session().teach(want)
        flash(f"Now {want} one card yourself in the bank window. BotTrack watches that click and does the same on the others.")
    except vcclock.Closed as e:
        flash(str(e))
    return _back()


@bp.get("/cards/locks/status")
def status():
    s = vcclock._session if vcclock.available() else None
    if not s:
        return jsonify({"running": False})
    learned = None
    try:
        learned = s.check_learned() if s.learning else None
    except vcclock.Closed:
        pass
    return jsonify({"running": s.job["running"], "done": s.job["done"], "total": s.job["total"],
                    "learning": s.learning, "learned": bool(learned), "open": bool(s.window)})


@bp.post("/cards/locks/import")
def import_cards():
    """Add the picked cards to Cards: encrypted in full when the page showed the number, otherwise by last 4."""
    s = vcclock.session() if vcclock.available() else None
    if not s:
        return _back()
    picks = set(request.form.getlist("last4"))
    con = _con()
    full = [c for c in s.cards if c["last4"] in picks and c.get("number") and c.get("exp") and c.get("cvv")]
    cards, _ = wallet.parse_lines("\n".join(f"{(c['label'] or '').replace(':', ' ')}:{c['number']}:{c['exp']}:{c['cvv']}"
                                            for c in full))
    added, dup = wallet.add_cards(con, cards, s.bank, "Virtual", group=request.form.get("group") or None) if cards else (0, 0)
    done = {c["last4"] for c in full}
    for c in s.cards:
        if c["last4"] in picks and c["last4"] not in done and wallet.add_last4(con, s.bank, c["last4"], c["label"], c["exp"]):
            added += 1
    if not picks:
        flash("Tick the cards to add first.")
    else:
        flash(f"Added {added} card{'' if added == 1 else 's'} to Cards." + (f" {dup} were already there." if dup else ""))
    return _back()
