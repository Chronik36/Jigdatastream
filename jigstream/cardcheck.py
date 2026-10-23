"""Card check: how many checkouts really went through, from what each card was charged and refunded.

A bot says "success" as soon as the site takes the order, and cancel emails don't always come. The card doesn't lie:
for each card (Bank page), the charges at a store minus the refunds of those same charges is how many orders went
through there. Charges are grouped by card, store and exact amount, and a group's charges close together in time
are one drop ("run"). The amount says what was bought: an order email with that exact total, or a bot checkout whose
price (times its quantity) plus tax and shipping comes to it.

A single charge is only tied to a single checkout when that is certain:
- the bank gave the charge a time (Plaid sends one for many cards) within LINK_MINUTES of the checkout, closest first;
- the charge paid an order email (Bank page match) that is that checkout's order email; or
- the run has exactly one charge and exactly one checkout it could be.
Otherwise the run shows counts only: "5 charged, 1 refunded, so 4 of the 6 checkouts went through", without saying
which 4. Bank data never leaves the Mac; this only reads the tables the Bank page fills.
"""
import json
import re
from collections import Counter
from datetime import timedelta

from . import bank, parsers, stuck

# A charge may post a day before the checkout's day (time zones) up to two weeks after it (sites that charge when they
# ship, pre-orders).
CHARGE_BEFORE, CHARGE_AFTER = timedelta(days=1), timedelta(days=14)
# Charges of one amount at one store more than this far apart are separate drops.
RUN_GAP = timedelta(days=10)
# A refund of a charge comes after it, up to two months later (Pokemon Center).
REFUND_DAYS = 60
# On top of the bot's price: sales tax (up to about 11%) plus a little rounding, or a flat shipping fee.
TAX, SHIPPING = 0.12, 15.00
# A charge with a time this close to a checkout is that checkout.
LINK_MINUTES = 30
QTY_FIELDS = ["quantity", "qty", "amount of items"]


def _last4(account):
    mask = re.sub(r"\D", "", account.get("mask") or "")
    if len(mask) >= 4:
        return mask[-4:]
    m = re.search(r"(\d{4})\D*$", account.get("name") or "")
    return m.group(1) if m else None


def _quantity(raw):
    try:
        embed = (json.loads(raw or "{}") or {}).get("embed") or {}
        fields = parsers.flatten_embed(embed) if isinstance(embed, dict) else {}
    except (ValueError, AttributeError):
        return 1
    for k in QTY_FIELDS:
        m = re.match(r"\s*(\d{1,2})\b", fields.get(k) or "")
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return 1


def price_fits(amount, price):
    """How far a card charge is from a bot price (0 = exact, smaller is closer), or None when it can't be that price
    with tax and shipping on top."""
    if not price or not amount or amount < price - 0.01:
        return None
    if amount <= price * (1 + TAX) + 0.01 or amount <= price + SHIPPING + 0.01:
        return round((amount - price) / price, 4)
    return None


class Stores:
    """Tells which store a bank description is, out of the sites your bots and order emails name."""

    def __init__(self, names):
        self.names = {}
        for n in names:
            key = stuck.site_key(n)
            if key and len(key) >= 3:
                self.names.setdefault(key, Counter())[n] += 1

    def of(self, description):
        squashed = bank._squash(description)
        hits = [k for k in self.names if k in squashed] or [k for k in self.names
                                                             if bank.names_agree(description, k)]
        if not hits:
            return None, None
        key = max(hits, key=len)  # "pokemoncenter" over "pokemon"
        return key, self.names[key].most_common(1)[0][0]


def _local_day(ts):
    t = stuck.when(ts)
    return t.astimezone().date() if t else None


def _pair_refunds(charges, refunds):
    """Each full refund goes to the charge it gives back: same card, store and amount, on or after the charge (a day
    early at most), nearest first. Returns refunds that gave back no charge here (partial, or the charge is older than
    the statement)."""
    for tx in charges:
        tx["refund"] = None
    loose = []
    for r in sorted(refunds, key=lambda r: r["date"]):
        day = bank._day(r["date"])
        fits = [tx for tx in charges if tx["refund"] is None and tx["account_id"] == r["account_id"]
                and tx["store"] == r["store"] and round(tx["amount"], 2) == round(-r["amount"], 2)
                and day and bank._day(tx["date"])
                and bank._day(tx["date"]) - timedelta(days=1) <= day <= bank._day(tx["date"]) + timedelta(days=REFUND_DAYS)]
        if fits:
            max(fits, key=lambda tx: (tx["date"], tx.get("time") or "", tx["id"]))["refund"] = r
        else:
            loose.append(r)
    return loose


