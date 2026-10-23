"""Your corrections to the Keyword finder's suggestions.

Removing or adding a + or - keyword saves a rule for that store (and that brand, when the page names
one), so the next page from the same store or brand gets the same treatment. Each page you correct is
also saved as an example of the "Keyword finder" task on the Training page (what the page looked like,
the keywords you ended up with), so the local model sees it before suggesting keywords for a new page,
and the task can be trained like any other.
"""

import json
import logging

from . import tasks

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
"""
TASK_NAME = "Keyword finder"


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


def keyword_task(con, instructions):
    """The Training page task for keyword suggestions (made the first time it's needed)."""
    row = con.execute("SELECT * FROM ai_tasks WHERE name = ?", (TASK_NAME,)).fetchone()
    if row:
        return dict(row)
    con.execute("INSERT INTO ai_tasks (name, instructions, answer, created_at) VALUES (?, ?, 'json', ?)",
                (TASK_NAME, instructions, tasks.now()))
    con.commit()
    return keyword_task(con, instructions)


def save_example(con, instructions, page, positives, negatives):
    """The page as the model sees it, with the keywords you settled on. One example per page: a newer
    correction of the same page replaces the older one."""
    t = keyword_task(con, instructions)
    text = json.dumps(page, ensure_ascii=False)
    answer = {"positive_keywords": [{"word": w} for w in positives],
              "negative_keywords": [{"word": w} for w in negatives]}
    con.execute("DELETE FROM ai_task_examples WHERE task_id = ? AND input = ?", (t["id"], text))
    tasks.add_examples(con, t["id"], [(text, json.dumps(answer, ensure_ascii=False))])
    return t["id"]


def all_rules(con):
    return [dict(r) for r in con.execute("SELECT * FROM keyword_rules ORDER BY host, kind, word")]
