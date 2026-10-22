# Handoff: Jigdatastream (read this first)

Personal, local-only tool for one user (macOS, Mac Mini M4 16 GB). Flask + SQLite, no cloud. Branch: `claude/chat-session-a11vsi`
(repo `Chronik36/Jigdatastream`). `README.md` has run/setup commands; this file has context and decisions.

## Purpose
1. Read AIO bot Discord-webhook JSON (Valor, Stellar, Hayha, Polar), classify success / decline / failure.
2. Cross-reference with profiles exported from AYCD Icebox; flag address problems and suggest cleaned-up addresses.
3. Scan Gmail for retailer order emails (Target, Amazon, Walmart, Best Buy, P-Bandai, Crunchyroll, Topps, Pokemon Center,
   Shopify stores), link them to checkouts, and track cancellations (Target instant-cancels, Pokemon Center day-of-shipping).
4. Dashboard for optimizing the setup.

## Layout
| File | Role |
|---|---|
| `jigstream/parsers.py` | Webhook JSON -> checkouts. Heuristic alias lists (`ALIASES`), per-bot detection, status classifier. **Tuned on made-up samples only.** |
| `jigstream/profiles.py` | AYCD/bot profile JSON/CSV loader. Never stores card data (`_scrub`, last4 only). |
| `jigstream/address.py` | Normalize/diagnose/suggest (unit in line1, ZIP, state names, AVS street#/ZIP5 mismatch). |
| `jigstream/analysis.py` | Failing-profile report, per-site/bot stats. |
| `jigstream/emails.py` | Gmail IMAP (app password, read-only, X-GM-RAW search by retailer domain), subject-first classification. |
| `jigstream/tracker.py` | Matches successful checkouts to emails (order # or retailer+product+time within 72h); cancel speed buckets. |
| `jigstream/discord.py` | Bot-token channel sync: embeds, ```json blocks, .json attachments; incremental; stores last 500 raw messages for Live feed. |
| `jigstream/llm.py` | Optional AI (ollama / openai-compatible / anthropic) for gap-filling extraction, email classification, failure explanations. Output untrusted, whitelisted. |
| `jigstream/redact.py` | Strips webhook URLs, emails, card-like numbers, tokens before copying samples. |
| `jigstream/updater.py` | `git fetch` + fast-forward-only self-update, banner + optional auto-update. |
| `jigstream/licensing.py`, `tools/license_tool.py` | Dormant Ed25519 license keys; off unless `JIGSTREAM_REQUIRE_LICENSE=1`. |
| `jigstream/env.py`, `.env.example` | `.env` loader (git-ignored). |
| `jigstream/app.py`, `templates/`, `static/` | Flask UI: Dashboard, Checkouts, Order tracker, Profiles, Emails, Live feed, Import & sync, License. |
| `run.py` | Entry point + opt-in background threads (Gmail auto-sync, Discord polling, auto-update). |

Tests: `python -m pytest -q` (19 passing, all on synthetic data). Data lives in `data/jigstream.db` (git-ignored).

## Settings (`.env`)
`DISCORD_BOT_TOKEN`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `JIGSTREAM_DISCORD_POLL_SEC`, `JIGSTREAM_AUTO_SYNC_MIN`,
`JIGSTREAM_AUTO_UPDATE_MIN`, `JIGSTREAM_AI_PROVIDER` (+ `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`, `JIGSTREAM_MODEL`),
`JIGSTREAM_REQUIRE_LICENSE`. App URL: http://127.0.0.1:5000.

## Not done / next steps
- **Parsers have never seen real data.** Need redacted real webhook JSON per bot AND per retailer (the user's bots format
  differently per retailer). Use the Live feed -> "Copy redacted JSON" button, then adjust `ALIASES` / add per-bot overrides.
- **Profile generation for bots** (master list -> each bot's profile format, encrypted local vault for accounts/passwords):
  designed, not built; blocked on sample profile exports from each bot. Cards: last 4 only by default.
- Email parsers are generic; need real confirmation/cancellation emails per retailer to add per-retailer patterns and product names.
- `install.sh` one-step installer, launchd auto-start, in-app first-run setup wizard (writes `.env`): agreed, not built.
- Licensing is dormant; if the user ever sells this, add a validation/revocation server and package as a Mac app.
- User wants Ollama on the Mac (`brew install ollama`, `ollama pull llama3.1:8b`).
- Not verified against live Discord, Gmail, Ollama or real AYCD exports.

## Decisions and boundaries (keep these)
- **No address "jig" generation**, in the app or via any AI/provider: that is generating variations to evade retailer purchase limits.
  Address features are validation, standardization and diagnosing declines/cancellations only.
- Card numbers, CVV and expiry are never stored; secrets stay in `.env`/env vars, never committed or shared in chat.
- Discord: bot token only (user tokens violate Discord ToS); needs Message Content Intent so embeds are visible.
- AI is for extraction/classification/explanation only; its output is badged (robot icon) and never acted on.
- Updater only fast-forwards and refuses with local uncommitted changes.

## Working notes
- The user is not a developer; give exact copy-paste Terminal steps. Setup so far: Homebrew + `gh auth login` (HTTPS), clone
  of this branch, venv, `pip install -r requirements.txt`, `python run.py`.
- Another Claude Code thread ("Order tracker") exists in the user's Mac/desktop app; the user wants it to be the single
  continuing session. It should `git pull` this branch and treat this file as source of truth. If it has its own divergent
  code, merge by file rather than overwriting either side.
