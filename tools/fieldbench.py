"""Measure how well the rules read order fields, against emails you labelled by hand.

    python3 tools/fieldbench.py CORPUS_DIR LABELS.json [-v] [--old]

CORPUS_DIR holds one <id>.json per email ({"sender", "subject", "date", "body", "to"}, the body as
mail.parse_message gives it). LABELS.json maps each id to the right answers, any of: order_number, total,
subtotal, shipping, tax, name, street, zip, tracking, cancel ("full", "items", "refund" or null). A field left out
isn't scored; null means "the email has none".

Real emails hold names, addresses and card digits: keep the corpus and labels OUT of this repo (it is public).
--old scores the older single-regex helpers (order number, total, tracking, cancel) for a before/after.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jigstream import extract, rulefields  # noqa: E402
from jigstream.mail import Email  # noqa: E402

MONEY = ("total", "subtotal", "shipping", "tax")


def load(corpus, i):
    with open(os.path.join(corpus, f"{i}.json")) as f:
        d = json.load(f)
    return Email(i, d.get("sender", ""), d.get("subject", ""), d.get("date") or "", d.get("body", ""), account="bench",
                 to=d.get("to", ""))


def norm(field, value):
    if value is None:
        return None
    if field == "order_number":
        return re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if field in MONEY:
        return round(float(value), 2)
    if field in ("name", "street"):
        return re.sub(r"[^a-z0-9]", "", str(value).lower())
    if field == "tracking":
        return str(value).replace(" ", "").upper()
    return value


def old_rules(email):
    signal = extract.cancel_signal(email)
    return {"order_number": extract.find_order_number(email), "total": extract.find_total(email.body),
            "tracking": extract.find_tracking(email.body)[0],
            "cancel": {"cancelled": "full", "refunded": "refund"}.get(signal)}


def main(argv):
    corpus, labels_path = argv[1], argv[2]
    with open(labels_path) as f:
        labels = json.load(f)
    reader = old_rules if "--old" in argv else rulefields.read
    score, misses = {}, []
    for i, want_all in labels.items():
        got = reader(load(corpus, i))
        for field, want in want_all.items():
            if field not in got:
                continue
            g, w = norm(field, got.get(field)), norm(field, want)
            ok = (w in g or g in w) if field == "street" and g and w else g == w
            s = score.setdefault(field, [0, 0])
            s[0], s[1] = s[0] + ok, s[1] + 1
            if not ok:
                misses.append(f"{i} {field}: want {want!r}, got {got.get(field)!r}")
    for field, (right, n) in score.items():
        print(f"{field:13} {right}/{n}")
    print("all          ", sum(r for r, _ in score.values()), "/", sum(n for _, n in score.values()))
    if "-v" in argv:
        print("\n".join(misses))


if __name__ == "__main__":
    main(sys.argv)
