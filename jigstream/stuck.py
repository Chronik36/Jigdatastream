"""Cross-reference successful bot checkouts with order emails, to see which ones stuck.

A bot's webhook says "success" the moment the site takes the order, but many orders get cancelled
afterwards. Each successful checkout is matched to the order emails: first by order number, and when
the webhook has none (or the email doesn't), by the same site within a few days of the checkout and the
same buyer: the account email the bot used is the email's To address (surest), or the same profile. Refunds from your bank or card (Bank page) count too: a refund from that store for that amount,
soon after the checkout, means it was cancelled even when no email said so. A checkout stuck when
neither a cancel email nor a refund followed it.
"""
import re
from functools import lru_cache
from datetime import datetime, timedelta, timezone

from . import bank, profilematch, profiles

CANCELLED = ("cancelled", "refunded")
# A refund posts soon after a cancel (Target refunds almost at once). It may carry tax and shipping on top
# of the price the bot reported, so it can be a little more than that price.
# Pokemon Center can cancel (and refund) a month or more after the order, so refunds are looked for up to 60 days on.
REFUND_DAYS, REFUND_EXTRA = 60, 0.15
# How far an order email's date may sit from the checkout when matching on site and profile.
BEFORE, AFTER = timedelta(days=1), timedelta(days=4)
# A cancelled order with no order date is dated by its last email, the cancel, which can come weeks after the checkout.
LATE_CANCEL = timedelta(days=60)
SITE_VERSION = re.compile(r"(?<=[a-z])[\s_-]*v\d+$")
SITE_FILLER = re.compile(r"^(www\.)|\.(com|net|co|us|ca|co\.uk)$|\s+(us|usa|store|online|inc)$")


@lru_cache(maxsize=50000)  # pure string helpers called for every checkout and order on each page load
def _number(s):
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


