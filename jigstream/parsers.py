"""Parse raw Discord webhook JSON into checkout records.

Bots format embeds differently and change them over time, so parsing is
heuristic: each embed is flattened to {lowercased field name: value} and then
looked up through alias lists. Add aliases below (or a bot-specific override)
once you have real payloads from your bots.
"""
import hashlib
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
    "reason": ["reason", "error", "message", "status message", "details", "response"],
}

SUCCESS_WORDS = ["success", "checked out", "checkout complete", "order placed", "order confirmed", "purchased"]
DECLINE_WORDS = ["declin", "card error", "payment failed", "payment error", "avs", "insufficient"]
FAIL_WORDS = ["fail", "error", "out of stock", "oos", "banned", "blocked", "timeout", "cancel", "invalid", "queue"]


def _clean(s):
    s = re.sub(r"[*_`~|]", "", str(s))
    s = re.sub(r"\|\|", "", s)
    return s.strip()


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


def classify(embed):
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


def _pick(fields, key):
    for a in ALIASES[key]:
        if a in fields and fields[a]:
            return fields[a]
    return ""


def parse_embed(payload, embed):
    fields = flatten_embed(embed)
    rec = {k: _pick(fields, k) for k in ALIASES}
    if not rec["product"]:
        rec["product"] = _clean(embed.get("description", ""))[:200] if classify(embed) == "success" else ""
    rec["status"] = classify(embed)
    rec["bot"] = detect_bot(payload, embed)
    rec["ts"] = embed.get("timestamp") or payload.get("timestamp") or ""
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


def ingest(con, text):
    n = 0
    for r in parse_text(text):
        cur = con.execute(
            """INSERT OR IGNORE INTO checkouts
            (fingerprint,bot,status,reason,site,product,size,price,profile,order_number,email,ts,raw)
            VALUES (:fingerprint,:bot,:status,:reason,:site,:product,:size,:price,:profile,:order_number,:email,:ts,:raw)""",
            r,
        )
        n += cur.rowcount
    con.commit()
    return n
