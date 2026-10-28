"""Drop results: which proxies and profiles really check out.

Every checkout webhook is grouped by retail site, then by the day of the drop, with its proxy group, proxy and profile.
Only real checkouts count as wins: a decline (a red Hayha embed, or any "Cancel Reason") never does, and neither does a
success that a cancel email or a bank refund caught later (the Stuck matching in stuck.py). So the results are only as
good as the cancel detection: a cancel that never reached a synced inbox still counts as a win.

A proxy can carry its login ("host:port:user:pass", "user:pass@host:port"). Only the host and port are ever kept for
display; the user name and password are dropped here, before anything reaches a page.
"""
import json
import re
from collections import Counter
from datetime import datetime, timezone

from . import address as A
from . import cardcheck, parsers, profilematch, profiles, stuck

GROUP_FIELDS = ["proxy group", "proxy list", "proxylist", "proxy profile", "proxy set"]
PROXY_FIELDS = ["proxy", "proxies", "proxy used", "ip"]
NO_PROXY = {"", "-", "none", "n/a", "na", "null", "local", "localhost", "no proxy", "no proxies", "off", "false"}
EMBEDDED = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9-]+(?:\.[a-z0-9-]+)+):(\d{2,5})(?!\d)", re.I)
HOST = re.compile(r"^(\[[0-9a-f:]+\]|[a-z0-9.\-]+)(?::(\d{1,5}))?$", re.I)


def proxy_host(value):
    """The host (and port) of a proxy as a bot printed it, without any user name or password. "" when there's none."""
    s = (value or "").strip().strip("<>").strip()
    if s.lower() in NO_PROXY:
        return ""
    if ":" not in s and "@" not in s:  # a proxy list's name, not a proxy: nothing secret in it
        return s[:60]
    if re.search(r"\s", s):  # "Leaf ISP - host:port:user:pass": keep just the host and port
        m = EMBEDDED.search(s)
        return f"{m.group(1).lower()}:{m.group(2)}" if m else "(hidden)"
    s = re.sub(r"^[a-z0-9+]+://", "", s, flags=re.I).split("/")[0]
    if "@" in s:  # user:pass@host:port
        s = s.rsplit("@", 1)[1]
    parts = s.split(":")
    looks_host = lambda p: "." in p or p.lower() == "localhost"
    if len(parts) >= 3:
        if parts[1].isdigit() and looks_host(parts[0]):  # host:port:user:pass
            s = f"{parts[0]}:{parts[1]}"
        elif parts[-1].isdigit() and looks_host(parts[-2]):  # user:pass:host:port
            s = f"{parts[-2]}:{parts[-1]}"
        else:
            return "(hidden)"
    m = HOST.match(s)
    if not m:
        return "(hidden)"  # something we can't read safely; never show it
    return m.group(1).lower() + (f":{m.group(2)}" if m.group(2) else "")


def proxy_of(raw):
    """(proxy group, proxy host) from a stored checkout's raw webhook JSON."""
    try:
        embed = (json.loads(raw or "{}") or {}).get("embed") or {}
    except (ValueError, AttributeError):
        return "", ""
    fields = parsers.flatten_embed(embed) if isinstance(embed, dict) else {}
    group = next((fields[k] for k in GROUP_FIELDS if fields.get(k)), "")
    proxy = next((fields[k] for k in PROXY_FIELDS if fields.get(k)), "")
    group = "" if group.strip().lower() in NO_PROXY else group.strip()
    # A group name is a label the user typed in the bot, but guard against a proxy pasted there.
    if ":" in group or "@" in group:
        group = proxy_host(group)
    return group[:80], proxy_host(proxy)


def set_verdict(con, checkout_id, verdict):
    """Mark a checkout good, cancelled or not an order by hand; "auto" goes back to what the emails and bank say."""
    if verdict == "auto":
        con.execute("DELETE FROM checkout_verdicts WHERE checkout_id = ?", (checkout_id,))
    else:
        con.execute("INSERT OR REPLACE INTO checkout_verdicts VALUES (?, ?, ?)",
                    (checkout_id, verdict, datetime.now(timezone.utc).isoformat()))
    con.commit()


