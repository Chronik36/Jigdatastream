"""The Expenses area: Overview and Ledger tabs, plus adding and editing botting expenses on the desktop."""
import json
import logging
import re
import threading
from datetime import date

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for

from . import bank, budget, db, inbox, ledger, recurring, sales
from .config import Config
from .llm import from_config as llm_for
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
        filled += bank.fill_amounts_from_charges(db.connect(path))  # the rest from the card charges that paid them
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
    bank.fill_amounts_from_charges(c)
    recurring.refresh(c)
    items, subs = ledger.rows(c), recurring.subscriptions(c)
    return render_template("expenses.html", tab="overview", o=ledger.overview(c), chart=ledger.month_chart(c),
                           counts=ledger.counts(items), recent=[r for r in items if r["kind"] == "expense"][:8], amounts=_amounts,
                           missing=sum(1 for r in items if r["kind"] == "expense" and r["amount"] is None),
                           rec=recurring.totals(subs), soon=recurring.upcoming(subs)[:6], sync=_sync(),
                           month=budget.month_view(c, budget.month_of(date.today())),
                           prev_month=budget.shift(budget.month_of(date.today()), -1),
                           profit=sales.month_numbers(c, budget.month_of(date.today())),
                           **_dialogs())


@pages.get("/expenses/ledger")
def ledger_page():
    c = _con()
    bank.fill_amounts_from_charges(c)
    items = ledger.rows(c)
    show = request.args.get("show", "all")
    show = show if show in ledger.FILTERS or show == "look" else "all"  # "look": every kind that needs a look
    months = sorted({(r["date"] or "")[:7] for r in items if r["date"]}, reverse=True)
    first, last = ledger.coverage(c)
    return render_template("expense_ledger.html", tab="ledger", rows=[r for r in items if show == "all"
                                                                      or r["state"] == show
                                                                      or (show == "look" and r["state"] in ledger.NEEDS_LOOK)],
                           month=request.args.get("month", ""),
                           counts=ledger.counts(items), show=show, filters=ledger.FILTERS, states=ledger.STATES,
                           months=months, first=first, last=last, methods=ledger.PAY_METHODS,
                           colors=ledger.CATEGORY_COLORS, subs={s["id"]: s for s in recurring.subscriptions(c)},
                           **_dialogs())


def _dialogs():
    """What the Add expense and Repeats dialogs need."""
    return {"categories": EXPENSE_CATEGORIES, "periods": BILLING_PERIODS, "rec_periods": recurring.PERIODS,
            "labels": inbox.LABELS}


