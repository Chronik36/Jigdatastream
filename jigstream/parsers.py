"""Parse raw Discord webhook JSON into checkout records.

Bots format embeds differently and change them over time, so parsing is
heuristic: each embed is flattened to {lowercased field name: value} and then
looked up through alias lists. Add aliases below (or a bot-specific override)
once you have real payloads from your bots.
"""
import hashlib
import html
import json
import re

BOT_HINTS = {
    "valor": "Valor",
    "stellar": "Stellar",
    "hayha": "Hayha",
    "polar": "Polar",
}

ALIASES = {
    "product": ["product", "item", "product name", "title", "name"],
    "size": ["size", "sizes", "variant"],
    "site": ["site", "store", "website", "retailer", "domain"],
    "profile": ["profile", "profile name", "billing profile", "account profile"],
    "price": ["price", "total", "amount", "cost"],
    "order_number": ["order", "order #", "order number", "order id", "order no", "confirmation"],
    "email": ["email", "account", "account email", "login"],
    "reason": ["cancel reason", "decline reason", "reason", "error", "message", "status message", "details", "response"],
}

SUCCESS_WORDS = ["success", "checked out", "checkout complete", "order placed", "order confirmed", "purchased"]
DECLINE_WORDS = ["declin", "card error", "payment failed", "payment error", "avs", "insufficient"]
FAIL_WORDS = ["fail", "error", "out of stock", "oos", "banned", "blocked", "timeout", "cancel", "invalid", "queue"]


def _clean(s):
    s = html.unescape(str(s))  # Hayha sends "Pok&#233;mon"
    s = re.sub(r"[*_`~|]", "", s)
    s = re.sub(r"\|\|", "", s)
    return s.strip()


def tidy(s):
    """Text a bot sent, as people should read it: HTML entities decoded even when sent twice ("Pok&amp;#233;mon"),
    and markdown links reduced to their text. For display only; stored fields stay as they came."""
    s = str(s or "")
    for _ in range(3):
        if "&" not in s:
            break
        s = html.unescape(s)
    return re.sub(r"\[([^\]]+)\]\((?:https?://)[^)\s]*\)", r"\1", s).strip()


def flatten_embed(embed):
    fields = {}
    for f in embed.get("fields", []) or []:
        fields[_clean(f.get("name", "")).lower().rstrip(":")] = _clean(f.get("value", ""))
    return fields


def detect_bot(payload, embed):
    blob = " ".join(
        str(x)
        for x in (
            payload.get("username", ""),
            (embed.get("footer") or {}).get("text", ""),
            (embed.get("author") or {}).get("name", ""),
            embed.get("title", ""),
        )
    ).lower()
    for hint, name in BOT_HINTS.items():
        if hint in blob:
            return name
    return "Unknown"


def color_status(color):
    """'success' for a green embed, 'decline' for a red one, else None."""
    try:
        color = int(color)
    except (TypeError, ValueError):
        return None
    r, g, b = color >> 16 & 255, color >> 8 & 255, color & 255
    if g > 100 and g > r * 1.3 and g >= b * 0.8:
        return "success"
    if r > 120 and r > g * 1.6 and r > b * 1.6:
        return "decline"
    return None


def classify(embed, bot=None):
    # A "Cancel Reason" field (Hayha) means the site cancelled it on the spot, whatever the title says.
    if any(k in ("cancel reason", "decline reason") and v for k, v in flatten_embed(embed).items()):
        return "decline"
    # Hayha titles its declines "Successful" too; only the embed color tells them apart (green or red).
    if bot == "Hayha":
        by_color = color_status(embed.get("color"))
        if by_color == "success":
            return "success"
        if by_color == "decline":
            text_status = classify(embed)
            return text_status if text_status == "failure" and "success" not in str(embed.get("title", "")).lower() \
                else "decline"
    text = f"{embed.get('title', '')} {embed.get('description', '')}".lower()
    flat = " ".join(flatten_embed(embed).keys()).lower()
    # decline/fail words win over "success" appearing in e.g. "not successful"
    if any(w in text for w in DECLINE_WORDS):
        return "decline"
    if re.search(r"not success|unsuccess|fail", text):
        return "failure"
    if any(w in text for w in SUCCESS_WORDS):
        return "success"
    if any(w in text for w in FAIL_WORDS):
        return "failure"
    if "order" in flat and "profile" in flat:
        return "success"
    return "unknown"


# A follow-up some bots (Valor) post after the checkout webhook, saying how the order turned out. Its title is about
# the order, not the checkout: "Order Cancelled", "Order Stuck", "Order Shipped", "Order Status Update".
UPDATE_TITLE = re.compile(r"\border\b.*\b(cancel|stuck|shipped|delivered|update|status|good)|"
                          r"\b(cancel+ed|stuck|shipped|delivered)\b.*\border\b", re.I)
UPDATE_CANCELLED = re.compile(r"cancel", re.I)


def update_verdict(embed):
    """"good" or "cancelled" when this embed is a bot's follow-up on an earlier order, else None. A checkout webhook
    (its title says checkout, or it carries a Cancel/Decline Reason, as Hayha declines do) is never one."""
    title = _clean(embed.get("title", "") or (embed.get("author") or {}).get("name", ""))
    if not title or "checkout" in title.lower() or any(
            k in ("cancel reason", "decline reason") for k in flatten_embed(embed)):
        return None
    if not UPDATE_TITLE.search(title):
        return None
    # Valor: "Order Canceled! (Policy - Item Demand)" in red, "Order Still Good ✅" in green.
    if UPDATE_CANCELLED.search(title):
        return "cancelled"
    if re.search(r"good|stuck|shipped|delivered", title, re.I):
        return "good"
    fields = flatten_embed(embed)
    status = fields.get("status", "") or fields.get("order status", "")
    if status:
        return "cancelled" if UPDATE_CANCELLED.search(status) else "good"
    return "cancelled" if color_status(embed.get("color")) == "decline" else "good"