def day_of(ts):
    t = stuck.when(ts)
    return t.astimezone().date().isoformat() if t else ""


def outcome(c):
    """'confirmed' (Good: an order email and no cancel, a card charge tied to it that wasn't refunded, or marked good
    by hand), 'no_email' (Unsure: bot success, nothing either way yet), 'cancelled' (a cancel email or bank refund
    after the success, or marked cancelled), 'not_order' (marked not an order: counted nowhere), 'decline' or
    'failure'."""
    if c["status"] == "success":
        card = c.get("card")
        if card and c["state"] == "no_email" and not c.get("verdict"):
            return "cancelled" if card["refunded"] else "confirmed"
        return {"emailed": "confirmed"}.get(c["state"], c["state"])
    return "decline" if c["status"] == "decline" else "failure"


def _address_key(line1, zip_):
    base = A.base_line1(line1)
    return f"{base} · {A.zip5(zip_)}" if base and A.zip5(zip_) else base


def _where(line1, zip_, full):
    """An address as (house number + street, ZIP, the full address as it was used)."""
    return {"base": A.base_line1(line1), "zip": A.zip5(zip_), "full": " ".join((full or "").split())}


def profile_addresses(con):
    """profile name -> where it ships: the base street (jig units and prefixes dropped), ZIP and full address."""
    out = {}
    for p in con.execute("SELECT name, ship_address1, ship_address2, ship_city, ship_state, ship_zip,"
                         " bill_address1, bill_address2, bill_city, bill_state, bill_zip FROM profiles"):
        f = p[1:6] if (p[1] or "").strip() else p[6:11]
        w = _where(f[0], f[4], ", ".join(x for x in (" ".join(y for y in f[:2] if y), f[2], " ".join(y for y in f[3:] if y)) if x))
        if w["base"]:
            out[p[0]] = w
    return out


def order_address(order):
    """Where the order email the checkout matched shipped to, when its profile has no address."""
    if not order or not order.get("shipping_address"):
        return None
    text = order["shipping_address"]
    w = _where(text, profilematch.parse_order_address(text)["zip"], text)
    return w if w["base"] else None


def cluster(w):
    """Jigged copies of one address share the house number and ZIP even when the street is spelled a little
    differently ("MAIN ST" / "MAINN ST"), so those count as one place."""
    house = w["base"].split()[0]
    return f"{house}|{w['zip']}" if w["zip"] else w["base"]


def label_cluster(label):
    """The place an address label ("12 MAIN ST · 78701") stands for."""
    base, _, zip_ = (label or "").partition(" · ")
    return cluster({"base": base.strip(), "zip": zip_.strip()}) if base.strip() else ""


def label_addresses(items):
    """Name each checkout's address by its base address (see cluster); the label is the spelling used most."""
    spellings = {}
    for c in items:
        if c.get("where"):
            spellings.setdefault(cluster(c["where"]), Counter())[
                f"{c['where']['base']} · {c['where']['zip']}" if c["where"]["zip"] else c["where"]["base"]] += 1
    for c in items:
        c["address"] = spellings[cluster(c["where"])].most_common(1)[0][0] if c.get("where") else ""
        c["address_full"] = c["where"]["full"] if c.get("where") else ""


def full_addresses(items):
    """Every full address used under the checkouts shown (one base address once it's picked), most used first."""
    used = {}
    for c in items:
        if c.get("address_full") and c["outcome"] != "not_order":
            u = used.setdefault(c["address_full"].upper(), {"full": c["address_full"], "tries": 0, "real": 0,
                                                             "profiles": set()})
            u["tries"] += 1
            u["real"] += c["outcome"] in ("confirmed", "no_email")
            if c["profile"]:
                u["profiles"].add(c["profile"])
    return sorted(({**u, "profiles": sorted(u["profiles"])} for u in used.values()),
                  key=lambda u: (-u["tries"], u["full"]))


