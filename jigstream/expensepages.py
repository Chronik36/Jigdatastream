"""The Expenses area: Overview and Ledger tabs, plus adding and editing botting expenses on the desktop."""
import logging
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import db, ledger
from .extract import BILLING_PERIODS, EXPENSE_CATEGORIES
from .orderstore import Store

logger = logging.getLogger("jigstream.expensepages")
pages = Blueprint("expenses", __name__)


def _con():
    return ledger.ensure(db.connect(current_app.config["DB_PATH"]))


def _back():
    return redirect(request.form.get("back") or request.referrer or url_for("expenses.ledger_page"))


def _sync():
    return current_app.config.get("SYNC_STATE") or {"running": False, "last": None}


@pages.get("/expenses")
def overview():
    c = _con()
    items = ledger.rows(c)
    return render_template("expenses.html", tab="overview", o=ledger.overview(c), chart=ledger.month_chart(c),
                           counts=ledger.counts(items), recent=[r for r in items if r["kind"] == "expense"][:8],
                           sync=_sync(), categories=EXPENSE_CATEGORIES, periods=BILLING_PERIODS)


@pages.get("/expenses/ledger")
def ledger_page():
    c = _con()
    items = ledger.rows(c)
    show = request.args.get("show", "all")
    show = show if show in ledger.FILTERS else "all"
    months = sorted({(r["date"] or "")[:7] for r in items if r["date"]}, reverse=True)
    first, last = ledger.coverage(c)
    return render_template("expense_ledger.html", tab="ledger", rows=[r for r in items if show == "all"
                                                                      or r["state"] == show],
                           counts=ledger.counts(items), show=show, filters=ledger.FILTERS, states=ledger.STATES,
                           months=months, first=first, last=last, categories=EXPENSE_CATEGORIES,
                           periods=BILLING_PERIODS, methods=ledger.PAY_METHODS, colors=ledger.CATEGORY_COLORS)


@pages.post("/expenses/ledger/<int:tx_id>/close")
def ledger_close(tx_id):
    c = _con()
    kind, _, target = request.form.get("target", "").partition(":")
    if kind in ("order", "expense") and target.isdigit():
        if request.form.get("action") == "confirm":
            ledger.confirm_close(c, tx_id, kind, int(target))
            logger.info("close amount confirmed: charge %s is %s %s", tx_id, kind, target)
            flash("Matched.")
        else:
            ledger.reject_close(c, tx_id, kind, int(target))
            flash("Got it, those two aren't the same purchase.")
    return _back()


@pages.post("/expenses/<int:expense_id>/paid")
def paid_with(expense_id):
    method = request.form.get("method", "")
    if method == "__other__":
        method = request.form.get("other", "")
    ledger.set_paid_with(_con(), expense_id, method)
    return _back()


def _text(name):
    return (request.form.get(name) or "").strip() or None


def _amount(name):
    s = re.sub(r"[^\d.\-]", "", request.form.get(name) or "")
    try:
        return round(float(s), 2) if s else None
    except ValueError:
        return None


def _form():
    category, period = request.form.get("category", ""), request.form.get("billing_period", "")
    return {"vendor": _text("vendor"), "category": category if category in EXPENSE_CATEGORIES else "other",
            "description": _text("description"), "amount": _amount("amount"),
            "currency": (_text("currency") or "USD").upper()[:3], "date": _text("date"),
            "billing_period": period if period in BILLING_PERIODS else None}


@pages.post("/expenses/new")
def expense_new():
    f = _form()
    if not f["vendor"] or f["amount"] is None:
        flash("Enter who you paid and how much.")
        return _back()
    Store(_con()).add_expense(f)
    logger.info("expense added by hand")
    flash(f"Added {f['vendor']}.")
    return _back()


@pages.post("/expenses/<int:expense_id>/edit")
def expense_edit(expense_id):
    store = Store(_con())
    e = store.expense(expense_id)
    if e:
        f = _form()
        f["vendor"] = f["vendor"] or e["vendor"]
        if f["amount"] is None:
            f.pop("amount")
        store.update_expense(expense_id, f)
        logger.info("expense %s edited", expense_id)
        flash(f"Saved {f['vendor']}.")
    return _back()


@pages.post("/expenses/<int:expense_id>/delete")
def expense_delete(expense_id):
    c = _con()
    # A charge that paid for it goes back to unmatched, so it shows up as a charge with no receipt.
    c.execute("UPDATE bank_transactions SET match_kind = NULL, match_id = NULL, match_how = NULL"
              " WHERE match_kind = 'expense' AND match_id = ?", (expense_id,))
    Store(c).delete_expense(expense_id)
    logger.info("expense %s deleted", expense_id)
    flash("Expense deleted.")
    return _back()