def _runs(charges):
    """Charges -> runs: one card, one store, one amount, close together in time."""
    groups = {}
    for tx in charges:
        groups.setdefault((tx["account_id"], tx["store"], round(tx["amount"], 2)), []).append(tx)
    runs = []
    for (account_id, store, amount), txs in groups.items():
        txs.sort(key=lambda tx: (tx["date"], tx["id"]))
        run = None
        for tx in txs:
            day = bank._day(tx["date"])
            if run is None or day - run["last"] > RUN_GAP:
                run = {"account_id": account_id, "store": store, "store_name": tx["store_name"], "amount": amount,
                       "first": day, "last": day, "charges": []}
                runs.append(run)
            run["charges"].append(tx)
            run["last"] = day
    return runs


def _checkout_fits(run, c, card, cards_of):
    """How well a bot checkout fits a run, smaller is better, or None."""
    if not stuck.same_site(c["site"], run["store"]) and not (
            c["order"] and stuck.same_site(c["order"]["retailer"], run["store"])):
        return None
    profile_card = cards_of.get(c["profile"])
    if card and profile_card and profile_card[-4:] != card:
        return None
    day = _local_day(c["ts"])
    if not day or not (run["first"] - CHARGE_AFTER <= day <= run["last"] + CHARGE_BEFORE):
        return None
    total = (c["order"] or {}).get("total")
    if total and round(total, 2) == run["amount"]:
        fit = 0.0
    else:
        price = bank.parse_amount(c["price"] or "")
        fit = price_fits(run["amount"], abs(price) * c["qty"]) if price else None
        if fit is None:
            return None
    # A card we know beats one we're guessing at; then the price, then the time.
    return (0 if profile_card else 1, fit, abs((run["first"] - day).days))


def _link(run):
    """Tie single charges to single checkouts only where it's certain (see the module notes)."""
    links = {}
    by_order = {c["order"]["id"]: c for c in run["checkouts"] if c["order"]}
    for tx in run["charges"]:
        if tx["match_kind"] == "order" and tx["match_id"] in by_order:
            links[tx["id"]] = (by_order[tx["match_id"]], "its order email")
    pairs = []
    for tx in run["charges"]:
        t = stuck.when(tx.get("time"))
        if tx["id"] in links or not t:
            continue
        for c in run["checkouts"]:
            ct = stuck.when(c["ts"])
            if ct and abs((t - ct).total_seconds()) <= LINK_MINUTES * 60:
                pairs.append((abs((t - ct).total_seconds()), tx["id"], c["id"], c, t >= ct))
    used = {c["id"] for c, _ in links.values()}
    for gap, tid, cid, c, after in sorted(pairs, key=lambda p: p[:3]):
        if tid not in links and cid not in used:
            links[tid] = (c, f"charged {round(gap / 60)} min {'after' if after else 'before'}" if gap >= 60
                          else "charged the same minute")
            used.add(cid)
    rest_tx = [tx for tx in run["charges"] if tx["id"] not in links]
    rest_c = [c for c in run["checkouts"] if c["id"] not in used]
    if len(run["charges"]) == 1 and len(run["checkouts"]) == 1 and rest_tx and rest_c:
        links[rest_tx[0]["id"]] = (rest_c[0], "the only charge and checkout")
    for tx in run["charges"]:
        tx["checkout"], tx["linked_by"] = links.get(tx["id"], (None, None))


def _product(run, orders):
    """What the amount bought: the run's checkouts, else an order email with that exact total at that store."""
    names = Counter(c["product"] for c in run["checkouts"] if c["product"])
    if names:
        return names.most_common(1)[0][0], "bot"
    exact = Counter(_item_names(o["items"]) for o in orders if o["total"] and round(o["total"], 2) == run["amount"]
                    and stuck.same_site(o["retailer"], run["store"]))
    exact.pop("", None)
    if exact:
        return exact.most_common(1)[0][0], "email"
    return None, None


def _item_names(items):
    try:
        return ", ".join(i.get("name") for i in json.loads(items or "[]") if isinstance(i, dict) and i.get("name"))[:120]
    except (ValueError, TypeError):
        return ""


def _guess_from_price(run, checkouts):
    """With no checkout in the run, the product whose bot price fits this amount at this store, any card or date."""
    fits = Counter()
    for c in checkouts:
        price = bank.parse_amount(c["price"] or "")
        if c["product"] and price and stuck.same_site(c["site"], run["store"]) \
                and price_fits(run["amount"], abs(price) * c["qty"]) is not None:
            fits[c["product"]] += 1
    return fits.most_common(1)[0][0] if fits else None