def rows(con, matched=None, links=None):
    """Every checkout webhook with its proxy group, proxy, profile, drop day, the card charge tied to it (Card check)
    and outcome."""
    matched = {c["id"]: c for c in (matched if matched is not None else stuck.cross_reference(con))}
    links = links if links is not None else cardcheck.report(con, list(matched.values()))["links"]
    homes = profile_addresses(con)
    resolve = profiles.resolver(con)
    out = []
    # Checkout attempts only: messages BotTrack can't place yet wait on Things to train, and "not a checkout" is out.
    for r in con.execute("SELECT * FROM checkouts WHERE status IN ('success', 'decline', 'failure') ORDER BY ts"):
        c = matched.get(r["id"]) or dict(r, order=None, refund=None, state=None)
        c = dict(c, card=links.get(r["id"]))
        # Shown and grouped by the saved profile ("Profile 1 (Target)" when two sites share a bot's name).
        c["profile"] = c.get("profile_key") or (resolve(r["profile"], r["site"]) if r["profile"] else r["profile"])
        c["proxy_group"], c["proxy"] = proxy_of(r["raw"])
        c.pop("raw", None)
        c["day"], c["outcome"] = day_of(c["ts"]), outcome(c)
        c["product"], c["size"] = shown_product(c["product"]), shown_size(c["size"])  # the stored row is untouched
        c["order_shown"], c["order_link"] = shown_order(c["order_number"])
        c["where"] = homes.get(c["profile"]) or order_address(c["order"])
        out.append(c)
    label_addresses(out)
    return out


MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
PLACEHOLDER = {"default", "default title", "n/a", "na", "none", "-", "one size"}


def shown_product(s):
    """A bot's product as people should read it: no markdown, no list dashes ("- 1x Booster Bundle - Default"),
    no "Default" variant tacked on."""
    parts = []
    for line in re.split(r"\n|,\s*(?=[-•*]\s)", parsers.tidy(s)):
        line = re.sub(r"^[\s\-•*·]+", "", line).strip()
        line = re.sub(r"\s+[-·]\s+(?:default(?: title)?)$", "", line, flags=re.I).strip()
        if line:
            parts.append(line)
    return ", ".join(parts)


def shown_size(s):
    s = parsers.tidy(s)
    return "" if s.lower() in PLACEHOLDER else s


def shown_order(s):
    """(the order number, a link to the order on the store) from what the bot sent, which can be a markdown link."""
    m = MD_LINK.search(s or "")
    return (m.group(1).strip(), m.group(2)) if m else (parsers.tidy(s), None)


def sources(con, checkout_id):
    """Where a checkout on the Drops page came from: the bot's webhook (and its follow-up), the order emails it was
    matched to, emails whose subject carries its order number, and, while it has no order, the store's other emails
    from that day, so the right one can be found and labelled. None when there's no such checkout."""
    c = next((r for r in stuck.cross_reference(con) if r["id"] == checkout_id), None)
    if c is None:
        row = con.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
        if row is None:
            return None
        c = dict(row, order=None, bot_update=None, matched_on=None)
    hook = webhook(c.get("raw"))
    update = c.get("bot_update")
    cols = "message_id, account, sender, subject, date"
    order = c.get("order")
    ids = (order or {}).get("message_ids") or []
    if isinstance(ids, str):
        ids = json.loads(ids or "[]")
    seen = set(ids)
    mails = [dict(r) for r in con.execute(
        f"SELECT {cols} FROM emails WHERE message_id IN ({','.join('?' * len(ids))}) ORDER BY date", ids)] if ids else []
    number, _ = shown_order(c.get("order_number"))
    by_number = []
    if len(stuck._number(number)) >= 5:
        for r in con.execute(f"SELECT {cols} FROM emails WHERE subject LIKE ? ORDER BY date LIMIT 20", (f"%{number}%",)):
            if r["message_id"] not in seen:
                seen.add(r["message_id"])
                by_number.append(dict(r))
    same_day = []
    key, day = stuck.site_key(c.get("site")), day_of(c.get("ts"))
    if not order and key and day:
        word = re.split(r"[^a-z0-9]", key)[0]
        for r in con.execute(f"SELECT {cols} FROM emails WHERE substr(date, 1, 10) BETWEEN date(?, '-1 day') AND"
                             " date(?, '+2 day') AND (lower(sender) LIKE ? OR lower(subject) LIKE ?) ORDER BY date"
                             " LIMIT 30", (day, day, f"%{word}%", f"%{word}%")):
            if r["message_id"] not in seen:
                seen.add(r["message_id"])
                same_day.append(dict(r))
    return {"checkout": dict(c, raw=None), "webhook": hook, "update": dict(update, hook=webhook(update.get("raw")))
            if update else None, "order": order, "mails": mails, "by_number": by_number, "same_day": same_day,
            "number": number}


