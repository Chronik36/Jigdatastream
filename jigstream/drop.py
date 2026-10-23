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

from . import parsers, stuck

GROUP_FIELDS = ["proxy group", "proxy list", "proxylist", "proxy profile", "proxy set"]
PROXY_FIELDS = ["proxy", "proxies", "proxy used", "ip"]
NO_PROXY = {"", "-", "none", "n/a", "na", "null", "local", "localhost", "no proxy", "no proxies", "off", "false"}
HOST = re.compile(r"^(\[[0-9a-f:]+\]|[a-z0-9.\-]+)(?::(\d{1,5}))?$", re.I)


def proxy_host(value):
    """The host (and port) of a proxy as a bot printed it, without any user name or password. "" when there's none."""
    s = (value or "").strip().strip("<>").strip()
    if s.lower() in NO_PROXY:
        return ""
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
    """'confirmed' (Good: an order email and no cancel, or marked good by hand), 'no_email' (Unsure: bot success,
    nothing either way yet), 'cancelled' (a cancel email or bank refund after the success, or marked cancelled),
    'decline' or 'failure'."""
    if c["status"] == "success":
        return {"emailed": "confirmed"}.get(c["state"], c["state"])
    return "decline" if c["status"] == "decline" else "failure"


def rows(con):
    """Every checkout webhook with its proxy group, proxy, profile, drop day and outcome."""
    matched = {c["id"]: c for c in stuck.cross_reference(con)}
    out = []
    for r in con.execute("SELECT * FROM checkouts ORDER BY ts"):
        c = matched.get(r["id"]) or dict(r, order=None, refund=None, state=None)
        c = dict(c)
        c["proxy_group"], c["proxy"] = proxy_of(r["raw"])
        c.pop("raw", None)
        c["day"], c["outcome"] = day_of(c["ts"]), outcome(c)
        out.append(c)
    return out


def tally(items):
    n = Counter(c["outcome"] for c in items)
    t = {"tries": len(items), "success": n["confirmed"] + n["no_email"] + n["cancelled"], "confirmed": n["confirmed"],
         "no_email": n["no_email"], "cancelled": n["cancelled"], "declines": n["decline"], "failures": n["failure"]}
    t["real"] = t["confirmed"] + t["no_email"]
    t["rate"] = round(100 * t["real"] / t["tries"]) if t["tries"] else 0
    return t


SORTS = {"proxy": lambda c: (c["proxy_group"].lower() or "~", c["proxy"] or "~", (c["profile"] or "~").lower()),
         "profile": lambda c: ((c["profile"] or "~").lower(), c["proxy_group"].lower() or "~", c["proxy"] or "~")}
RANK = {"confirmed": 0, "no_email": 1, "cancelled": 2, "decline": 3, "failure": 4}


def leaderboard(items, key):
    """Groups by key (proxy group, proxy, profile, or proxy group + profile), most real checkouts first."""
    groups = {}
    for c in items:
        groups.setdefault(key(c), []).append(c)
    board = [dict(tally(g), name=k) for k, g in groups.items()]
    return sorted(board, key=lambda t: (-t["real"], -t["rate"], t["tries"]))


def drops(items, sort="proxy"):
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
                             "proxies": leaderboard(cs, lambda c: c["proxy_group"] or c["proxy"] or "No proxy")})
        out.append({"site": name, "days": day_list, "tally": tally(group), "latest": day_list[0]["day"]})
    return sorted(out, key=lambda s: s["latest"], reverse=True)


def report(con, site=None, sort="proxy", day=None):
    """Totals, leaderboards and drops for every checkout, or one site's, or one drop day's."""
    items = rows(con)
    sites = sorted({c["site"] for c in items if c["site"]}, key=str.lower)
    if site:
        items = [c for c in items if stuck.same_site(c["site"], site)]
    if day:
        items = [c for c in items if c["day"] == day]
    return {
        "sites": sites,
        "tally": tally(items),
        "groups": leaderboard(items, lambda c: c["proxy_group"] or "No group"),
        "proxies": leaderboard(items, lambda c: c["proxy"] or "No proxy"),
        "profiles": leaderboard(items, lambda c: c["profile"] or "No profile"),
        "combos": [t for t in leaderboard(items, lambda c: f"{c['proxy_group'] or c['proxy'] or 'No proxy'} + "
                                                         f"{c['profile'] or 'No profile'}") if t["real"]][:10],
        "drops": drops(items, sort),
    }
