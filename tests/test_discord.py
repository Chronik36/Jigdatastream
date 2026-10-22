import json
import os
from datetime import datetime, timezone

import jigstream.app as appmod
from jigstream import db, discord, logs
from jigstream.config import Config


BASE = int(discord.snowflake_for(datetime.now(timezone.utc))) - (86400_000 << 22)  # a day ago


def msg(n, embeds=None, content="", attachments=None, author="Valor AIO"):
    return {"id": str(BASE + n), "timestamp": "2026-10-20T10:00:00+00:00", "author": {"username": author},
            "content": content, "embeds": embeds or [], "attachments": attachments or []}


SUCCESS = {"title": "Successful Checkout", "timestamp": "2026-10-20T10:00:00Z",
           "fields": [{"name": "Product", "value": "Dunk Low"}, {"name": "Site", "value": "footlocker.com"},
                      {"name": "Profile", "value": "||prof1||"}, {"name": "Order #", "value": "ABC12345"}]}
DECLINE = {"title": "Card Declined", "description": "AVS mismatch", "timestamp": "2026-10-20T11:00:00Z",
           "fields": [{"name": "Product", "value": "Jordan 4"}, {"name": "Site", "value": "nike.com"},
                      {"name": "Profile", "value": "prof2"}]}
RAW = {"username": "Stellar", "embeds": [{"title": "Checkout Failed", "description": "Out of stock",
                                          "timestamp": "2026-10-20T12:00:00Z",
                                          "fields": [{"name": "Site", "value": "target.com"}]}]}


class FakeClient:
    def __init__(self, pages, error=None):
        self.pages = pages
        self.error = error
        self.afters = []

    def channel(self, cid):
        if self.error:
            raise discord.DiscordError(self.error)
        return {"name": f"chan{cid}"}

    def messages_after(self, cid, after):
        self.afters.append((cid, after))
        for page in self.pages.get(cid, []):
            yield [m for m in page if int(m["id"]) > int(after)]

    def download(self, url):
        return json.dumps(RAW)


def test_reads_embeds_pasted_json_and_attachments_once():
    con = db.connect(":memory:")
    pages = {"1": [[msg(101, [SUCCESS]), msg(102, [DECLINE], author="Stellar"),
                    msg(103, content="```json\n" + json.dumps(RAW) + "\n```"),
                    msg(104, attachments=[{"filename": "export.json", "url": "https://cdn/x", "size": 10}]),
                    msg(105, content="gm")]]}
    client = FakeClient(pages)
    counts = discord.sync_channels(con, client, ["1"])
    rows = {r["status"]: r for r in con.execute("SELECT * FROM checkouts")}
    assert counts["messages"] == 5 and counts["checkouts"] == 3  # attachment duplicates the pasted JSON
    assert rows["success"]["order_number"] == "ABC12345" and rows["success"]["bot"] == "Valor"
    assert rows["decline"]["bot"] == "Stellar" and rows["failure"]["site"] == "target.com"
    assert con.execute("SELECT COUNT(*) FROM discord_messages").fetchone()[0] == 5

    # First pull starts a year back; the next one resumes after the last message read.
    first_after = int(client.afters[0][1])
    year_ago = datetime.now(timezone.utc).timestamp() * 1000 - 365 * 86400_000
    assert abs(((first_after >> 22) + discord.DISCORD_EPOCH_MS) - year_ago) < 60_000
    discord.sync_channels(con, client, ["1"])
    assert client.afters[1] == ("1", str(BASE + 105))

    con.execute("DELETE FROM checkouts")
    assert discord.reparse(con) == 3


def test_channel_error_is_recorded_and_bad_token_stops():
    con = db.connect(":memory:")
    counts = discord.sync_channels(con, FakeClient({}, error="Discord rejected the bot token. Copy a fresh one."),
                                   ["1", "2"])
    assert counts["failed"] == ["1"]
    assert "token" in discord.channel_states(con)["1"]["error"]


def test_snowflake_round_trip():
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sf = int(discord.snowflake_for(when))
    assert (sf >> 22) + discord.DISCORD_EPOCH_MS == int(when.timestamp() * 1000)


def test_setup_connects_bot_and_saves_channels(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items()
                                        if not k.startswith(("GMAIL", "LLM", "DISCORD"))})
    monkeypatch.setattr(appmod, "installed_models", lambda url: [])
    token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.abcdef.ghijklmnopqrstuvwxyz0123456789"

    class Client:
        def __init__(self, t):
            if t != token:
                raise discord.DiscordError("Discord rejected the bot token. Copy a fresh one.")

        def me(self):
            return {"username": "Order Tracker", "id": "555"}

        def channels(self):
            return [("111", "My Server", "success-webhooks"), ("222", "My Server", "general")]

    monkeypatch.setattr(appmod.discord, "DiscordClient", Client)
    app = appmod.create_app(str(tmp_path / "d.db"))
    c = app.test_client()
    assert "Developer Portal" in c.get("/setup").get_data(as_text=True)
    r = c.post("/setup/discord/token", data={"token": "nope"}, follow_redirects=True)
    assert "rejected" in r.get_data(as_text=True)
    c.post("/setup/discord/token", data={"token": "Bot " + token})
    assert f"DISCORD_BOT_TOKEN={token}" in (tmp_path / ".env").read_text()
    page = c.get("/setup").get_data(as_text=True)
    assert "Connected as Order Tracker" in page and "#success-webhooks" in page and "client_id=555" in page

    monkeypatch.setattr(appmod.discord, "sync_channels", lambda *a, **k: {"messages": 0, "checkouts": 0, "failed": []})
    c.post("/setup/discord/channels", data={"channel": ["111"]})
    assert Config.from_env().discord_channel_ids == ["111"]
    assert token not in logs.redact(f"token is {token}")


def test_client_http(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    calls = []
    limited = {"done": False}
    ids = [str(BASE + n) for n in range(1, 151)]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers["Authorization"]))
            u = urlparse(self.path)
            if u.path.endswith("/messages"):
                if not limited["done"]:
                    limited["done"] = True
                    return self._send(429, {"retry_after": 0.01})
                after = int(parse_qs(u.query)["after"][0])
                newer = [i for i in ids if int(i) > after][:100]
                return self._send(200, [{"id": i} for i in reversed(newer)])  # Discord sends newest first
            if u.path == "/api/v10/users/@me":
                return self._send(401, {"message": "401: Unauthorized"}) if "bad" in self.headers["Authorization"] \
                    else self._send(200, {"id": "9", "username": "bot"})
            self._send(404, {})

        def _send(self, code, body):
            out = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(discord, "API", f"http://127.0.0.1:{server.server_port}/api/v10")
    try:
        client = discord.DiscordClient("tok")
        assert client.me()["username"] == "bot"
        pages = list(client.messages_after("7", str(BASE)))
        assert [len(p) for p in pages] == [100, 50]
        assert [m["id"] for p in pages for m in p] == ids  # oldest first, nothing skipped
        assert calls[0][1] == "Bot tok"
        try:
            discord.DiscordClient("bad").me()
            assert False
        except discord.DiscordError as e:
            assert "token" in str(e)
    finally:
        server.shutdown()