LOGIN = re.compile(r"(?:\S+:\S+@)?(\b(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9-]+(?:\.[a-z0-9-]+)+):\d{2,5})(?::[^:\s|`]+:[^:\s|`]+)?",
                   re.I)


def scrub(text):
    """Any proxy login written into a webhook's text, cut to its host and port."""
    return LOGIN.sub(r"\1", text or "")


def webhook(raw):
    """The bot's Discord embed as lines to show: {title, description, color, fields: [(name, value)], footer}.
    Proxy logins are cut to host and port, as everywhere else."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return None
    e = data.get("embed") if isinstance(data, dict) else None
    if not isinstance(e, dict):
        return None
    fields = []
    for f in e.get("fields") or []:
        name, value = parsers.tidy(f.get("name")), parsers.tidy(f.get("value"))
        if name.lower().rstrip(":") in PROXY_FIELDS + GROUP_FIELDS and (":" in value or "@" in value):
            value = proxy_host(value) or value
        value = scrub(value)
        if name.lower().rstrip(":") in ("password", "pass", "card", "card number", "cvv"):
            continue
        fields.append((name, value.strip("|` ")))
    color = e.get("color")
    return {"sender": data.get("payload_username") or "", "title": parsers.tidy(e.get("title")),
            "description": scrub(parsers.tidy(e.get("description"))), "fields": fields,
            "footer": parsers.tidy((e.get("footer") or {}).get("text")),
            "color": f"#{int(color):06x}" if isinstance(color, int) else None}


def tally(items):
    n = Counter(c["outcome"] for c in items)
    t = {"tries": len(items) - n["not_order"], "not_order": n["not_order"],
         "success": n["confirmed"] + n["no_email"] + n["cancelled"], "confirmed": n["confirmed"],
         "no_email": n["no_email"], "cancelled": n["cancelled"], "declines": n["decline"], "failures": n["failure"]}
    t["real"] = t["confirmed"] + t["no_email"]
    t["rate"] = round(100 * t["real"] / t["tries"]) if t["tries"] else 0
    return t


SORTS = {"proxy": lambda c: (c["proxy_group"].lower() or "~", c["proxy"] or "~", (c["profile"] or "~").lower()),
         "profile": lambda c: ((c["profile"] or "~").lower(), c["proxy_group"].lower() or "~", c["proxy"] or "~"),
         "address": lambda c: (c["address"] or "~", (c["profile"] or "~").lower(), c["proxy_group"].lower() or "~")}
OUTCOMES = ("confirmed", "no_email", "cancelled", "decline")
RANK = {"confirmed": 0, "no_email": 1, "cancelled": 2, "decline": 3, "failure": 4}


def leaderboard(items, key):
    """Groups by key (proxy group, proxy, profile, or proxy group + profile), most real checkouts first."""
    groups = {}
    for c in items:
        groups.setdefault(key(c), []).append(c)
    board = [dict(tally(g), name=k) for k, g in groups.items()]
    return sorted(board, key=lambda t: (-t["real"], -t["rate"], t["tries"]))


def site_name(names):
    """One name for a store the bots spell several ways ("amazon", "amazonv3"): the most used spelling without a
    module version, capitalised when the bot wrote it all in lower case."""
    names = [n for n in names if n]
    if not names:
        return "Unknown site"
    plain = [n for n in names if re.sub(r"[^a-z0-9]", "", n.lower()) == stuck.site_key(n)] or names
    name = Counter(plain).most_common(1)[0][0]
    return name[:1].upper() + name[1:] if name == name.lower() else name


def drops(items, sort="proxy", cards=None):
    """Site -> day -> checkouts, newest day first; each drop's checkouts sorted by proxy or by profile, with
    real checkouts first inside each proxy/profile."""
    sites = {}
    for c in items:
        key = stuck.site_key(c["site"]) or "unknown"
        sites.setdefault(key, []).append(c)
    out = []
    for key, group in sites.items():
        name = site_name(c["site"] for c in group)
        days = {}
        for c in group:
            days.setdefault(c["day"], []).append(c)
        day_list = []
        for day, cs in sorted(days.items(), reverse=True):
            cs.sort(key=lambda c: (SORTS[sort](c), RANK[c["outcome"]], c["ts"] or ""))
            products = [p for p, _ in Counter(c["product"] for c in cs if c["product"]).most_common(3)]
            day_list.append({"day": day, "checkouts": cs, "products": products, "tally": tally(cs),
                             "proxies": leaderboard(cs, lambda c: c["proxy_group"] or c["proxy"] or "No proxy"),
                             "card": cardcheck.for_drop(cards, name, day) if cards else None})
        out.append({"site": name, "days": day_list, "tally": tally(group), "latest": day_list[0]["day"]})
    return sorted(out, key=lambda s: s["latest"], reverse=True)


# The groups a drop's checkouts can be looked at by; the name of each is what the leaderboards show.
GROUPS = {"group": lambda c: c["proxy_group"] or "No group", "proxy": lambda c: c["proxy"] or "No proxy",
          "profile": lambda c: c["profile"] or "No profile", "address": lambda c: c["address"] or "No address"}


def report(con, site=None, sort="proxy", day=None, by=None, name=None, base=None, only=None):
    """Totals, leaderboards and drops for every checkout, or one site's, one drop day's, and/or one proxy group's,
    proxy's or profile's (by + name). With base addresses set up, the page opens on the checkouts per base address
    (base_overview) and base picks one ("none": the unmatched, "all": every one). only= one outcome's checkouts
    (confirmed, no_email, cancelled, decline), as the cards at the top open them; the totals stay whole."""
    from . import bases
    matched = stuck.cross_reference(con)
    cards = cardcheck.report(con, matched)
    items = rows(con, matched, cards["links"])
    res = bases.Resolver(con)
    base_overview, place = None, None
    if res.bases and base and base != "all":
        want = None if base == "none" else int(base) if base.isdigit() else -1
        items = [c for c in items if res.of_checkout(c) == want]
        b = res.by_id.get(want)
        place = {"id": base, "name": b["name"] if b else "Not matched to a base", "full": b["full"] if b else ""}
    elif res.bases and not (base or site or day or by or only):
        base_overview = bases.overview(items, res.of_checkout, res.bases, tally)
    by_key = {}
    for c in items:
        if c["site"]:
            by_key.setdefault(stuck.site_key(c["site"]) or c["site"], []).append(c["site"])
    sites = sorted((site_name(names) for names in by_key.values()), key=str.lower)
    if site:
        items = [c for c in items if stuck.same_site(c["site"], site)]
    if day:
        items = [c for c in items if c["day"] == day]
    # The groups to click into come from before picking one, so the others stay on screen.
    chips = {k: leaderboard(items, f) for k, f in GROUPS.items()}
    if by in GROUPS and name:
        items = [c for c in items if GROUPS[by](c) == name]
    totals = tally(items)
    if only in OUTCOMES:
        items = [c for c in items if c["outcome"] == only]
    return {
        "sites": sites,
        "chips": chips,
        "tally": totals,
        "groups": leaderboard(items, GROUPS["group"]),
        "proxies": leaderboard(items, GROUPS["proxy"]),
        "profiles": leaderboard(items, GROUPS["profile"]),
        "addresses": leaderboard(items, GROUPS["address"]),
        "full_addresses": full_addresses(items) if by == "address" else [],
        "combos": [t for t in leaderboard(items, lambda c: f"{c['proxy_group'] or c['proxy'] or 'No proxy'} + "
                                                         f"{c['profile'] or 'No profile'}") if t["real"]][:10],
        "drops": drops(items, sort, cards),
        "base_overview": base_overview,
        "place": place,
        "has_bases": bool(res.bases),
    }