@lru_cache(maxsize=20000)
def site_key(s):
    s = (s or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    for _ in range(3):
        s = SITE_FILLER.sub("", s).strip()
    s = SITE_VERSION.sub("", s)  # a bot's module version ("amazonv3", "Walmart V2") is the same store
    return re.sub(r"[^a-z0-9]", "", s)


def same_site(a, b):
    a, b = site_key(a), site_key(b)
    return bool(a and b) and (a in b or b in a)


def when(s):
    """An ISO date or time as an aware UTC datetime, or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _order_time(o):
    # The order date from the email is often just a day; the email time is more precise when it's that day.
    placed, emailed = when(o["order_date"]), when(o["last_email_date"])
    if placed and emailed and placed.date() == emailed.date():
        return emailed
    return placed or emailed


def _same_buyer(c, o):
    """How the checkout's buyer matches the order email: "To email" (the account email the bot used is the
    address the email was sent to), "profile", or None."""
    email = profilematch.norm_email(c["email"])
    if email and email == profilematch.norm_email(o["recipient_email"]):
        return "To email"
    if c["profile"] and o["profile_name"] and c.get("profile_key", c["profile"]) == o["profile_name"]:
        return "profile"
    if email and email == profilematch.norm_email(o["account"]):
        return "inbox"
    return None


def state_of(order):
    if order is None:
        return "no_email"
    status = order["shipping_status"] or "unknown"
    return "cancelled" if status in CANCELLED else "emailed"


def cross_reference(con):
    """Every successful checkout, newest first, with the order email it matched (or None), how it matched,
    and its state: "cancelled" (a cancel or refund email, or a bank refund), "emailed" (order emails, none of them
    a cancel) or "no_email". The last two stuck. A bank refund that matched sits in "refund"."""
    profilematch.match_orders(con)
    orders = [dict(o) for o in con.execute(
        "SELECT * FROM tracked_orders WHERE COALESCE(shipping_status, '') != 'not_an_order'")]
    by_number = {}
    for o in orders:
        if _number(o["order_number"]):
            by_number.setdefault(_number(o["order_number"]), o)
    checkouts = [dict(c) for c in con.execute("SELECT * FROM checkouts WHERE status='success' ORDER BY ts")]
    resolve = profiles.resolver(con)
    for c in checkouts:  # the saved profile it ran on: one bot name can be saved once per retail site
        c["profile_key"] = resolve(c["profile"], c["site"]) if c["profile"] else c["profile"]
    used, out = set(), []
    for c in checkouts:
        o = by_number.get(_number(c["order_number"]))
        c["order"], c["matched_on"] = (o, "order number" + (" and To email" if o and _same_buyer(c, o) == "To email"
                                                              else "")) if o else (None, None)
        if o:
            used.add(o["id"])
    # The rest pair up with order emails for the same site and profile, closest in time first, so two
    # checkouts of the same drop don't both claim one email.
    by_site, times = {}, {o["id"]: _order_time(o) for o in orders}
    for o in orders:
        if o["id"] not in used:
            by_site.setdefault(site_key(o["retailer"]), []).append(o)
    # Worked out once per order and per site rather than for every checkout/order pair.
    facts = {o["id"]: (_number(o["order_number"]), times[o["id"]],
                       LATE_CANCEL if not o["order_date"] and state_of(o) == "cancelled" else AFTER) for o in orders}
    near_site = {}
    pairs = []
    for i, c in enumerate(checkouts):
        if c["order"]:
            continue
        t, key = when(c["ts"]), site_key(c["site"])
        if key not in near_site:
            near_site[key] = [o for k, group in by_site.items() if key and k and (key in k or k in key) for o in group]
        number = _number(c["order_number"])
        for o in near_site[key]:
            onumber, ot, after = facts[o["id"]]
            # Both sides have numbers that differ: that's another order.
            if number and onumber:
                continue
            if t and ot and not (t - BEFORE <= ot <= t + after):
                continue
            buyer = _same_buyer(c, o)
            if not buyer:
                continue
            # The To address is the surest sign it's this checkout's order; then the profile, then the inbox.
            rank = ("To email", "profile", "inbox").index(buyer)
            pairs.append(((rank, abs((ot - t).total_seconds()) if t and ot else float("inf")), i, o["id"], o, buyer))
    taken = set()
    for _, i, oid, o, buyer in sorted(pairs, key=lambda p: p[:3]):
        if i in taken or oid in used:
            continue
        checkouts[i]["order"], checkouts[i]["matched_on"] = o, f"same site and {buyer}"
        taken.add(i)
        used.add(oid)
    _match_refunds(con, checkouts)
    _match_bot_updates(con, checkouts)
    verdicts = {r[0]: (r[1], r[2]) for r in con.execute("SELECT checkout_id, verdict, set_at FROM checkout_verdicts")}
    for c in checkouts:
        c["state"] = "cancelled" if c["refund"] else state_of(c["order"])
        # The bot's own follow-up: a cancel always counts; "good" confirms it unless an email or refund cancelled it.
        if c["bot_update"]:
            c["state"] = "cancelled" if c["bot_update"]["verdict"] == "cancelled" or c["state"] == "cancelled" \
                else "emailed"
        # Set by hand: "good" counts as a confirmed order, "cancelled" as cancelled, "not_order" as no checkout at all
        # (a test or a duplicate webhook): it's left out of every count.
        c["verdict"] = verdicts.get(c["id"])
        if c["verdict"] and c["verdict"][0] == "good" and c["state"] == "cancelled" and _cancelled_after(c, c["verdict"][1]):
            c["verdict"] = None  # marked good, but a cancel email or refund came in since: the cancel wins
        c["verdict"] = c["verdict"] and c["verdict"][0]
        if c["verdict"]:
            c["state"] = {"good": "emailed", "cancelled": "cancelled", "not_order": "not_order"}[c["verdict"]]
        out.append(c)
    out.sort(key=lambda c: c["ts"] or "", reverse=True)
    return out


def _match_bot_updates(con, checkouts):
    """Give each checkout the bot's latest follow-up on it (Valor posts whether the order stuck or got cancelled):
    by order number, else the same bot, site (Valor's follow-ups name none) and profile or account email after the
    checkout, nearest checkout first."""
    for c in checkouts:
        c["bot_update"] = None
    updates = [dict(u) for u in con.execute("SELECT * FROM checkout_updates ORDER BY ts")]
    by_number = {}
    for i, c in enumerate(checkouts):
        if _number(c["order_number"]):
            by_number.setdefault(_number(c["order_number"]), i)
    for u in updates:
        i = by_number.get(_number(u["order_number"]))
        if i is None and not _number(u["order_number"]):
            t = when(u["ts"])
            near = [(when(c["ts"]), j) for j, c in enumerate(checkouts)
                    if c["bot"] == u["bot"] and (not u["site"] or same_site(c["site"], u["site"]))
                    and ((u["profile"] and c["profile"] == u["profile"])
                         or (u["email"] and profilematch.norm_email(c["email"]) == profilematch.norm_email(u["email"])))]
            near = [(ct, j) for ct, j in near if not (t and ct) or ct - BEFORE <= t <= ct + LATE_CANCEL]
            # Prefer a checkout that has no order number either and no follow-up yet, then the latest one.
            i = max(near, key=lambda p: (not _number(checkouts[p[1]]["order_number"]),
                                         not checkouts[p[1]]["bot_update"], p[0] or when("1970-01-01")))[1] if near else None
        if i is not None:
            checkouts[i]["bot_update"] = u  # updates are in time order, so the latest one wins


def _cancelled_after(c, set_at):
    """Whether the cancel email, bank refund or bot follow-up that cancels this checkout came after set_at."""
    mark = when(set_at)
    update = c.get("bot_update")
    if update and update["verdict"] == "cancelled" and mark and when(update["ts"]) and when(update["ts"]) > mark:
        return True
    if c["refund"]:
        day = when(c["refund"]["date"])
        return bool(mark and day and day.date() > mark.date())
    news = when((c["order"] or {}).get("last_email_date"))
    return bool(mark and news and news > mark)


def _cards(con):
    """profile name -> card last 4, and bank account id -> its last 4, where known."""
    cards = {r[0]: r[1] for r in con.execute("SELECT name, card_last4 FROM profiles") if r[1]}
    accounts = {r[0]: r[1] for r in con.execute("SELECT id, mask FROM bank_accounts") if r[1]}
    return cards, accounts


def _refund_fits(c, tx, price, profile_cards, account_cards):
    """How far off this refund is for this checkout (smaller is closer), or None when it can't be its refund."""
    if not bank.names_agree(tx["description"], c["site"]) and not (
            c["order"] and bank.names_agree(tx["description"], c["order"]["retailer"])):
        return None
    card, account = profile_cards.get(c.get("profile_key", c["profile"])), account_cards.get(tx["account_id"])
    if card and account and card[-4:] != account[-4:]:
        return None
    t, day = when(c["ts"]), bank._day(tx["date"])
    if t and day and not (t.date() - timedelta(days=1) <= day <= t.date() + timedelta(days=REFUND_DAYS)):
        return None
    amount = round(-tx["amount"], 2)
    if not price or not (price - 0.01 <= amount <= price * (1 + REFUND_EXTRA) + 0.01):
        return None
    return (amount - price, (day - t.date()).days if t and day else REFUND_DAYS)


def _match_refunds(con, checkouts):
    """Give each checkout the bank refund that followed it, if any: same store, the order total (or the price
    the bot reported), on the card the profile uses. Closest first, one refund per checkout."""
    for c in checkouts:
        c["refund"] = None
    bank.ensure(con)
    refunds = [dict(r) for r in con.execute("SELECT * FROM bank_transactions WHERE amount < 0"
                                            " AND COALESCE(match_how, '') != 'ignored'")]
    if not refunds:
        return
    profile_cards, account_cards = _cards(con)
    pairs = []
    for i, c in enumerate(checkouts):
        if c["order"] and (c["order"]["shipping_status"] or "") in ("delivered",):
            continue  # it arrived; a later refund is a return, not a cancel
        price = (c["order"] or {}).get("total") or bank.parse_amount(c["price"])
        price = round(abs(price), 2) if price else None
        for tx in refunds:
            fit = _refund_fits(c, tx, price, profile_cards, account_cards)
            if fit:
                pairs.append((fit, i, tx["id"], tx))
    taken, used = set(), set()
    for _, i, tid, tx in sorted(pairs, key=lambda p: p[:3]):
        if i not in taken and tid not in used:
            checkouts[i]["refund"] = tx
            taken.add(i)
            used.add(tid)


def counts(rows):
    n = {"all": 0, "emailed": 0, "cancelled": 0, "no_email": 0, "not_order": 0}
    for r in rows:
        n[r["state"]] += 1
    n["all"] = len(rows) - n["not_order"]
    n["stuck"] = n["emailed"] + n["no_email"]
    return n


def _by_buyer(items, profile_of, emails_of):
    index = {}
    for x in items:
        if profile_of(x):
            index.setdefault(("p", profile_of(x)), []).append(x)
        for e in {profilematch.norm_email(e) for e in emails_of(x)} - {""}:
            index.setdefault(("e", e), []).append(x)
    return index


def _lookup(index, d):
    keys = [("p", d["profile"])] if d["profile"] else []
    if profilematch.norm_email(d["email"]):
        keys.append(("e", profilematch.norm_email(d["email"])))
    found = {}
    for k in keys:
        for x in index.get(k, ()):
            found[id(x)] = x
    return list(found.values())


def decline_history(con, rows=None):
    """For each declined checkout: what happened earlier on the same profile or account email, from the webhooks
    (successes, how many stuck or got cancelled, other declines) and the order emails. checkout id -> summary."""
    rows = [c for c in (rows if rows is not None else cross_reference(con)) if c["state"] != "not_order"]
    orders = [dict(o) for o in con.execute(
        "SELECT id, retailer, order_date, last_email_date, profile_name, recipient_email, account"
        " FROM tracked_orders WHERE COALESCE(shipping_status, '') != 'not_an_order'")]
    declines = [dict(r) for r in con.execute("SELECT * FROM checkouts WHERE status = 'decline'")]
    wins_by = _by_buyer(rows, lambda c: c["profile"], lambda c: [c["email"]])
    declines_by = _by_buyer(declines, lambda c: c["profile"], lambda c: [c["email"]])
    mails_by = _by_buyer(orders, lambda o: o["profile_name"], lambda o: [o["recipient_email"]])
    out = {}
    for d in declines:
        before = lambda ts: not d["ts"] or bool(ts) and ts < d["ts"]
        wins = sorted((c for c in _lookup(wins_by, d) if before(c["ts"])), key=lambda c: c["ts"])
        out[d["id"]] = {
            "checkouts": len(wins),
            "stuck": sum(c["state"] != "cancelled" for c in wins),
            "cancelled": sum(c["state"] == "cancelled" for c in wins),
            "declines": sum(1 for x in _lookup(declines_by, d) if x["id"] != d["id"] and before(x["ts"])),
            "orders": sum(1 for o in _lookup(mails_by, d) if before(o["order_date"] or o["last_email_date"])),
            "last": wins[-1] if wins else None,
        }
    return out
