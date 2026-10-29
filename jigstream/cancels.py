"""Mark an order cancelled from any one of the three things that say it was: the store's cancel email, the bot's
decline or cancel webhook, or the card charge for it coming back as a refund. Where several agree, all of them are
kept as the reason (tracked_orders.cancel_why), so the order sheet can say why it was cancelled.

Only orders that haven't shipped are touched (a refund after it shipped is a return), and a status set by hand is
never changed.
"""
import json
from datetime import timedelta

from . import bank, stuck

# Statuses a cancel can replace. Once it shipped, only the emails decide (orderstore._save_order).
OPEN = ("", "unknown", "ordered", "processing", "exception", "cancelled")
# The card refund can follow the order by up to two months (Pokemon Center cancels late; Target refunds at once).
REFUND_DAYS = stuck.REFUND_DAYS
# A bot decline with no order number in it: same site and account email, this close to the order email.
DECLINE_NEAR = timedelta(days=1)
EMAIL, BOT, CARD = "cancel email", "bot webhook", "card refund"


def _number(s):
    n = stuck._number(s)
    return n if len(n) >= 5 else ""


def _email_signals(con, orders):
    from types import SimpleNamespace
    from .extract import cancel_signal
    out = {}
    subjects = {}
    for o in orders:
        for mid in json.loads(o["message_ids"] or "[]"):
            subjects.setdefault(mid, []).append(o["id"])
    if not subjects:
        return out
    for mid, subject in con.execute("SELECT message_id, subject FROM emails"):
        if mid in subjects and cancel_signal(SimpleNamespace(subject=subject or "", body="")) == "cancelled":
            for oid in subjects[mid]:
                out[oid] = EMAIL
    return out


def _bot_signals(con, orders):
    """The bot's decline or cancel for an order: by order number (Hayha declines carry it, and are told apart by
    their red embed when parsed), else a decline with a cancel reason on the same site and account email right
    around the order, when exactly one order fits it."""
    by_number = {}
    for c in con.execute("SELECT bot, order_number FROM checkouts WHERE status = 'decline'"):
        if _number(c["order_number"]):
            by_number.setdefault(_number(c["order_number"]), c["bot"] or "bot")
    for u in con.execute("SELECT bot, order_number FROM checkout_updates WHERE verdict = 'cancelled'"):
        if _number(u["order_number"]):
            by_number.setdefault(_number(u["order_number"]), u["bot"] or "bot")
    out = {}
    for o in orders:
        bot = by_number.get(_number(o["order_number"]))
        if bot:
            out[o["id"]] = f"{bot} decline"
    loose = [dict(c) for c in con.execute(
        "SELECT * FROM checkouts WHERE status = 'decline' AND COALESCE(reason, '') != ''"
        " AND COALESCE(order_number, '') = '' AND COALESCE(email, '') != ''")]
    for c in loose:
        t, email = stuck.when(c["ts"]), _norm(c["email"])
        fits = [o for o in orders if email and email == _norm(o["recipient_email"])
                and stuck.same_site(c["site"], o["retailer"]) and t and stuck._order_time(o)
                and abs(stuck._order_time(o) - t) <= DECLINE_NEAR]
        if len(fits) == 1:
            out.setdefault(fits[0]["id"], f"{c['bot'] or 'bot'} decline")
    return out


def _norm(email):
    from .profilematch import norm_email
    return norm_email(email)


def _card_signals(con, orders):
    """The order's card charge given back in full: the charge matched to the order (Bank page) and a refund of the
    same amount on the same card from that store after it. An order with no matched charge takes a refund of its
    exact total from that store when that refund fits no other open order and the order fits no other refund."""
    bank.ensure(con)
    refunds = [dict(r) for r in con.execute(
        "SELECT * FROM bank_transactions WHERE amount < 0 AND COALESCE(match_how, '') != 'ignored'"
        " AND COALESCE(match_kind, '') != 'expense'")]
    if not refunds:
        return {}
    by_id = {o["id"]: o for o in orders}
    used, out = set(), {}
    charges = [dict(t) for t in con.execute(
        "SELECT * FROM bank_transactions WHERE amount > 0 AND match_kind = 'order' ORDER BY date")]
    charged = {t["match_id"] for t in charges}
    for tx in charges:
        o = by_id.get(tx["match_id"])
        day = bank._day(tx["date"])
        if o is None or not day:
            continue
        fits = [r for r in refunds if r["id"] not in used and r["account_id"] == tx["account_id"]
                and round(-r["amount"], 2) == round(tx["amount"], 2) and bank._day(r["date"])
                and day - timedelta(days=1) <= bank._day(r["date"]) <= day + timedelta(days=REFUND_DAYS)
                and (bank.names_agree(r["description"], o["retailer"])
                     or bank._squash(tx["description"])[:8] == (bank._squash(r["description"])[:8] or None))]
        if fits:
            r = min(fits, key=lambda r: r["date"])
            used.add(r["id"])
            out[o["id"]] = CARD
    loose = [o for o in orders if o["id"] not in charged and o["total"]]
    fit = {}
    for o in loose:
        start = bank._day(o["order_date"]) or bank._day(o["last_email_date"])
        if not start:
            continue
        for r in refunds:
            day = bank._day(r["date"])
            if r["id"] in used or not day or round(-r["amount"], 2) != round(o["total"], 2):
                continue
            if start - timedelta(days=1) <= day <= start + timedelta(days=REFUND_DAYS) \
                    and bank.names_agree(r["description"], o["retailer"]):
                fit.setdefault(o["id"], []).append(r["id"])
    takers = {}
    for oid, rids in fit.items():
        for rid in rids:
            takers.setdefault(rid, []).append(oid)
    for oid, rids in fit.items():
        if len(rids) == 1 and len(takers[rids[0]]) == 1:
            out[oid] = CARD
    return out


def reconcile(con):
    """Cancel every open order that one of the three signals says was cancelled, and note the reasons on every
    cancelled order they explain. Returns how many orders were newly marked cancelled."""
    orders = [dict(o) for o in con.execute("SELECT * FROM tracked_orders WHERE COALESCE(status_manual, 0) = 0")
              if (o["shipping_status"] or "") in OPEN]
    if not orders:
        return 0
    found = {}
    for signals in (_email_signals(con, orders), _bot_signals(con, orders), _card_signals(con, orders)):
        for oid, why in signals.items():
            found.setdefault(oid, []).append(why)
    marked = 0
    for o in orders:
        why = found.get(o["id"])
        if not why:
            continue
        why = json.dumps(why)
        if o["shipping_status"] != "cancelled":
            con.execute("UPDATE tracked_orders SET shipping_status = 'cancelled', cancel_why = ? WHERE id = ?",
                        (why, o["id"]))
            marked += 1
        elif o.get("cancel_why") != why:
            con.execute("UPDATE tracked_orders SET cancel_why = ? WHERE id = ?", (why, o["id"]))
    con.commit()
    return marked


def reasons(order):
    """The order's cancel reasons as words, for the order sheet."""
    try:
        return json.loads(order.get("cancel_why") or "[]")
    except ValueError:
        return []
