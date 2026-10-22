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

1. With [Ollama](https://ollama.com) installed, pull a model: `ollama pull huihui_ai/qwen3-abliterated:14b` (the default).
   On a 16 GB Mac, 14B (about 9 GB) is the largest that fits comfortably; 8B models are faster (set `LLM_MODEL` to switch);
   12B and larger will swap memory and crawl. Emails are sent to the model one at a time.
   LM Studio or llama.cpp work too; any OpenAI-compatible endpoint does.
2. Create a Google App Password for each inbox at https://myaccount.google.com/apppasswords (requires 2FA).
3. `cp .env.example .env` and list your inboxes in `GMAIL_ACCOUNTS` as `address:app password`, comma-separated.
4. `python -m jigstream check` confirms the model answers.
5. Sync from the Orders page, or from the terminal:

        python -m jigstream sync --limit 50     # quick trial: 50 new emails per inbox
        python -m jigstream sync                # every inbox in turn
        python -m jigstream sync --all          # whole-mailbox backfill
        python -m jigstream sync --inbox you@gmail.com

**How far back.** An inbox's first sync covers the last year. After that, each sync only searches mail since the
previous one. "Backfill whole inboxes" (or `--all`) searches each entire mailbox. Processed emails are
remembered, so a backfill that is stopped part way picks up where it left off, and it never repeats model work.

**Spam folder.** Each inbox's Spam folder is read too, since forwarded receipts often land there (Trash is
not). Gmail deletes spam after 30 days, so sync at least every couple of weeks to catch it.

**What gets read.** Gmail's search narrows mail to anything with order, shipping, receipt, invoice,
subscription or payment wording. A quick local check then skips emails with no price or tracking number.
Only the rest go to the model, which is the slow step (a few seconds per email on Apple silicon).

**Orders.** Retailer, order number, items, total, ship-to address, order date, tracking number, carrier,
status, ETA and which inbox it came from. Confirmation, shipping and delivery emails for one order merge into
one row, and the newest email sets the status. Orders the retailer cancelled after a bot reported success
show on the dashboard.

**Botting expenses.** There is no vendor list. The model decides from the receipt itself whether it is for
proxies, bots, cook groups, captcha solving, servers, ACO services or accounts, so new providers are picked
up automatically. It names the actual seller even when the receipt comes from Stripe, PayPal, Whop or a
similar platform, and records whether the charge is one-time, monthly and so on. The page totals spend by
category, month and vendor.

**Tracking links.** Shipping status comes from the retailer's emails; nothing paid or polled. Each
tracking number links to the carrier's free public tracking page: the link from the email itself when
there is one, otherwise one built for UPS, USPS, FedEx, DHL or OnTrac.

JSON is at `/api/orders` and `/api/expenses`.

## Discord checkouts
Instead of exporting webhook JSON by hand, the app can read your bots' checkout webhooks straight from Discord.
On the Setup page, paste the token of a Discord bot you create (free, in the Developer Portal, with
**Message Content Intent** on), add it to your server with the link shown, and tick the channels to read.
Each sync reads new messages: the first pull covers the last year, then it resumes after the last message read.
Embeds go through the same parsers as imported webhook JSON, as does JSON pasted in a message or attached as a
`.json` file. Every message is also kept raw in `discord_messages`, so after improving the parsers you can run
`python -m jigstream reparse-discord` without fetching anything again. `python -m jigstream discord` runs a
Discord-only sync.

## Bank and card charges
The **Bank & card charges** page (linked from Expenses, at `/bank`) shows which order or botting receipt each card
charge paid for. Two ways to bring charges in, both free:

- **Statement files.** On your bank or card site, download the account activity as CSV, or as Quicken/QFX or OFX,
  and import it. Chase, Amex, Capital One, Citi, Discover and most banks' exports work: the columns are found by name
  (Date / Description / Amount, or separate Debit / Credit columns), lines above the header are skipped, and whether
  charges show as negative or positive is detected (or set by hand). Importing the same file twice adds nothing.
- **Plaid, with your own keys (optional).** Make a free account at dashboard.plaid.com, paste its client_id and
  secret at `/bank/plaid`, and sign in to your bank in Plaid's window. Start with Sandbox (test bank `user_good` /
  `pass_good`), then switch to Production once Plaid approves it. Charges from the last two years are pulled, then
  new ones every `PLAID_SYNC_HOURS` (default 6) alongside the scheduled email sync. Keys and bank tokens are stored
  encrypted with the same Keychain-held key as cards and never logged.

Each charge is matched when it has the same amount to the cent as an order or receipt dated up to 2 days after or 7
days before it, and either the store name appears in the bank's description or it's the only candidate. Ambiguous
charges are left for you. Match, Unmatch, Ignore (groceries, rent) and Auto-match are on every row, and a choice you
make is never undone by a later import or sync. A charge with no email receipt can be added as a botting expense.
Pending Plaid charges wait until they post, and a match you made on a pending charge moves to the posted one.

## Profiles

Profiles show as cards with edit and delete. **Export** writes profiles in any bot's file layout:
the layout comes from a profile previously imported from that bot (card values are blanked when
stored, so the keys survive as a template). Card numbers are never stored; uploading the original
bot export alongside an export copies its card details into the download in memory only.

## Mac app (BotTrack.app) and updates

`packaging/build_app.sh` (run on Apple Silicon by `.github/workflows/build-mac.yml`) builds a
self-contained `BotTrack.app` and `BotTrack.dmg`: a standalone Python with Flask and pywebview,
this code, and the Ollama engine. `jigstream/macapp.py` runs it all while the window is open:
the dashboard on localhost, the bundled engine on port 11435 with models in
`~/Library/Application Support/BotTrack/models` (reusing a model already in `~/.ollama` via APFS
clones), timed syncs and update checks. It also moves data over from the old installer version.

`jigstream/updater.py` compares the running `VERSION` with `UPDATE_BRANCH` (default `main`) of
`UPDATE_REPO`; in the app, updates are unpacked to the data folder (`app/`, with packages in
`pydeps/`), so the bundle is never modified, and "Undo last update" falls back to older code.
A private repo needs `GITHUB_TOKEN` (fine-grained, Contents read-only).

## Tuning parsers
Bots change embed layouts. Field-name aliases live in `jigstream/parsers.py` (`ALIASES`) and profile
column aliases in `jigstream/profiles.py` (`FIELD_ALIASES`). Add real samples under `samples/` and extend.

Data stays in `data/jigstream.db` (git-ignored). Keep webhook/profile exports out of the repo.
