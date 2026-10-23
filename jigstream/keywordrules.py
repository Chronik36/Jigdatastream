"""Your corrections to the Keyword finder's suggestions.

Removing or adding a + or - keyword saves a rule for that store (and that brand, when the page names
one), so the next page from the same store or brand gets the same treatment. Each page you correct is
also kept as an example (what the page looked like, the keywords you ended up with) and shown to the
local model before it suggests keywords for a new page.
"""

import json
import logging

logger = logging.getLogger("jigstream.keywords")

SCHEMA = """
CREATE TABLE IF NOT EXISTS keyword_rules (
    id INTEGER PRIMARY KEY,
    host TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('+', '-')),
    word TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add', 'remove')),
    url TEXT,
    product TEXT,
    created TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (host, kind, word)
);
CREATE TABLE IF NOT EXISTS keyword_examples (
    url TEXT PRIMARY KEY,
    host TEXT,
    product TEXT,
    page TEXT NOT NULL,
    answer TEXT NOT NULL,
    updated TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def ensure(con):
    con.executescript(SCHEMA)
    return con


def save_rule(con, host, brand, kind, word, action, url="", product=""):
    word = word.strip().lstrip("+-").strip().lower()
    if not word or kind not in ("+", "-") or action not in ("add", "remove"):
        raise ValueError("Need a keyword, + or -, and add or remove.")
    con.execute(
        "INSERT INTO keyword_rules (host, brand, kind, word, action, url, product) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (host, kind, word) DO UPDATE SET action = excluded.action, brand = excluded.brand,"
        " url = excluded.url, product = excluded.product, created = CURRENT_TIMESTAMP",
        (host.lower(), (brand or "").lower(), kind, word, action, url, product))
    con.commit()
    logger.info("keyword finder: rule %s%s %s for %s%s", kind, word, action, host, f" / {brand}" if brand else "")
    rule_id = con.execute("SELECT id FROM keyword_rules WHERE host = ? AND kind = ? AND word = ?",
                          (host.lower(), kind, word)).fetchone()[0]
    return word, rule_id


def delete_rule(con, rule_id):
    con.execute("DELETE FROM keyword_rules WHERE id = ?", (rule_id,))
    con.commit()


def rules_for(con, host, brand=""):
    """Rules from this store, plus rules from other stores for the same brand."""
    return [dict(r) for r in con.execute(
        "SELECT * FROM keyword_rules WHERE host = ? OR (brand != '' AND brand = ?) ORDER BY created",
        (host.lower(), (brand or "").lower()))]


def apply(result, rules):
    """Drop what you removed, add what you added. Marks result['removed'] for the page to list."""
    if not rules:
        result.setdefault("removed", [])
        return result
    removed = {(r["kind"], r["word"]): r for r in rules if r["action"] == "remove"}
    added = [r for r in rules if r["action"] == "add"]

    def where(r):
        return r["host"] if r["host"] == result["host"].lower() else f"{r['brand'].upper()} items"

    result["positives"] = [p for p in result["positives"] if ("+", p[0].lower()) not in removed]
    result["negatives"] = [n for n in result["negatives"] if ("-", n[0].lower()) not in removed]
    have_pos = {p[0].lower() for p in result["positives"]}
    have_neg = {n[0].lower() for n in result["negatives"]}
    for r in added:
        reason = f"you added this for {where(r)}"
        if r["kind"] == "+" and r["word"] not in have_pos:
            result["positives"].append((r["word"], reason, True))
            have_pos.add(r["word"])
        elif r["kind"] == "-" and r["word"] not in have_neg and r["word"] not in have_pos:
            result["negatives"].insert(0, (r["word"], reason))
            have_neg.add(r["word"])
    result["removed"] = [{"id": r["id"], "kind": r["kind"], "word": r["word"], "where": where(r)}
                         for r in removed.values()]
    return result


def filter_ai(ai, rules):
    """The model's suggestions minus anything you removed."""
    removed = {(r["kind"], r["word"]) for r in rules if r["action"] == "remove"}
    ai["positives"] = [(w, r) for w, r in ai["positives"] if ("+", w.lower()) not in removed]
    ai["negatives"] = [(w, r) for w, r in ai["negatives"] if ("-", w.lower()) not in removed]
    return ai


def save_example(con, url, host, product, page, positives, negatives):
    """The page as the model sees it, with the keywords you settled on."""
    answer = {"positive_keywords": [{"word": w} for w in positives],
              "negative_keywords": [{"word": w} for w in negatives]}
    con.execute(
        "INSERT INTO keyword_examples (url, host, product, page, answer) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (url) DO UPDATE SET page = excluded.page, answer = excluded.answer, product = excluded.product,"
        " updated = CURRENT_TIMESTAMP",
        (url, host, product, json.dumps(page, ensure_ascii=False), json.dumps(answer, ensure_ascii=False)))
    con.commit()


def examples(con, n=None, skip_url=""):
    rows = con.execute("SELECT * FROM keyword_examples WHERE url != ? ORDER BY updated DESC" + (" LIMIT ?" if n else ""),
                       (skip_url, n) if n else (skip_url,)).fetchall()
    return [dict(r) for r in rows]


def few_shot(con, url, n=2):
    """(page JSON, corrected answer JSON) pairs for the model, most recent first, never the page itself."""
    return [(e["page"], e["answer"]) for e in examples(con, n, skip_url=url)]


def delete_example(con, url):
    con.execute("DELETE FROM keyword_examples WHERE url = ?", (url,))
    con.commit()


def export_jsonl(con, system):
    lines = [json.dumps({"messages": [{"role": "system", "content": system}, {"role": "user", "content": e["page"]},
                                      {"role": "assistant", "content": e["answer"]}]}, ensure_ascii=False)
             for e in examples(con)]
    return "\n".join(lines) + ("\n" if lines else "")


def all_rules(con):
    return [dict(r) for r in con.execute("SELECT * FROM keyword_rules ORDER BY host, kind, word")]
