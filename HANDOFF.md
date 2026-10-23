# BotTrack handoff (2026-10-23)

## 1. Goal
BotTrack is a self-installing Mac app that uses a local AI model (Ollama, default huihui_ai/qwen3-abliterated:14b, M4 16 GB) to read several Gmail inboxes, Spam included, over IMAP. It pulls out orders (tracking, address, dates, status) and botting expenses (proxies, bots, Whop). It also tracks bot checkouts as Drops, cross-checks them against cancel emails and card charges, and has a Keyword finder that builds +/- monitor lines from product pages. Everything is free: no paid APIs.

## 2. Current state
- Merged to main 2026-10-23: speed pass #51, editable keyword line #52, manual #53, Card check tab #54, clickable Drop rows and clean product names #55, DB lock fix #56, address grouping on Drops #57, Inbox infinite scroll #58, keyword accuracy plus Line check panel #59.
- Released: build 63 (has the lock fix). Build 64+ should hold #57–#59. Not yet confirmed on the real Mac.
- Untested on real data: Card check (test data only), keyword pass (saved page copies only, live store sites are blocked from the cloud).
- Open question: Drops "Best proxy" shows "(hidden)" because the Valor Proxy field couldn't be parsed safely. Waiting for a sample with the password replaced by xxxx.

## 3. Key files
- `jigstream/keywords.py`, `templates/keywords.html`: Keyword finder (/keywords).
- `jigstream/keywordrules.py`: per-store and per-brand rules (keyword_rules table) plus AI examples.
- `jigstream/drop.py`, `templates/drop.html`, `_drop_tabs.html`: Drops tab (/drop, /stuck, /checkouts).
- `jigstream/stuck.py`: matches checkouts against cancels and refunds (checkout_verdicts).
- `jigstream/parsers.py`: Discord webhook parsing (Hayha color rule, Valor follow-ups).
- `jigstream/extract.py`: AI extraction, cancel_signal, compact().
- `jigstream/mail.py`: IMAP sync, forwarded_headers.
- `jigstream/bank.py`, `plaid.py`, `bankpages.py`: card import and matching. Card check was added in #54.
- `jigstream/db.py`: setup_once migrations.
- `packaging/build_app.sh`, `.github/workflows/build-mac.yml`, `packaging/RELEASES.md`: Mac build and releases.

## 4. Decisions
- Releases build from **main** only. Branch claude/order-tracker-llm is stale: work put there once never shipped.
- Hayha "successful" text lies, so classify by embed color (green good, red decline).
- Cancels are rule-based and override the AI. Matches look back 60 days, because Pokémon Center cancels can come about a month later.
- Drop labels: Good is green, Unsure is orange, Cancelled is red. Real = Good + Unsure.
- Card check ties one charge to one checkout only when certain: Plaid swipe time within 30 minutes, the charge paid the order email, or the drop has one charge and one checkout. Otherwise it shows counts.
- Addresses are normalized to base line1 plus ZIP, with suite, apt, unit and jig prefixes removed.
- Keywords never negate spaced spellings of a + word (Nee Doh/NeeDoh) or the store name.
- Rejected: Tailscale (Cloudflare QR pairing instead), paid tracking APIs, an Icebox import.
- Bank data never leaves the Mac. No telemetry.

## 5. Next steps
1. Install the latest build, run a sync, then press Send logs. Confirm there are no "database is locked" errors and no model 500s.
2. Try Keyword finder on live gotoytime and Target pages, and tune from the Line check results.
3. Get a redacted Valor Proxy field and fix "(hidden)".
4. Check Card check against real statements. Each profile's card last 4 must match the card on the Bank page.
5. Still pending: a button to re-read the last 60 days of emails for the forwarded-address fix.
6. FUTURE, only on the owner's go: move confirmed-cancel emails to Trash, with a review step.

## 6. Gotchas
- The Mac build waits for a GitHub macOS runner. Updates come from the public repo Chronik36/BotTrack-releases, and the app updates through Setup > Check for updates > Update now.
- The cloud container can't download Actions artifacts (403).
- To benchmark speed, seed 400 profiles and 3,000 orders and time pages with the Flask test client.
- Don't add vendor lists: detect vendors generically.
