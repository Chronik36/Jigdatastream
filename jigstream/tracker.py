"""Link successful checkouts to retailer emails and summarize the order lifecycle."""
import re
from datetime import datetime, timedelta

from .emails import SITE_KEYS

WINDOW = timedelta(hours=72)
STOP = {"the", "and", "with", "for", "pack", "box", "set", "new", "edition"}
RANK = {"cancelled": 3, "shipped": 2, "confirmed": 1}


def _dt(s):
    try:
        d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return d.replace(tzinfo=None) if d.tzinfo is None else d.astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in STOP}


def _same_retailer(site, o):
    site = (site or "").lower()
    keys = SITE_KEYS.get(o["retailer"])
    if keys:
        return any(k in site for k in keys)
    # Shopify store: the sender's display name should resemble the checkout site
    st = _tokens(o["store"]) - {"shopify", "orders", "order", "noreply", "store", "team"}
    return bool(st) and any(t in re.sub(r"[^a-z0-9]", "", site) for t in st)


def link(con):
    """One row per successful checkout with the strongest matching email status."""
    orders = con.execute("SELECT * FROM orders ORDER BY ts").fetchall()
    out = []
    for c in con.execute("SELECT * FROM checkouts WHERE status='success' ORDER BY ts DESC"):
        cts, ptoks = _dt(c["ts"]), _tokens(c["product"])
        matches, how = [], ""
        if c["order_number"]:
            num = re.sub(r"\W", "", c["order_number"]).upper()
            matches = [o for o in orders if o["order_number"] and re.sub(r"\W", "", o["order_number"]) == num]
            how = "order #" if matches else ""
        if not matches and cts:
            for o in orders:
                ots = _dt(o["ts"])
                if not ots or not _same_retailer(c["site"], o) or not (timedelta(0) <= ots - cts <= WINDOW):
                    continue
                if ptoks and not (ptoks & _tokens(o["body"] + " " + o["subject"])):
                    continue
                matches.append(o)
            how = "retailer+product+time" if matches else ""
        best = max(matches, key=lambda o: RANK[o["kind"]], default=None)
        out.append({"checkout": c, "status": best["kind"] if best else "no email",
                    "emails": matches, "matched_by": how})
    return out


def retailer_stats(con):
    stats = {}
    for r in link(con):
        site = r["checkout"]["site"] or "unknown"
        s = stats.setdefault(site, {"checkouts": 0, "confirmed": 0, "shipped": 0, "cancelled": 0, "no email": 0})
        s["checkouts"] += 1
        s[r["status"]] += 1
    for s in stats.values():
        s["cancel_rate"] = round(100 * s["cancelled"] / s["checkouts"]) if s["checkouts"] else 0
    return dict(sorted(stats.items(), key=lambda kv: -kv[1]["checkouts"]))