def report(con, rows=None):
    """Per card: runs of charges with refunds, how many went through, the bot checkouts they cover and any certain
    one-to-one links. rows are stuck.cross_reference(con) when the caller already has them."""
    bank.ensure(con)
    accounts = {a["id"]: dict(a, last4=_last4(a)) for a in bank.accounts(con)}
    if not accounts:
        return {"cards": [], "runs": [], "links": {}, "loose": []}
    checkouts = [dict(c) for c in (rows if rows is not None else stuck.cross_reference(con))]
    raw = {r[0]: r[1] for r in con.execute("SELECT id, raw FROM checkouts WHERE status = 'success'")}
    for c in checkouts:
        c["qty"] = _quantity(raw.get(c["id"]))
    orders = [dict(o) for o in con.execute("SELECT id, retailer, items, total FROM tracked_orders"
                                           " WHERE COALESCE(shipping_status, '') != 'not_an_order'")]
    stores = Stores([c["site"] for c in checkouts if c["site"]] + [o["retailer"] for o in orders if o["retailer"]])
    charges, refunds = [], []
    for tx in con.execute("SELECT * FROM bank_transactions WHERE pending = 0 AND COALESCE(match_how, '') != 'ignored'"
                          " AND COALESCE(match_kind, '') != 'expense' ORDER BY date, id"):
        tx = dict(tx)
        tx["store"], tx["store_name"] = stores.of(tx["description"])
        if tx["store"] and tx["amount"]:
            (charges if tx["amount"] > 0 else refunds).append(tx)
    loose = _pair_refunds(charges, refunds)
    runs = _runs(charges)
    cards_of = {r[0]: re.sub(r"\D", "", r[1])[-4:] for r in con.execute("SELECT name, card_last4 FROM profiles")
                if r[1] and re.sub(r"\D", "", r[1])}
    # Each bot checkout goes to the run it fits best, so two cards' runs of one drop don't both count it.
    by_site = {}  # only checkouts at a run's store are tried against it
    for j, c in enumerate(checkouts):
        for name in {c["site"], (c["order"] or {}).get("retailer")} - {None, ""}:
            by_site.setdefault(stuck.site_key(name), set()).add(j)
    near = {}
    pairs = []
    for i, run in enumerate(runs):
        run["checkouts"] = []
        card = accounts.get(run["account_id"], {}).get("last4")
        if run["store"] not in near:
            near[run["store"]] = sorted({j for k, js in by_site.items() if k and (k in run["store"] or run["store"] in k)
                                         for j in js})
        for j in near[run["store"]]:
            c = checkouts[j]
            fit = _checkout_fits(run, c, card, cards_of)
            if fit is not None:
                pairs.append((fit, i, j))
    placed = set()
    for _, i, j in sorted(pairs):
        if j not in placed:
            runs[i]["checkouts"].append(checkouts[j])
            placed.add(j)
    links = {}
    for run in runs:
        run["checkouts"].sort(key=lambda c: c["ts"] or "")
        _link(run)
        n = len(run["charges"])
        run["refunded"] = sum(1 for tx in run["charges"] if tx["refund"])
        run["went_through"] = n - run["refunded"]
        run["bot"] = len(run["checkouts"])
        run["bot_cancelled"] = sum(1 for c in run["checkouts"] if c["state"] == "cancelled")
        run["never_charged"] = max(0, run["bot"] - n)
        run["linked"] = sum(1 for tx in run["charges"] if tx["checkout"])
        run["product"], run["product_from"] = _product(run, orders)
        if not run["product"]:
            run["product"] = _guess_from_price(run, checkouts)
            run["product_from"] = "price" if run["product"] else None
        days = Counter(_local_day(c["ts"]) for c in run["checkouts"] if c["ts"])
        run["drop_day"] = days.most_common(1)[0][0].isoformat() if days else None
        run["card"] = accounts.get(run["account_id"])
        for tx in run["charges"]:
            if tx["checkout"]:
                links[tx["checkout"]["id"]] = {"tx": tx, "by": tx["linked_by"], "refunded": bool(tx["refund"])}
    runs.sort(key=lambda r: (r["last"], r["store"]), reverse=True)
    cards = []
    for a in accounts.values():
        mine = [r for r in runs if r["account_id"] == a["id"]]
        if not mine:
            continue
        cards.append(dict(a, runs=mine, profiles=sorted(p for p, last4 in cards_of.items() if last4 == a["last4"]),
                          **_sum(mine)))
    return {"cards": cards, "runs": runs, "links": links, "loose": loose, "total": _sum(runs)}


def _sum(runs):
    keys = ("went_through", "refunded", "bot", "bot_cancelled", "never_charged", "linked")
    out = {k: sum(r[k] for r in runs) for k in keys}
    out["charged"] = sum(len(r["charges"]) for r in runs)
    out["spent"] = round(sum(r["amount"] * r["went_through"] for r in runs), 2)
    return out


def for_drop(card_report, site, day):
    """What the cards say about one drop (site + day): runs whose checkouts are mostly from that day."""
    mine = [r for r in card_report["runs"] if r["drop_day"] == day and stuck.same_site(r["store"], site)]
    return dict(_sum(mine), runs=mine) if mine else None
