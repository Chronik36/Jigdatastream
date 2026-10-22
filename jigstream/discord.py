"""Pull bot checkout webhooks and raw JSON from Discord channels with a bot you create.

Discord only lets a bot read channels in servers it has been added to, and only sees
message text and embeds when its "Message Content Intent" is switched on. Both are free
and done once in the Discord Developer Portal; the Setup page walks through it.

Every message read is kept as raw JSON in discord_messages, so improving the parsers
later can re-run over old messages without fetching them again.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import parsers

logger = logging.getLogger("jigstream.discord")

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
TEXT_CHANNELS = (0, 5)  # text and announcement channels

SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_channels (
    channel_id TEXT PRIMARY KEY,
    name TEXT,
    server TEXT,
    last_message_id TEXT,
    last_synced_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS discord_messages (
    message_id TEXT PRIMARY KEY,
    channel_id TEXT,
    ts TEXT,
    author TEXT,
    raw TEXT
);
"""


class DiscordError(RuntimeError):
    pass


def snowflake_for(when):
    """The smallest message id Discord could have issued at `when`."""
    return str((int(when.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22)


class DiscordClient:
    def __init__(self, token, timeout=20):
        self.token = token.strip()
        self.timeout = timeout

    def get(self, path, attempts=5):
        for _ in range(attempts):
            req = urllib.request.Request(API + path, headers={
                "Authorization": f"Bot {self.token}",
                "User-Agent": "DiscordBot (https://github.com/Chronik36/Jigdatastream, 1.0)",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.load(resp)
                    # Stay under the per-route limit instead of waiting to be told off.
                    if resp.headers.get("X-RateLimit-Remaining") == "0":
                        time.sleep(min(float(resp.headers.get("X-RateLimit-Reset-After", 1)), 30))
                    return data
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code == 429:
                    try:
                        wait = float(json.loads(body).get("retry_after", 2))
                    except ValueError:
                        wait = 2
                    time.sleep(min(wait, 60))
                    continue
                if e.code == 401:
                    raise DiscordError("Discord rejected the bot token. Copy a fresh one from the Developer Portal.")
                if e.code == 403:
                    raise DiscordError("The bot can't see this channel. Give it View Channel and Read Message "
                                       "History there.")
                if e.code == 404:
                    raise DiscordError("Channel not found, or the bot isn't in that server.")
                raise DiscordError(f"Discord error {e.code}: {body}")
            except (urllib.error.URLError, OSError) as e:
                raise DiscordError(f"Could not reach Discord: {e}")
        raise DiscordError("Discord kept rate-limiting; will try again next sync.")

    def me(self):
        return self.get("/users/@me")

    def channels(self):
        """Text channels in every server the bot is in: [(channel_id, server name, channel name)]."""
        out = []
        for guild in self.get("/users/@me/guilds"):
            for ch in self.get(f"/guilds/{guild['id']}/channels"):
                if ch.get("type") in TEXT_CHANNELS:
                    out.append((ch["id"], guild.get("name", ""), ch.get("name", "")))
        return out

    def channel(self, channel_id):
        return self.get(f"/channels/{channel_id}")

    def messages_after(self, channel_id, after):
        """Yield pages of messages newer than message id `after`, oldest first."""
        while True:
            page = self.get(f"/channels/{channel_id}/messages?limit=100&after={after}")
            if not page:
                return
            page.sort(key=lambda m: int(m["id"]))
            yield page
            after = page[-1]["id"]
            if len(page) < 100:
                return

    def download(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "OrderTracker (local app, 1.0)"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read(5_000_000).decode("utf-8", "replace")


def json_blocks(content):
    """Raw JSON pasted into a message, with or without ``` fences."""
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", content or "", re.S) or [content or ""]
    return [b.strip() for b in blocks if b.strip()[:1] in ("{", "[")]


def ingest_message(con, client, message):
    """Turn one Discord message into checkout rows. Returns how many new rows it added."""
    payload = dict(message, username=(message.get("author") or {}).get("username", ""))
    added = parsers.ingest(con, json.dumps([payload])) if message.get("embeds") else 0
    texts = json_blocks(message.get("content"))
    for att in message.get("attachments") or []:
        if att.get("filename", "").lower().endswith((".json", ".txt")) and att.get("size", 0) < 5_000_000:
            try:
                texts.append(client.download(att["url"]))
            except (urllib.error.URLError, OSError) as e:
                logger.warning("could not download attachment %s: %s", att.get("filename"), e)
    for text in texts:
        try:
            added += parsers.ingest(con, text)
        except (ValueError, TypeError, AttributeError) as e:
            logger.info("message %s has JSON the parser couldn't use: %s", message.get("id"), e)
    return added


def sync_channels(con, client, channel_ids, first_sync_days=365, log=lambda msg: None):
    """Read new messages from each channel. Returns {"messages": n, "checkouts": n, "failed": [...]}."""
    con.executescript(SCHEMA)
    counts = {"messages": 0, "checkouts": 0, "failed": []}
    for channel_id in channel_ids:
        row = con.execute("SELECT * FROM discord_channels WHERE channel_id = ?", (channel_id,)).fetchone()
        after = row["last_message_id"] if row and row["last_message_id"] else snowflake_for(
            datetime.now(timezone.utc) - timedelta(days=first_sync_days))
        try:
            info = client.channel(channel_id)
            name = info.get("name", channel_id)
            con.execute("INSERT OR IGNORE INTO discord_channels (channel_id) VALUES (?)", (channel_id,))
            con.execute("UPDATE discord_channels SET name = ?, error = NULL WHERE channel_id = ?", (name, channel_id))
            seen = added = 0
            for page in client.messages_after(channel_id, after):
                if not page:
                    continue
                for m in page:
                    con.execute("INSERT OR IGNORE INTO discord_messages VALUES (?,?,?,?,?)",
                                (m["id"], channel_id, m.get("timestamp"),
                                 (m.get("author") or {}).get("username"), json.dumps(m)))
                    added += ingest_message(con, client, m)
                seen += len(page)
                # Save progress per page so an interrupted first pull resumes where it stopped.
                con.execute("UPDATE discord_channels SET last_message_id = ? WHERE channel_id = ?",
                            (page[-1]["id"], channel_id))
                con.commit()
            con.execute("UPDATE discord_channels SET last_synced_at = ? WHERE channel_id = ?",
                        (datetime.now(timezone.utc).isoformat(), channel_id))
            con.commit()
            counts["messages"] += seen
            counts["checkouts"] += added
            logger.info("#%s: %d new messages, %d new checkouts", name, seen, added)
            log(f"#{name}: {seen} new messages, {added} new checkouts")
        except DiscordError as e:
            con.execute("INSERT OR IGNORE INTO discord_channels (channel_id) VALUES (?)", (channel_id,))
            con.execute("UPDATE discord_channels SET error = ? WHERE channel_id = ?", (str(e), channel_id))
            con.commit()
            counts["failed"].append(channel_id)
            logger.warning("channel %s: %s", channel_id, e)
            log(f"! channel {channel_id}: {e}")
            if "token" in str(e) or "reach Discord" in str(e):
                break
    return counts


def reparse(con):
    """Run the checkout parsers again over every stored Discord message (after improving them)."""
    con.executescript(SCHEMA)
    n = 0
    for row in con.execute("SELECT raw FROM discord_messages").fetchall():
        m = json.loads(row["raw"])
        payload = dict(m, username=(m.get("author") or {}).get("username", ""))
        if m.get("embeds"):
            n += parsers.ingest(con, json.dumps([payload]))
        for text in json_blocks(m.get("content")):
            try:
                n += parsers.ingest(con, text)
            except (ValueError, TypeError, AttributeError):
                pass
    return n


def channel_states(con):
    con.executescript(SCHEMA)
    return {r["channel_id"]: dict(r) for r in con.execute("SELECT * FROM discord_channels")}
