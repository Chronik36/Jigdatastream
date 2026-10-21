# Jigdatastream

Local tool (Flask + SQLite) that reads your AIO bot Discord-webhook JSON (Valor, Stellar, Hayha, Polar),
classifies checkouts, cross-references them with your exported profiles, flags address problems on failing
profiles with a cleaned-up suggestion, and reads Gmail with a local AI model to track orders from any
retailer plus botting expenses (proxies, bots, whop.com and cook groups, captcha solvers, servers).

## Run
    pip install -r requirements.txt
    python run.py            # http://127.0.0.1:5000
    python -m pytest

Import webhook JSON and profile exports (.json/.csv) from the dashboard.

## Gmail order tracking (local AI)
Email content never leaves your Mac: Gmail is read over IMAP (read-only) and a local model does the extraction.

1. Install [Ollama](https://ollama.com) and pull a model: `ollama pull llama3.1:8b`
   (`qwen2.5:14b` is more accurate if you have 16 GB+ RAM). LM Studio or llama.cpp work too; any
   OpenAI-compatible endpoint does.
2. Create a Google App Password at https://myaccount.google.com/apppasswords (requires 2FA).
3. `cp .env.example .env` and fill in `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`.
4. `python -m jigstream check` confirms the model answers.
5. `python -m jigstream sync --days 30 --limit 50` for a first small run, or press "Sync Gmail" on the
   Orders page (it runs in the background).

For each order it captures retailer, order number, items, total, ship-to address, order date, tracking
number, carrier, shipping status and ETA. Confirmation, shipping and delivery emails for one order merge
into one row, and the newest email sets the status. Orders the retailer cancelled after a bot reported
success show on the dashboard. Each sync skips emails already processed and retries ones that failed.
JSON is at `/api/orders` and `/api/expenses`. Settings are listed in `.env.example`.

## Tuning parsers
Bots change embed layouts. Field-name aliases live in `jigstream/parsers.py` (`ALIASES`) and profile
column aliases in `jigstream/profiles.py` (`FIELD_ALIASES`). Add real samples under `samples/` and extend.

Data stays in `data/jigstream.db` (git-ignored). Keep webhook/profile exports out of the repo.
