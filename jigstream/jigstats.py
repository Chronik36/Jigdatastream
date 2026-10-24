"""Which jig styles get cancelled, per store, from your own Drops results; and a ready expression for each style.

Every successful checkout's profile address is sorted by style: letters in front of the house number, letters
right after it, the unit word (Apt / Unit / Suite / #), periods in the street, random capitals. A checkout counts
as kept (Good or Unsure on Drops) or cancelled; declines are left out since they're about payment. A style with
at least MIN_TRIES checkouts and the lowest cancel rate in its group is the suggestion, with the expression that
makes it on the Profiles > Expressions window. The numbers decide; nothing is guessed by the AI.
"""
import re
from collections import defaultdict

from . import drop, stuck

MIN_TRIES = 5
KEPT, CANCELLED = ("confirmed", "no_email"), ("cancelled",)
STREET_WORDS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "ST", "AVE", "RD", "DR", "LN", "CT", "BLVD", "HWY", "RTE",
                "OLD", "NEW", "THE", "MT", "FT", "US", "SR", "CR", "FM"}
# group -> (style label, test on the address line, how to make it: (field, mode, find, replace))
STYLES = {
    "Front": [
        ("Letters before the number", lambda s: re.match(r"\s*[A-Za-z]{1,6}[\s-]+\d", s),
         ("address1", "template", "", "%c3% {value}")),
        ("Letters after the number", lambda s: re.match(r"\s*\d+[A-Za-z]{2,}\b", s) or _spaced_after(s),
         ("address1", "regex", r"^(\d+)\s*", r"\1 %c3% ")),
        ("Plain number", lambda s: re.match(r"\s*\d+\s+[A-Za-z]", s) and not _spaced_after(s), None),
    ],
    "Unit": [
        ("Apt", lambda s: re.search(r"\b(apt|apartment)\b", s, re.I), ("address2", "template", "", "Apt %n[1-999]%")),
        ("Unit", lambda s: re.search(r"\bunit\b", s, re.I), ("address2", "template", "", "Unit %n[1-999]%")),
        ("Suite", lambda s: re.search(r"\b(suite|ste)\b", s, re.I), ("address2", "template", "", "Suite %n[100-999]%")),
        ("#", lambda s: "#" in s, ("address2", "template", "", "#%d3%")),
        ("No unit", lambda s: not re.search(r"\b(apt|apartment|unit|suite|ste)\b|#", s, re.I), None),
    ],
    "Spelling": [
        ("Periods in the street", lambda s: re.search(r"[A-Za-z]\.[A-Za-z]", s), ("address1", "template", "", "%dots[{value}]%")),
        ("Random capitals", lambda s: re.search(r"[a-z][A-Z]", s), ("address1", "template", "", "%rc[{value}]%")),
        ("Plain spelling", lambda s: not re.search(r"[A-Za-z]\.[A-Za-z]|[a-z][A-Z]", s), None),
    ],
}


SUFFIXES = re.compile(r"(st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|ct|court|way|pl|place|pkwy|cir|hwy|route|rte|ter|trl)\b", re.I)


def _spaced_after(s):
    """A made-up letter group between the number and the street: "6432 XQZ State Route 22", not "12 Elm St"."""
    m = re.match(r"\s*\d+\s+([A-Za-z]{2,6})\s+(\S+)", s)
    if not m or m.group(1).upper() in STREET_WORDS or SUFFIXES.match(m.group(2)):
        return False
    token = m.group(1)
    rest = s[m.end(1):]
    return token.isupper() and (not re.search(r"[AEIOU]", token) or (len(s.split()) >= 4 and rest != rest.upper()))


def lines(con):
    """profile -> its ship address line 1 and 2, as typed."""
    out = {}
    for name, a1, a2, b1, b2 in con.execute("SELECT name, ship_address1, ship_address2, bill_address1, bill_address2 FROM profiles"):
        a = " ".join(x for x in ((a1, a2) if (a1 or "").strip() else (b1, b2)) if x)
        if a.strip():
            out[name] = a
    return out


def report(con, items=None):
    """{store: {"n", "groups": {group: [{style, kept, cancelled, rate, recipe, best}]}}}, busiest store first."""
    items = items if items is not None else drop.rows(con)
    where = lines(con)
    tallies = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    names = defaultdict(list)
    for c in items:
        if c["outcome"] not in KEPT + CANCELLED or not c.get("profile") or c["profile"] not in where:
            continue
        key = stuck.site_key(c["site"]) or "?"
        names[key].append(c["site"])
        line = where[c["profile"]]
        for group, styles in STYLES.items():
            for label, test, _ in styles:
                if test(line):
                    tallies[key][(group, label)][c["outcome"] in CANCELLED] += 1
    out = {}
    for key, t in tallies.items():
        groups = {}
        for group, styles in STYLES.items():
            rows = []
            for label, _, recipe in styles:
                kept, cancelled = t.get((group, label), (0, 0))
                if kept + cancelled:
                    rows.append({"style": label, "kept": kept, "cancelled": cancelled, "tries": kept + cancelled,
                                 "rate": round(100 * cancelled / (kept + cancelled)),
                                 "recipe": dict(zip(("field", "mode", "find", "replace"), recipe)) if recipe else None})
            sure = [r for r in rows if r["tries"] >= MIN_TRIES]
            if len(sure) > 1:
                best = min(sure, key=lambda r: (r["rate"], -r["tries"]))
                best["best"] = True
            groups[group] = sorted(rows, key=lambda r: (r["rate"], -r["tries"]))
        out[drop.site_name(names[key])] = {"n": len(names[key]), "groups": groups}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))
