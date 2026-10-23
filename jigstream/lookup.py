"""One place, everything tied to it: the profiles that ship to a base address (jig units and prefixes dropped),
the checkouts sent there and the order emails that shipped there. Opened from any address on Drops, Profile
orders or Profiles, so a checkout leads to its profile, the profile to its address, and the address to the rest."""
from . import drop, profilematch


def address(con, label, orders):
    place = drop.label_cluster(label)
    if not place:
        return None
    homes = drop.profile_addresses(con)
    names = {n for n, w in homes.items() if drop.cluster(w) == place}
    checkouts = [c for c in drop.rows(con) if c.get("where") and drop.cluster(c["where"]) == place]
    names |= {c["profile"] for c in checkouts if c["profile"]}
    shipped = []
    for o in orders:
        w = drop.order_address(o)
        if (w and drop.cluster(w) == place) or (o.get("profile_name") in names and not w):
            shipped.append(o)
    hist = profilematch.history(con, shipped)
    profiles = sorted((dict(hist[n]["profile"], orders=len(hist[n]["orders"]),
                            checkouts=sum(1 for c in checkouts if c["profile"] == n),
                            full=homes[n]["full"] if n in homes else "")
                       for n in names if n in hist), key=lambda p: p["name"].lower())
    fulls = drop.full_addresses(checkouts)
    return {"label": label, "profiles": profiles, "checkouts": sorted(checkouts, key=lambda c: c["ts"] or "", reverse=True),
            "orders": shipped, "tally": drop.tally(checkouts), "full_addresses": fulls,
            "spend": sum(o["total"] or 0 for o in shipped if (o["shipping_status"] or "") not in
                         ("cancelled", "returned", "refunded", "not_an_order"))}


def profile_checkouts(con, name):
    """Every checkout webhook a profile ran, newest first, with its outcome (as on Drops)."""
    return sorted((c for c in drop.rows(con) if c["profile"] == name), key=lambda c: c["ts"] or "", reverse=True)


def label_of(w):
    return f"{w['base']} · {w['zip']}" if w and w["zip"] else (w or {}).get("base", "")


def profile_address(con, name):
    """The address label a profile ships to, for linking to its address page."""
    return label_of(drop.profile_addresses(con).get(name))
