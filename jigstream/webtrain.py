"""Things to train: webhooks BotTrack couldn't place, taught one kind at a time.

A webhook lands here when its bot is Unknown (the sender's name doesn't say Valor, Hayha...) or BotTrack can't
tell what it is (not a checkout, decline or failure it recognises). Webhooks from the same sender with the same
kind of title are shown as one card, so a few hundred messages are a handful of choices. Each choice is saved as a
rule: the sender's bot, and what that kind of message is. Rules apply to the webhooks already stored and to every
one imported or synced later. "Not a checkout" (task logs, monitors, restock pings) is left out of every count.
"""
import json
import re

from . import parsers

KNOWN_BOTS = sorted(set(parsers.BOT_HINTS.values()))
STATUSES = {"success": "Successful checkout", "decline": "Declined", "failure": "Failed checkout",
            "ignored": "Not a checkout"}
COUNTED = ("success", "decline", "failure")  # statuses that are checkout attempts


def _raw(raw):
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}, {}
    return (d if isinstance(d, dict) else {}), (d.get("embed") if isinstance(d, dict) and isinstance(d.get("embed"), dict) else {})


def sender_of(payload_username, embed):
    """Who posted it: the webhook's name, else its footer or author line."""
    for s in (payload_username, (embed.get("footer") or {}).get("text"), (embed.get("author") or {}).get("name")):
        s = re.sub(r"\s+", " ", str(s or "")).strip().lower()
        if s:
            return s[:80]
    return ""


def shape_of(embed):
    """The kind of message: its title with numbers, prices and ids blanked."""
    title = re.sub(r"\s+", " ", str(embed.get("title") or embed.get("description") or "")).strip().lower()
    return re.sub(r"[\d$.,#:/-]+", "#", title)[:80]


def keys(raw):
    d, embed = _raw(raw)
    sender = sender_of(d.get("payload_username"), embed)
    return sender, f"{sender}|{shape_of(embed)}"


class Rules:
    def __init__(self, con):
        self.bots, self.kinds = {}, {}
        for kind, key, value in con.execute("SELECT kind, key, value FROM webhook_rules"):
            (self.bots if kind == "bot" else self.kinds)[key] = value

    def apply(self, rec):
        """Fill in a freshly parsed webhook from what was taught. Returns True when a rule applied."""
        if not (self.bots or self.kinds):
            return False
        sender, shape = keys(rec["raw"])
        hit = False
        if rec["bot"] == "Unknown" and sender in self.bots:
            rec["bot"], hit = self.bots[sender], True
        if shape in self.kinds:
            rec["status"], hit = self.kinds[shape], True
        return hit


def queue(con):
    """Cards to teach, biggest first: one per sender + kind of message, with an example and how many share it."""
    groups = {}
    for r in con.execute("SELECT id, bot, status, site, product, profile, ts, raw FROM checkouts"
                         " WHERE COALESCE(labeled, 0) = 0 AND (bot = 'Unknown' OR status NOT IN ('success', 'decline',"
                         " 'failure')) ORDER BY ts DESC"):
        sender, shape = keys(r["raw"])
        g = groups.get(shape)
        if g is None:
            d, embed = _raw(r["raw"])
            g = groups[shape] = {"id": r["id"], "sender": sender, "shape": shape, "n": 0, "bot": r["bot"],
                                 "status": r["status"], "title": embed.get("title") or "", "ts": r["ts"],
                                 "description": (embed.get("description") or "")[:400],
                                 "fields": list(parsers.flatten_embed(embed).items())[:14],
                                 "statuses": {}}
        g["n"] += 1
        g["statuses"][r["status"]] = g["statuses"].get(r["status"], 0) + 1
    return sorted(groups.values(), key=lambda g: -g["n"])


def teach(con, checkout_id, bot=None, status=None, just_this=False):
    """Save what this webhook is. Unless just_this, it becomes a rule for every webhook like it. Returns how
    many stored webhooks changed."""
    row = con.execute("SELECT raw FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
    if not row or (status and status not in STATUSES) or not (bot or status):
        return 0
    sender, shape = keys(row["raw"])
    if just_this:
        sets = {k: v for k, v in (("bot", bot), ("status", status)) if v}
        con.execute(f"UPDATE checkouts SET {', '.join(k + ' = ?' for k in sets)}, labeled = 1 WHERE id = ?",
                    (*sets.values(), checkout_id))
        con.commit()
        return 1
    if bot and sender:
        con.execute("INSERT OR REPLACE INTO webhook_rules VALUES ('bot', ?, ?)", (sender, bot))
    if status:
        con.execute("INSERT OR REPLACE INTO webhook_rules VALUES ('kind', ?, ?)", (shape, status))
    changed = 0
    for r in con.execute("SELECT id, bot, raw FROM checkouts WHERE COALESCE(labeled, 0) = 0").fetchall():
        s, sh = keys(r["raw"])
        sets = {}
        if bot and s == sender and r["bot"] == "Unknown":
            sets["bot"] = bot
        if status and sh == shape:
            sets["status"] = status
        if sets:
            con.execute(f"UPDATE checkouts SET {', '.join(k + ' = ?' for k in sets)}, labeled = 1 WHERE id = ?",
                        (*sets.values(), r["id"]))
            changed += 1
    con.commit()
    return changed


def forget(con):
    """Undo everything taught: the rules go, and taught webhooks are read again from their raw embed."""
    con.execute("DELETE FROM webhook_rules")
    for r in con.execute("SELECT id, raw FROM checkouts WHERE labeled = 1").fetchall():
        d, embed = _raw(r["raw"])
        rec = parsers.parse_embed({"username": d.get("payload_username")}, embed)
        con.execute("UPDATE checkouts SET bot = ?, status = ?, labeled = NULL WHERE id = ?", (rec["bot"], rec["status"], r["id"]))
    con.commit()


def counts(con):
    """By bot: how many webhooks are checkout attempts, not checkouts, and still to teach."""
    out = {}
    for bot, status, n in con.execute("SELECT bot, status, COUNT(*) FROM checkouts GROUP BY bot, status"):
        b = out.setdefault(bot or "Unknown", {"bot": bot or "Unknown", "attempts": 0, "success": 0, "ignored": 0, "unsure": 0})
        if status in COUNTED:
            b["attempts"] += n
            b["success"] += n if status == "success" else 0
        elif status == "ignored":
            b["ignored"] += n
        else:
            b["unsure"] += n
    return sorted(out.values(), key=lambda b: -(b["attempts"] + b["unsure"]))
