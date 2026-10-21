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