@pages.get("/expenses/recurring")
def recurring_page():
    c = _con()
    recurring.refresh(c)
    items, subs = ledger.rows(c), recurring.subscriptions(c)
    totals = recurring.totals(subs)
    # The cards open their subscriptions: ?show=soon (renewing in 30 days) or alerts (needs a look).
    show = request.args.get("show", "")
    if show == "soon":
        soon = {s["id"] for _, s in recurring.upcoming(subs, date.today(), 30)}
        subs = [s for s in subs if s["id"] in soon]
    elif show == "alerts":
        subs = [s for s in subs if s["state"] in ("bad", "warn")]
    else:
        show = ""
    return render_template("expense_recurring.html", tab="recurring", counts=ledger.counts(items), subs=subs,
                           totals=totals, show=show, weeks=recurring.calendar_weeks(subs),
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
    sales.match_payouts(c)
    month, this = _month_arg()
    items = ledger.rows(c)
    return render_template("expense_profit.html", tab="profit", counts=ledger.counts(items),
                           rec=recurring.totals(recurring.subscriptions(c)), p=sales.month_numbers(c, month),
                           rows=sales.rows(c, month), hist=sales.history(c, width=380), deposits=sales.deposits(c),
                           unsold=sales.unsold(c, 8), platforms=sales.platforms(c), custom=sales.custom_platforms(c),
                           orders=sales.order_choices(c), prev=budget.shift(month, -1),
                           fee_pcts={pl.lower(): v for pl in sales.platforms(c) if (v := sales.fee_pct(c, pl)) is not None},
                           find_state=getattr(current_app, "find_state", None), month=month,
                           next=budget.shift(month, 1) if month < this else None, **_dialogs())


@pages.post("/expenses/profit/goal")
def profit_goal():
    amount = _amount("goal")
    sales.set_setting(_con(), "monthly_goal", str(amount if amount and amount > 0 else 0))
    flash("Monthly profit goal saved.")
    return _back()


def _sale_form():
    """The sale dialog. Fees and payout fill in from the fee percent when left blank, and the percent is
    remembered for that platform."""
    order = request.form.get("order_id", type=int)
    pct = _amount("fee_pct")
    pct = pct if pct is not None and 0 <= pct < 100 else None
    price, fees, payout = sales.amounts(_amount("sale_price"), pct, _amount("fees"), _amount("payout"))
    if pct is not None and _text("platform"):
        sales.set_setting(_con(), f"fee_pct:{sales._key(_text('platform'))}", str(pct))
    return {"platform": _text("platform"), "item": _text("item"), "size": _text("size"),
            "sale_price": price, "fees": fees, "payout": payout,
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


@pages.post("/expenses/sales/find")
def sale_find():
    if current_app.start_find_sales():
        flash("Looking through the past year of emails from Tradepost, StockX, eBay and your other selling apps. "
              "Sales show up here as they're found.")
    else:
        flash("A sync is running or no inbox is set up; try again when it's done.")
    return _back()


@pages.post("/expenses/sales/<int:sale_id>/<action>")
def sale_action(sale_id, action):
    c = _con()
    if action == "edit":
        f = _sale_form()
        if f["order_id"] and f["cost"] is None:
            f["cost"] = c.execute("SELECT total FROM tracked_orders WHERE id = ?", (f["order_id"],)).fetchone()[0]
        before = c.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
        sales.update(c, sale_id, {k: v for k, v in f.items() if v is not None or k in ("order_id", "cost")})
        config = Config.from_env()
        teach = before and before["source"] == "email" and config.accounts and \
            any(f.get(k) is not None and f.get(k) != before[k] for k in ("platform", "item", "sale_price", "fees", "payout"))
        if teach:
            threading.Thread(target=teach_sale, args=(config, sale_id, current_app.config.get("DB_PATH")),
                             daemon=True).start()
        flash("Sale saved." + (" The AI re-reads its email and learns from your fix." if teach else ""))
    elif action == "email":
        # One of the emails around a payout is a sale (or something else): it's re-read as that, so the AI learns.
        label, what = _what_it_is()
        mid = request.form.get("message_id", "")
        if label and any(e["message_id"] == mid for e in sales.nearby_emails(c, sale_id)):
            taught = _relabel_email(c, mid, label, what)
            flash(f"That email is now \"{what or inbox.LABELS[label][0]}\"."
                  + (" The AI re-reads it; a sale shows up here and is matched to its payout." if taught else ""))
        else:
            flash("Pick what that email is.")
    elif action == "is":
        label, what = _what_it_is()
        row = c.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
        if not row or not label or label == "sale":
            flash("Pick what it really is.")
            return _back()
        sales.delete(c, sale_id)
        taught = _relabel_email(c, row["message_id"], label, what)
        flash(f"That isn't a sale, it's \"{what or inbox.LABELS[label][0]}\"."
              + ({"order": " It moves to Orders.", "botting_expense": " It moves to Expenses."}.get(
                  inbox.LABELS[label][1]["kind"], "") + " The AI re-reads its email and learns from it."
                 if taught else " Removed from your sales."))
    elif action == "count":
        sales.update(c, sale_id, {"counted": 1 if request.form.get("counted") == "1" else 0})
    elif action == "delete":
        sales.delete(c, sale_id)
        flash("Sale deleted.")
    return _back()


@pages.get("/expenses/sales/<int:sale_id>/emails")
def sale_emails(sale_id):
    """The emails from a sale's platform around it, for the sale window's "Emails around it" list."""
    return {"emails": [{k: e[k] for k in ("message_id", "account", "sender", "subject", "date", "kind")}
                       | {"sale": bool(e["sale"]), "item": (e["sale"] or {}).get("item")}
                       for e in sales.nearby_emails(_con(), sale_id)]}


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
    """Change anything about an expense. A fix to who you paid, the price, the category or how often it's billed
    teaches the AI from its email, and "Repeats" starts tracking it as a subscription."""
    c = _con()
    store = Store(c)
    e = store.expense(expense_id)
    if e:
        f = _form()
        f["vendor"] = f["vendor"] or e["vendor"]
        if f["amount"] is None:
            f.pop("amount")
        repeats = request.form.get("repeats", "")
        if repeats in BILLING_PERIODS:
            f["billing_period"] = repeats
        changed = any(f.get(k) != e.get(k) for k in ("vendor", "amount", "category", "billing_period") if k in f)
        store.update_expense(expense_id, f)
        tracked = repeats in recurring.PERIODS and not e.get("subscription_id") and \
            recurring.track(c, f["vendor"], repeats, category=f["category"], amount=f.get("amount", e["amount"]),
                            expense_id=expense_id)
        config = Config.from_env()
        teach = changed and config.accounts and not (e["message_id"] or "").startswith("manual:")
        if teach:
            threading.Thread(target=teach_expense, args=(config, expense_id, current_app.config.get("DB_PATH")),
                             daemon=True).start()
        logger.info("expense %s edited (taught: %s, repeats: %s)", expense_id, bool(teach), repeats or "-")
        flash(f"Saved {f['vendor']}." + (f" Tracked as {recurring.PERIODS[repeats].lower()} on Recurring." if tracked else "")
              + (" The AI re-reads its email and learns from your fix." if teach else ""))
    return _back()


@pages.post("/expenses/<int:expense_id>/refunded")
def expense_refunded(expense_id):
    """Returned or refunded: what came back comes off what it cost (all of it unless an amount is typed)."""
    c = _con()
    e = Store(c).expense(expense_id)
    if e:
        back = None if request.form.get("undo") else (_amount("amount") or e["amount"])
        c.execute("UPDATE expenses SET refunded = ? WHERE id = ?", (back, expense_id))
        if back is None:
            c.execute("UPDATE bank_transactions SET match_kind = NULL, match_id = NULL, match_how = 'manual'"
                      " WHERE match_kind = 'expense_refund' AND match_id = ?", (expense_id,))
        c.commit()
        flash(f"{e['vendor']}: " + (f"{back:.2f} refunded, taken off your spend." if back else "refund removed."))
    return _back()


def _what_it_is():
    """The "This is…" pickers: one of the Inbox labels, or "custom" with your own words for what it is."""
    label, what = request.form.get("label", ""), _text("what")
    if label == "custom":
        return ("other", what) if what else (None, None)
    return (label, None) if label in inbox.LABELS else (None, None)


def _relabel_email(c, message_id, label, what=None):
    """Re-read an email in the background as what you said it is: it moves to Orders, Sales or nowhere, and is
    saved as a training example. False when there's no email to read (typed in by hand, or no inbox set up)."""
    mail = c.execute("SELECT message_id, account, sender, subject, date FROM emails WHERE message_id = ?",
                     (message_id,)).fetchone()
    config = Config.from_env()
    if not mail or not config.accounts:
        return False
    threading.Thread(target=inbox.label_emails, args=(config, Store(_con()), llm_for(config), [dict(mail)], label),
                     kwargs={"what": what}, daemon=True).start()
    return True


MOVED = {"order": " It moves to Orders.", "sale": " It moves to your sales on Profit goal."}


def _expense_is(expense_id, label, what=None):
    c = _con()
    store = Store(c)
    e = store.expense(expense_id)
    if not e:
        return _back()
    c.execute("UPDATE bank_transactions SET match_kind = NULL, match_id = NULL, match_how = NULL"
              " WHERE match_kind = 'expense' AND match_id = ?", (expense_id,))
    store.delete_expense(expense_id)
    taught = _relabel_email(c, e["message_id"], label, what)
    name = what or inbox.LABELS[label][0]
    flash(f"{e['vendor'] or 'That'} isn't an expense, it's \"{name}\"."
          + ((MOVED.get(inbox.LABELS[label][1]["kind"], "") + " The AI re-reads its email and learns from it.")
             if taught else " Removed from Expenses."))
    logger.info("expense %s relabelled %s (email: %s)", expense_id, label, taught)
    return _back()


@pages.post("/expenses/<int:expense_id>/is")
def expense_is(expense_id):
    """"This is…": an expense that is really something else (a sale, an order, an ad, a bank alert, or your own
    words). It leaves Expenses, and its email is re-read as what you said so the AI learns from it."""
    label, what = _what_it_is()
    if not label or label == "expense":
        flash("Pick what it really is.")
        return _back()
    return _expense_is(expense_id, label, what)


@pages.post("/expenses/<int:expense_id>/not-expense")
def not_expense(expense_id):
    """'Not an expense': remove it, and when it came from an email, teach the AI that emails like it aren't
    botting expenses (the email is re-read and saved as a training example, as on the Inbox tab)."""
    return _expense_is(expense_id, "other")


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


# ---------- in-person buys and sales: what no email shows ----------

IN_PERSON = "in person"
STORES = ["Sam's Club", "Costco", "Target", "Walmart", "Best Buy", "GameStop", "Barnes & Noble", "Five Below", "Meijer"]
SELL_PLACES = ["Tradepost", "Card show", "Local shop", "Facebook Marketplace", "Friend / local"]


@pages.get("/expenses/in-person")
def in_person_page():
    c = _con()
    buys = [dict(r) for r in c.execute(
        "SELECT o.*, (SELECT COUNT(*) FROM sales s WHERE s.order_id = o.id) AS sold FROM tracked_orders o"
        " WHERE o.account = ? ORDER BY o.order_date DESC, o.id DESC LIMIT 200", (IN_PERSON,))]
    for b in buys:
        b["items"] = json.loads(b["items"] or "[]")
    sold = [s for s in sales.rows(c) if s["source"] == "in_person"]
    pick = request.args.get("order", type=int)
    picked = next((o for o in sales.order_choices(c, only=pick)), None) if pick else None
    if picked:
        picked["item"] = picked.pop("first", None)
    return render_template("expense_in_person.html", tab="in_person", counts={}, buys=buys, sold=sold, picked=picked,
                           stores=STORES, places=SELL_PLACES + [p for p in sales.platforms(c) if p not in SELL_PLACES],
                           orders=sales.order_choices(c, limit=2000), today=date.today().isoformat(), **_dialogs())


@pages.post("/expenses/in-person/buy")
def in_person_buy():
    f = request.form
    store, total = _text("store"), _amount("total")
    if not store or total is None:
        flash("Enter the store and what you paid.")
        return _back()
    qty = request.form.get("quantity", type=int) or 1
    item = _text("item") or "In-store buy"
    oid = Store(_con()).add_order({"retailer": store, "order_date": _text("date") or date.today().isoformat(),
                                   "items": [{"name": item, "quantity": qty, "price": round(total / qty, 2)}],
                                   "total": total, "shipping_status": "delivered", "account": IN_PERSON})
    bank.auto_match(_con())  # its card charge, if your statements have it, now has an order
    logger.info("in-person buy %s added", oid)
    flash(f"Added your {store} buy. It counts as an order, and as the cost when you sell it.")
    return _back()


@pages.post("/expenses/in-person/sale")
def in_person_sale():
    f = _sale_form()
    f["platform"] = _text("place") or f["platform"]
    if not f["platform"] or (f["payout"] is None and f["sale_price"] is None):
        flash("Enter where you sold it and what you got for it.")
        return _back()
    c = _con()
    if f["payout"] is None:
        f["payout"] = (f["sale_price"] or 0) - (f["fees"] or 0)
    if f["order_id"] and f["cost"] is None:
        f["cost"] = c.execute("SELECT total FROM tracked_orders WHERE id = ?", (f["order_id"],)).fetchone()[0]
    sales.add(c, f, "in_person")
    logger.info("in-person sale added")
    flash(f"Added your {f['platform']} sale.")
    return _back()


# ---------- Review: go through every expense an email gave, one at a time, and fix it ----------

def _review_rows(c, show):
    where = "e.message_id NOT LIKE 'manual:%'" + ("" if show == "all" else " AND COALESCE(e.reviewed, 0) = 0")
    return [dict(r) for r in c.execute(
        "SELECT e.*, m.subject, m.sender, m.date AS email_date, m.account AS email_account FROM expenses e"
        " LEFT JOIN emails m ON m.message_id = e.message_id WHERE " + where +
        " ORDER BY COALESCE(e.date, m.date) DESC, e.id DESC")]


@pages.get("/expenses/review")
def review_page():
    c = _con()
    Store(c)  # the reviewed column on older databases
    show = request.args.get("show", "")
    rows = _review_rows(c, show)
    pick = request.args.get("id", type=int)
    current = next((r for r in rows if r["id"] == pick), None) or \
        (next((r for r in _review_rows(c, "all") if r["id"] == pick), None) if pick else None) or (rows[0] if rows else None)
    done = c.execute("SELECT COUNT(*) FROM expenses WHERE message_id NOT LIKE 'manual:%' AND COALESCE(reviewed, 0) = 1"
                     ).fetchone()[0]
    return render_template("expense_review.html", tab="review", counts={}, rows=rows, e=current, show=show,
                           done=done, **_dialogs())


def teach_expense(config, expense_id, db_path=None):
    """An expense fixed or confirmed on Review becomes a training example: its email re-read by the model, with
    your vendor, amount, category and billing period as the right answer."""
    from .extract import email_prompt, extract_raw
    from .training import few_shot
    store = Store(db.connect(db_path))
    e = store.expense(expense_id)
    row = store.db.execute("SELECT account FROM emails WHERE message_id = ?", (e["message_id"],)).fetchone() if e else None
    if not e or not config.accounts:
        return False
    email = inbox.open_email(config, (row["account"] if row else None) or e.get("account"), e["message_id"])
    if not email:
        return False
    raw, _ = extract_raw(llm_for(config), email, config.max_body_chars, few_shot(store))
    corrected = dict(raw, kind="botting_expense", vendor=e["vendor"], total=e["amount"],
                     expense_category=e["category"], billing_period=e["billing_period"],
                     description=e["description"] or raw.get("description"))
    store.add_inbox_example(email, email_prompt(email, config.max_body_chars), raw, corrected, "expense")
    logger.info("expense %s reviewed: saved as a training example", expense_id)
    return True


def teach_sale(config, sale_id, db_path=None):
    """A sale fixed by hand becomes a training example: its email re-read by the model, with the platform and every
    number the email shows set to yours. Only the example is saved; your sale is left as you typed it."""
    from .extract import email_prompt, extract_raw
    from .training import few_shot
    store = Store(db.connect(db_path))
    s = sales.ensure(store.db).execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if not s or not config.accounts:
        return False
    email = inbox.open_email(config, s["account"], s["message_id"])
    if not email:
        return False
    platforms = sales.platforms(store.db)
    raw, _ = extract_raw(llm_for(config), email, config.max_body_chars, few_shot(store), platforms)
    corrected = dict(raw, kind="sale", **inbox._sale_fix(email, dict(s)))
    corrected.pop("shipping_status", None)
    store.add_inbox_example(email, email_prompt(email, config.max_body_chars, platforms), raw, corrected, "sale")
    logger.info("sale %s fixed by hand: saved as a training example", sale_id)
    return True


@pages.post("/expenses/<int:expense_id>/review")
def review_save(expense_id):
    c = _con()
    store = Store(c)
    e = store.expense(expense_id)
    nxt = request.form.get("next", type=int)
    if e:
        f = _form()
        f["vendor"] = f["vendor"] or e["vendor"]
        if f["amount"] is None:
            f.pop("amount")
        changed = any(f.get(k) != e.get(k) for k in ("vendor", "amount", "category", "billing_period") if k in f)
        store.update_expense(expense_id, dict(f, reviewed=1))
        config = Config.from_env()
        if changed and config.accounts:  # only fixes go back to the AI; a "looks right" just marks it done
            threading.Thread(target=teach_expense, args=(config, expense_id, current_app.config.get("DB_PATH")),
                             daemon=True).start()
        flash(f"Saved {f['vendor']}." + (" The AI learns from your fix." if changed else ""))
    show = request.form.get("show", "")
    return redirect(url_for("expenses.review_page", id=nxt, show=show or None))
