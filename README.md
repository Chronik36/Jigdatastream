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
