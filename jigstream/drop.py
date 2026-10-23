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
from . import cardcheck, parsers, profilematch, stuck

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
    """Mark a checkout good or cancelled by hand; "auto" goes back to what the emails and bank say."""
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
    after the success, or marked cancelled), 'decline' or 'failure'."""
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


def label_addresses(items):
    """Name each checkout's address by its base address. Jigged copies of one address share the house number and
    ZIP even when the street is spelled a little differently ("MAIN ST" / "MAINN ST"), so those count as one;
    the label is the spelling used most."""
    def cluster(w):
        house = w["base"].split()[0]
        return f"{house}|{w['zip']}" if w["zip"] else w["base"]
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
        if c.get("address_full"):
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
    out = []
    for r in con.execute("SELECT * FROM checkouts ORDER BY ts"):
        c = matched.get(r["id"]) or dict(r, order=None, refund=None, state=None)
        c = dict(c, card=links.get(r["id"]))
        c["proxy_group"], c["proxy"] = proxy_of(r["raw"])
        c.pop("raw", None)
        c["day"], c["outcome"] = day_of(c["ts"]), outcome(c)
        c["product"] = parsers.tidy(c["product"])  # for showing; the stored row is untouched
        c["where"] = homes.get(c["profile"]) or order_address(c["order"])
        out.append(c)
    label_addresses(out)
    return out


def tally(items):
    n = Counter(c["outcome"] for c in items)
    t = {"tries": len(items), "success": n["confirmed"] + n["no_email"] + n["cancelled"], "confirmed": n["confirmed"],
         "no_email": n["no_email"], "cancelled": n["cancelled"], "declines": n["decline"], "failures": n["failure"]}
    t["real"] = t["confirmed"] + t["no_email"]
    t["rate"] = round(100 * t["real"] / t["tries"]) if t["tries"] else 0
    return t


SORTS = {"proxy": lambda c: (c["proxy_group"].lower() or "~", c["proxy"] or "~", (c["profile"] or "~").lower()),
         "profile": lambda c: ((c["profile"] or "~").lower(), c["proxy_group"].lower() or "~", c["proxy"] or "~"),
         "address": lambda c: (c["address"] or "~", (c["profile"] or "~").lower(), c["proxy_group"].lower() or "~")}
RANK = {"confirmed": 0, "no_email": 1, "cancelled": 2, "decline": 3, "failure": 4}


def leaderboard(items, key):
    """Groups by key (proxy group, proxy, profile, or proxy group + profile), most real checkouts first."""
    groups = {}
    for c in items:
        groups.setdefault(key(c), []).append(c)
    board = [dict(tally(g), name=k) for k, g in groups.items()]
    return sorted(board, key=lambda t: (-t["real"], -t["rate"], t["tries"]))


def drops(items, sort="proxy", cards=None):
    """Site -> day -> checkouts, newest day first; each drop's checkouts sorted by proxy or by profile, with
    real checkouts first inside each proxy/profile."""
    sites = {}
    for c in items:
        key = stuck.site_key(c["site"]) or "unknown"
        sites.setdefault(key, []).append(c)
    out = []
    for key, group in sites.items():
        name = Counter(c["site"] or "Unknown site" for c in group).most_common(1)[0][0]
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


def report(con, site=None, sort="proxy", day=None, by=None, name=None):
    """Totals, leaderboards and drops for every checkout, or one site's, one drop day's, and/or one proxy group's,
    proxy's or profile's (by + name)."""
    matched = stuck.cross_reference(con)
    cards = cardcheck.report(con, matched)
    items = rows(con, matched, cards["links"])
    sites = sorted({c["site"] for c in items if c["site"]}, key=str.lower)
    if site:
        items = [c for c in items if stuck.same_site(c["site"], site)]
    if day:
        items = [c for c in items if c["day"] == day]
    # The groups to click into come from before picking one, so the others stay on screen.
    chips = {k: leaderboard(items, f) for k, f in GROUPS.items()}
    if by in GROUPS and name:
        items = [c for c in items if GROUPS[by](c) == name]
    return {
        "sites": sites,
        "chips": chips,
        "tally": tally(items),
        "groups": leaderboard(items, GROUPS["group"]),
        "proxies": leaderboard(items, GROUPS["proxy"]),
        "profiles": leaderboard(items, GROUPS["profile"]),
        "addresses": leaderboard(items, GROUPS["address"]),
        "full_addresses": full_addresses(items) if by == "address" else [],
        "combos": [t for t in leaderboard(items, lambda c: f"{c['proxy_group'] or c['proxy'] or 'No proxy'} + "
                                                         f"{c['profile'] or 'No profile'}") if t["real"]][:10],
        "drops": drops(items, sort, cards),
    }
