"""The Expenses area: Overview and Ledger tabs, plus adding and editing botting expenses on the desktop."""
import logging
import re
import threading
from datetime import date

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for

from . import budget, db, inbox, ledger, recurring, sales
from .config import Config
from .extract import BILLING_PERIODS, EXPENSE_CATEGORIES
from .orderstore import Store

logger = logging.getLogger("jigstream.expensepages")
pages = Blueprint("expenses", __name__)


def _con():
    return sales.ensure(db.connect(current_app.config["DB_PATH"]))


def _back():
    return redirect(request.form.get("back") or request.referrer or url_for("expenses.ledger_page"))


def _sync():
    return current_app.config.get("SYNC_STATE") or {"running": False, "last": None}


_amounts = {"running": False, "last": None}


def _fill_amounts(path):
    try:
        filled, removed = inbox.fill_missing_amounts(Config.from_env(), Store(db.connect(path)), limit=500, recheck=True)
        _amounts["last"] = (f"Found {filled} missing amount(s)" + (f" and removed {removed} reminder(s)" if removed else "")
                            + ". Any still blank have no total BotTrack can read; click the expense to type it in.")
    except Exception as e:
        logger.exception("finding missing expense amounts failed")
        _amounts["last"] = f"Couldn't finish: {e}"
    finally:
        _amounts["running"] = False


@pages.post("/expenses/fill-amounts")
def fill_amounts():
    """Re-read every expense email that has no amount yet (the sync only tries each one once)."""
    if not _amounts["running"]:
        _amounts.update(running=True, last=None)
        threading.Thread(target=_fill_amounts, args=(current_app.config["DB_PATH"],), daemon=True).start()
    flash("Reading the expense emails that have no amount. Refresh in a minute.")
    return _back()


@pages.get("/expenses")
def overview():
    c = _con()
    recurring.refresh(c)
    items, subs = ledger.rows(c), recurring.subscriptions(c)
    return render_template("expenses.html", tab="overview", o=ledger.overview(c), chart=ledger.month_chart(c),
                           counts=ledger.counts(items), recent=[r for r in items if r["kind"] == "expense"][:8], amounts=_amounts,
                           missing=sum(1 for r in items if r["kind"] == "expense" and r["amount"] is None),
                           rec=recurring.totals(subs), soon=recurring.upcoming(subs)[:6], sync=_sync(),
                           month=budget.month_view(c, budget.month_of(date.today())),
                           profit=sales.month_numbers(c, budget.month_of(date.today())),
                           **_dialogs())


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
                           months=months, first=first, last=last, methods=ledger.PAY_METHODS,
                           colors=ledger.CATEGORY_COLORS, subs={s["id"]: s for s in recurring.subscriptions(c)},
                           **_dialogs())


def _dialogs():
    """What the Add expense and Repeats dialogs need."""
    return {"categories": EXPENSE_CATEGORIES, "periods": BILLING_PERIODS, "rec_periods": recurring.PERIODS}


@pages.get("/expenses/recurring")
def recurring_page():
    c = _con()
    recurring.refresh(c)
    items, subs = ledger.rows(c), recurring.subscriptions(c)
    return render_template("expense_recurring.html", tab="recurring", counts=ledger.counts(items), subs=subs,
                           totals=recurring.totals(subs), weeks=recurring.calendar_weeks(subs),
                           ideas=recurring.suggestions(c), today=date.today(), **_dialogs())


def _rec_form():
    period = request.form.get("period", "monthly")
    category = request.form.get("category", "")
    days = request.form.get("every_days", type=int)
    return {"vendor": _text("vendor"), "period": period if period in recurring.PERIODS else "monthly",
            "every_days": days if days and days > 0 else None, "amount": _amount("amount"),
            "category": category if category in EXPENSE_CATEGORIES else None, "next_due": _text("next_due")}


@pages.post("/expenses/recurring/track")
def recurring_track():
    f = _rec_form()
    sid = recurring.track(_con(), f["vendor"], f["period"], f["every_days"], f["category"], f["amount"],
                          expense_id=request.form.get("expense_id", type=int),
                          tx_id=request.form.get("tx_id", type=int), next_due=f["next_due"])
    if sid:
        logger.info("tracking subscription %s (%s)", sid, f["period"])
        flash(f"Tracking {f['vendor'] or 'it'} as {recurring.label(dict(f, every_days=f['every_days'] or 30)).lower()}.")
    else:
        flash("Enter who you pay and how often.")
    return _back()


@pages.post("/expenses/recurring/<int:sub_id>/<action>")
def recurring_action(sub_id, action):
    c = _con()
    if action == "edit":
        f = {k: v for k, v in _rec_form().items() if v is not None}
        if f.get("period") != "custom":
            f["every_days"] = None
        recurring.update(c, sub_id, f)
        flash("Saved.")
    elif action == "cancel":
        recurring.cancel(c, sub_id)
        flash("Marked as cancelled. You'll see a warning if it charges you again.")
    elif action == "reactivate":
        recurring.reactivate(c, sub_id)
    elif action == "delete":
        recurring.delete(c, sub_id)
        flash("No longer tracked. Its receipts stay in your expenses.")
    return _back()