def _pick(fields, key):
    for a in ALIASES[key]:
        if a in fields and fields[a]:
            return fields[a]
    return ""


def parse_embed(payload, embed):
    fields = flatten_embed(embed)
    rec = {k: _pick(fields, k) for k in ALIASES}
    rec["bot"] = detect_bot(payload, embed)
    rec["status"] = classify(embed, rec["bot"])
    if not rec["product"]:
        rec["product"] = _clean(embed.get("description", ""))[:200] if rec["status"] == "success" else ""
    # Hayha puts the price at the end of the item: "Booster Bundle Box - $63.98".
    m = re.match(r"(.+?)\s+-\s+(\$\s?[\d,]+(?:\.\d\d)?)$", rec["product"])
    if m and not rec["price"]:
        rec["product"], rec["price"] = m.group(1), m.group(2)
    rec["ts"] = embed.get("timestamp") or payload.get("timestamp") or ""
    rec["update"] = update_verdict(embed)
    if rec["update"]:
        rec["status"], rec["reason"] = "update", rec["update"]
    if rec["status"] != "success" and not rec["reason"]:
        rec["reason"] = _clean(embed.get("description", ""))[:300]
    if rec["status"] == "success":
        rec["reason"] = ""
    rec["raw"] = json.dumps({"payload_username": payload.get("username"), "embed": embed})
    key = "|".join([rec["bot"], rec["site"], rec["product"], rec["profile"], rec["order_number"], rec["ts"], rec["status"]])
    rec["fingerprint"] = hashlib.sha1(key.encode()).hexdigest()
    return rec


def _payloads(text):
    text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(data, dict):
        # discord channel exports use {"messages":[...]}
        data = data.get("messages", [data])
    return data


def parse_text(text):
    out = []
    for p in _payloads(text):
        if not isinstance(p, dict):
            continue
        for emb in p.get("embeds", []) or []:
            out.append(parse_embed(p, emb))
    return out


def reclassify(con, bot="Hayha"):
    """Re-read stored checkouts of one bot from their raw embed, after a parsing rule changed. Returns how many
    changed status. The fingerprint follows, so importing the same webhook again doesn't add it twice."""
    changed = 0
    for row in con.execute("SELECT id, status, product, price, fingerprint, raw FROM checkouts WHERE bot = ?"
                           " AND COALESCE(labeled, 0) = 0", (bot,)).fetchall():
        try:
            raw = json.loads(row["raw"] or "{}")
        except ValueError:
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("embed"), dict):
            continue
        rec = parse_embed({"username": raw.get("payload_username")}, raw["embed"])
        if rec["bot"] != bot or all(rec[k] == row[k] for k in ("status", "product", "price", "fingerprint")):
            continue
        cur = con.execute("UPDATE OR IGNORE checkouts SET status = ?, reason = ?, product = ?, price = ?, fingerprint = ?"
                          " WHERE id = ?", (rec["status"], rec["reason"], rec["product"], rec["price"],
                                            rec["fingerprint"], row["id"]))
        if not cur.rowcount:  # the corrected row is already stored
            con.execute("DELETE FROM checkouts WHERE id = ?", (row["id"],))
        changed += rec["status"] != row["status"]
    con.commit()
    return changed


UPDATE_SQL = """INSERT OR IGNORE INTO checkout_updates (fingerprint, bot, verdict, site, profile, order_number, email, ts, raw)
    VALUES (:fingerprint, :bot, :update, :site, :profile, :order_number, :email, :ts, :raw)"""


def move_updates(con):
    """Follow-ups stored as checkouts before they were told apart move to checkout_updates. Returns how many."""
    moved = 0
    for row in con.execute("SELECT id, raw FROM checkouts WHERE raw LIKE '%rder%'").fetchall():
        try:
            raw = json.loads(row["raw"] or "{}")
        except ValueError:
            continue
        if isinstance(raw, dict) and isinstance(raw.get("embed"), dict) and update_verdict(raw["embed"]):
            con.execute(UPDATE_SQL, parse_embed({"username": raw.get("payload_username")}, raw["embed"]))
            con.execute("DELETE FROM checkouts WHERE id = ?", (row["id"],))
            moved += 1
    con.commit()
    return moved


def ingest(con, text):
    from . import webtrain
    n, teach = 0, webtrain.Rules(con)
    for r in parse_text(text):
        if r["update"]:  # not a checkout: how an earlier one turned out
            n += con.execute(UPDATE_SQL, r).rowcount
            continue
        r["labeled"] = 1 if teach.apply(r) else None  # taught on Things to train; the fingerprint stays as parsed
        cur = con.execute(
            """INSERT OR IGNORE INTO checkouts
            (fingerprint,bot,status,reason,site,product,size,price,profile,order_number,email,ts,raw,labeled)
            VALUES (:fingerprint,:bot,:status,:reason,:site,:product,:size,:price,:profile,:order_number,:email,:ts,:raw,
                    :labeled)""",
            r,
        )
        n += cur.rowcount
    con.commit()
    return n
