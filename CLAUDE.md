# Jigdatastream: instructions for any Claude session (cloud or Mac)

Read `HANDOFF.md` first: architecture, status, boundaries, next steps. `README.md` has run commands.

## Working across two places (cloud session and the user's Mac)
- Work on branch `claude/chat-session-a11vsi` unless the user says otherwise.
- **Start of every session:** `git fetch origin && git pull --ff-only` (if that fails, stop and show the user the conflict).
- **End of every task:** run `python -m pytest -q`, update `HANDOFF.md` ("Not done / next steps" and any new decisions),
  commit with a clear message, and push. The other place only sees what is pushed.
- Never rewrite history or force-push. If a pull would conflict, merge by file and tell the user.
- Do not open a pull request unless the user asks.

## Hard rules
- Never commit secrets: `.env`, tokens, passwords, license private keys, profile exports, webhook samples with private data.
  Redact samples with `jigstream/redact.py` before they go in the repo (`samples/` only holds redacted files).
- Never store card numbers/CVV/expiry. Last 4 digits only.
- Do not build address "jig" generation (variations to evade retailer purchase limits), with or without AI. Address
  features are validation, standardization and decline diagnosis only.
- The user is not a developer: give exact copy-paste Terminal steps and explain in plain language.