@pages.post("/expenses/recurring/dismiss")
def recurring_dismiss():
    recurring.dismiss(_con(), request.form.get("key", ""))
    return _back()


@pages.get("/expenses/budget")
def budget_page():
    c = _con()
    recurring.refresh(c)
    this = budget.month_of(date.today())
    month = request.args.get("month", this)
    month = month if re.fullmatch(r"\d{4}-\d{2}", month) and month <= this else this
    items = ledger.rows(c)
    return render_template("expense_budget.html", tab="budget", counts=ledger.counts(items),
                           rec=recurring.totals(recurring.subscriptions(c)), m=budget.month_view(c, month),
                           plan=budget.budgets(c, this), hist=budget.history(c, width=480, height=220), this=this,
                           prev=budget.shift(month, -1), next=budget.shift(month, 1) if month < this else None,
                           **_dialogs())


@pages.post("/expenses/budget")
def budget_save():
    amounts = {}
    for cat in EXPENSE_CATEGORIES:
        if f"b_{cat}" in request.form:
            amounts[cat] = _amount(f"b_{cat}")
    budget.set_budgets(_con(), amounts)
    logger.info("budgets saved")
    flash("Budgets saved from this month on.")
    return _back()


@pages.get("/expenses/export.csv")
def export():
    month = request.args.get("month")
    month = month if month and re.fullmatch(r"\d{4}-\d{2}", month) else None
    return Response(budget.export_csv(_con(), month), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=botting-expenses{'-' + month if month else ''}.csv"})


def _month_arg():
    this = budget.month_of(date.today())
    month = request.args.get("month", this)
    return (month if re.fullmatch(r"\d{4}-\d{2}", month) and month <= this else this), this


@pages.get("/expenses/profit")
def profit_page():
    c = _con()
    recurring.refresh(c)
    sales.link_orders(c)
    month, this = _month_arg()
    items = ledger.rows(c)
    return render_template("expense_profit.html", tab="profit", counts=ledger.counts(items),
                           rec=recurring.totals(recurring.subscriptions(c)), p=sales.month_numbers(c, month),
                           rows=sales.rows(c, month), hist=sales.history(c, width=380), deposits=sales.deposits(c),
                           unsold=sales.unsold(c, 8), platforms=sales.platforms(c), custom=sales.custom_platforms(c),
                           orders=sales.order_choices(c), prev=budget.shift(month, -1),
                           next=budget.shift(month, 1) if month < this else None, **_dialogs())


@pages.post("/expenses/profit/goal")
def profit_goal():
    amount = _amount("goal")
    sales.set_setting(_con(), "monthly_goal", str(amount if amount and amount > 0 else 0))
    flash("Monthly profit goal saved.")
    return _back()


def _sale_form():
    order = request.form.get("order_id", type=int)
    return {"platform": _text("platform"), "item": _text("item"), "size": _text("size"),
            "sale_price": _amount("sale_price"), "fees": _amount("fees"), "payout": _amount("payout"),
            "date": _text("date"), "cost": _amount("cost"), "order_id": order or None}


@pages.post("/expenses/sales/new")
def sale_new():
    f = _sale_form()
    if not f["platform"] or (f["payout"] is None and f["sale_price"] is None):
        flash("Enter where you sold it and what you got for it.")
        return _back()
    c = _con()
    if f["order_id"] and f["cost"] is None:
        f["cost"] = c.execute("SELECT total FROM tracked_orders WHERE id = ?", (f["order_id"],)).fetchone()[0]
    sales.add(c, f)
    logger.info("sale added by hand")
    flash(f"Added your {f['platform']} sale.")
    return _back()


@pages.post("/expenses/sales/<int:sale_id>/<action>")
def sale_action(sale_id, action):
    c = _con()
    if action == "edit":
        f = _sale_form()
        if f["order_id"] and f["cost"] is None:
            f["cost"] = c.execute("SELECT total FROM tracked_orders WHERE id = ?", (f["order_id"],)).fetchone()[0]
        sales.update(c, sale_id, {k: v for k, v in f.items() if v is not None or k in ("order_id", "cost")})
        flash("Sale saved.")
    elif action == "count":
        sales.update(c, sale_id, {"counted": 1 if request.form.get("counted") == "1" else 0})
    elif action == "delete":
        sales.delete(c, sale_id)
        flash("Sale deleted.")
    return _back()


@pages.post("/expenses/sales/deposit/<int:tx_id>")
def sale_deposit(tx_id):
    if sales.add_deposit(_con(), tx_id, _text("platform")):
        flash("Deposit added to your sales.")
    return _back()


@pages.post("/expenses/platforms")
def platforms_save():
    c = _con()
    if request.form.get("remove"):
        sales.remove_platform(c, request.form["remove"])
    else:
        name = sales.add_platform(c, request.form.get("name"))
        if name:
            flash(f"Added {name}. The next sync also looks for its sale and payout emails.")
    return _back()


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
