# Jigdatastream

Local tool (Flask + SQLite) that reads your AIO bot Discord-webhook JSON (Valor, Stellar, Hayha, Polar),
classifies checkouts, cross-references them with your exported profiles, flags address problems on failing
profiles with a cleaned-up suggestion, and scans Gmail for retailer order emails.

## Run
    pip install -r requirements.txt
    python run.py            # http://127.0.0.1:5000
    python -m pytest

Import webhook JSON and profile exports (.json/.csv) from the dashboard.

## Gmail
Create a Google App Password (requires 2FA), then:
    export GMAIL_ADDRESS=you@gmail.com GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
and press "Sync Gmail". The mailbox is opened read-only.

## Tuning parsers
Bots change embed layouts. Field-name aliases live in `jigstream/parsers.py` (`ALIASES`) and profile
column aliases in `jigstream/profiles.py` (`FIELD_ALIASES`). Add real samples under `samples/` and extend.

Data stays in `data/jigstream.db` (git-ignored). Keep webhook/profile exports out of the repo.

## Auto-sync
Set `JIGSTREAM_AUTO_SYNC_MIN=15` (with the Gmail variables) before `python run.py` to poll Gmail every 15 minutes in the
background so cancellations appear without pressing Sync.

## Updates from GitHub
The dashboard checks your tracked branch on load and shows an "Update & restart" banner when new commits exist.
It only fast-forwards and refuses if you have local uncommitted changes. For hands-off updating set
`JIGSTREAM_AUTO_UPDATE_MIN=30` before `python run.py`. Your data (`data/`) is git-ignored, so updates never touch it.

## Local AI (optional, Ollama)
Install Ollama (https://ollama.com), then `ollama pull llama3.1:8b` (change with `JIGSTREAM_MODEL`; host via `OLLAMA_HOST`).
The dashboard shows whether it is reachable. Tick "AI for unrecognized" on import/sync to let the model fill gaps the
regular parsers miss; those rows get a 🤖 badge so you can verify them. "Explain" next to a failing profile gives a
plain-English diagnosis. Everything runs on your machine; card-like numbers are stripped before text reaches the model.

### Other AI providers
Set `JIGSTREAM_AI_PROVIDER` to `ollama` (default, fully local), `openai` (also any OpenAI-compatible server via
`OPENAI_BASE_URL`, e.g. LM Studio) or `anthropic`, plus `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` and optionally
`JIGSTREAM_MODEL`. Cloud providers receive the webhook/email text being analyzed (card-like numbers are stripped first).
Keys are read from the environment only and are never stored. AI is used for extraction, classification and
explanations only.

## Discord sync
Create a bot at https://discord.com/developers/applications, enable **Message Content Intent**, invite it to your own server
with View Channel + Read Message History, then `export DISCORD_BOT_TOKEN=...`. On the Import page enter the channel IDs
(Discord Developer Mode -> right-click channel -> Copy Channel ID) and press Sync Discord. Only new messages are fetched
after the first run. Use a bot token only; user tokens violate Discord's terms.

## Settings file
Copy `.env.example` to `.env` and fill in your tokens once; `run.py` loads it on every start. `.env` is git-ignored.

## Live mode
Set `JIGSTREAM_DISCORD_POLL_SEC=30` in `.env` to poll your Discord channels continuously. The **Live feed** page shows the
newest messages as raw JSON (embeds, ```json code blocks and attached .json files are all read) alongside what the app understood,
with a "Copy redacted JSON" button that strips webhook URLs, emails, card-like numbers and tokens before you share a sample.

## Licensing (off by default)
Dormant unless `JIGSTREAM_REQUIRE_LICENSE=1`. Keys are Ed25519-signed; the app holds only the public key.
Seller workflow (keep the private key OUT of this repo):
    python tools/license_tool.py keygen ~/secret/jds-private.key      # once; paste printed public key into jigstream/licensing.py
    python tools/license_tool.py issue ~/secret/jds-private.key "Customer" --days 365
Limits: a local check can be removed by editing the source and offline keys can't be revoked remotely
(`REVOKED` in licensing.py is a stopgap shipped via updates). Real enforcement needs a validation server.
