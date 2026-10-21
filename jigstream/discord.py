"""Pull bot webhook messages from your own Discord channels via a bot token (read-only).

Setup: create an app at discord.com/developers, add a Bot, enable the "Message Content Intent" (Discord blanks
embeds from other bots without it), invite the bot to your server with View Channel + Read Message History,
then set DISCORD_BOT_TOKEN. Never paste the token anywhere but your own environment.
"""
import json
import os
import time
import urllib.error
import urllib.request

from . import parsers

API = "https://discord.com/api/v10"


def _get(path, token):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bot {token}", "User-Agent": "Jigdatastream (local, 1.0)"})
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(float(json.loads(e.read() or b"{}").get("retry_after", 1)), 30))
                continue
            if e.code in (401, 403, 404):
                raise RuntimeError({401: "Discord rejected the token", 403: "Bot can't read that channel (needs View Channel + "
                                    "Read Message History)", 404: "Channel not found"}[e.code])
            raise
    raise RuntimeError("Discord rate limit: try again shortly")


def _state(con, key, value=None):
    if value is None:
        r = con.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else ""
    con.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)", (key, value))


def fetch_channel(channel_id, token, after=None, max_messages=5000):
    """Return messages oldest-first, paging back from the newest until `after` (a message id) is reached."""
    msgs, before = [], None
    while len(msgs) < max_messages:
        q = f"/channels/{channel_id}/messages?limit=100" + (f"&before={before}" if before else "")
        page = _get(q, token)
        if not page:
            break
        for m in page:
            if after and int(m["id"]) <= int(after):
                return sorted(msgs, key=lambda m: int(m["id"]))
            msgs.append(m)
        before = page[-1]["id"]
    return sorted(msgs, key=lambda m: int(m["id"]))


def sync(con, channel_ids=None, token=None, use_llm=False):
    token = token or os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Set DISCORD_BOT_TOKEN in the environment.")
    if channel_ids is not None:
        _state(con, "discord_channels", ",".join(channel_ids))
    ids = [c for c in (_state(con, "discord_channels") or "").split(",") if c.strip()]
    if not ids:
        raise RuntimeError("Enter at least one channel ID.")
    added = scanned = 0
    for ch in ids:
        msgs = fetch_channel(ch.strip(), token, after=_state(con, f"discord_last_{ch}") or None)
        payloads = [{"username": (m.get("author") or {}).get("username", ""), "timestamp": m.get("timestamp", ""),
                     "embeds": m["embeds"]} for m in msgs if m.get("embeds")]
        scanned += len(msgs)
        if payloads:
            added += parsers.ingest(con, json.dumps(payloads), use_llm=use_llm)
        if msgs:
            _state(con, f"discord_last_{ch}", msgs[-1]["id"])
    con.commit()
    return added, scanned
