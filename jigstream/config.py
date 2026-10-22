"""Settings, read from environment variables or a local .env file."""

import os
from dataclasses import dataclass, field
from pathlib import Path

# Broad on purpose: the model decides what is an order or a botting receipt, so the
# query only needs to skip mail that is clearly neither. Stripe, Paddle, Sellix, Shopify
# and whop cover how most proxy and bot sellers send receipts.
DEFAULT_QUERY = (
    "(order OR shipped OR shipping OR delivered OR delivery OR tracking OR receipt OR invoice"
    " OR purchase OR subscription OR payment OR paid OR renewal OR renew OR license OR billing"
    " OR charge OR proxies OR proxy OR whop OR stripe OR paddle OR sellix OR shopify)"
    " -in:spam -in:trash"
)


def load_dotenv(path=".env"):
    """Load KEY=VALUE lines into os.environ without overriding real env vars."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def save_env(updates, path=".env"):
    """Set KEY=VALUE lines in the .env file (creating it, readable only by you) and in os.environ."""
    p = Path(path)
    lines = p.read_text().splitlines() if p.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and "=" in line and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in remaining.items()]
    p.write_text("\n".join(out) + "\n")
    p.chmod(0o600)
    for key, value in updates.items():
        os.environ[key] = str(value)


def format_accounts(accounts):
    return ", ".join(f"{address}:{password}" for address, password in accounts)


def parse_accounts(value):
    """'a@gmail.com:app pass, b@gmail.com:app pass' -> [(address, password), ...]"""
    accounts = []
    for entry in (value or "").split(","):
        if ":" not in entry:
            continue
        address, password = entry.split(":", 1)
        if address.strip() and password.strip():
            accounts.append((address.strip(), password.replace(" ", "").strip()))
    return accounts


@dataclass
class Config:
    accounts: list = field(default_factory=list)  # [(address, app_password)], synced in order
    imap_host: str = "imap.gmail.com"
    mailbox: str = '"[Gmail]/All Mail"'
    search_query: str = DEFAULT_QUERY
    first_sync_days: int = 365  # how far back an inbox's first sync reaches
    days_back: int = None  # set by --days to override the above for one run
    full_history: bool = False  # set by --all: search the whole mailbox
    max_emails: int = 0  # per inbox per run; 0 means no limit

    llm_engine: str = "ollama"  # or "mlx" (Apple's engine; the Mac app runs whichever is chosen)
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "huihui_ai/qwen3-abliterated:14b"
    llm_api_key: str = "ollama"
    llm_timeout: int = 300  # a 14B model on a 16 GB Mac can take a while on long emails
    max_body_chars: int = 12000

    training_mode: bool = True  # status dropdowns on Orders; corrections become model examples
    use_examples: bool = True  # show saved corrections to the model before each email

    discord_bot_token: str = ""
    discord_channel_ids: list = field(default_factory=list)  # channels holding bot checkout webhooks

    @classmethod
    def from_env(cls):
        load_dotenv()
        env = os.environ.get
        c = cls()
        c.accounts = parse_accounts(env("GMAIL_ACCOUNTS", ""))
        if env("GMAIL_ADDRESS") and env("GMAIL_APP_PASSWORD"):
            single = (env("GMAIL_ADDRESS").strip(), env("GMAIL_APP_PASSWORD").replace(" ", ""))
            if single[0] not in [a for a, _ in c.accounts]:
                c.accounts.insert(0, single)
        c.imap_host = env("IMAP_HOST", c.imap_host)
        c.mailbox = env("IMAP_MAILBOX", c.mailbox)
        c.search_query = env("GMAIL_QUERY", c.search_query)
        c.first_sync_days = int(env("FIRST_SYNC_DAYS", c.first_sync_days))
        c.max_emails = int(env("MAX_EMAILS", c.max_emails))
        c.llm_base_url = env("LLM_BASE_URL", c.llm_base_url).rstrip("/")
        c.llm_model = env("LLM_MODEL", c.llm_model)
        c.llm_engine = env("LLM_ENGINE", c.llm_engine)
        c.llm_api_key = env("LLM_API_KEY", c.llm_api_key)
        c.llm_timeout = int(env("LLM_TIMEOUT", c.llm_timeout))
        c.max_body_chars = int(env("MAX_BODY_CHARS", c.max_body_chars))
        c.training_mode = env("TRAINING_MODE", "1") == "1"
        c.use_examples = env("USE_TRAINING_EXAMPLES", "1") == "1"
        c.discord_bot_token = env("DISCORD_BOT_TOKEN", "").strip()
        c.discord_channel_ids = [x.strip() for x in env("DISCORD_CHANNEL_IDS", "").split(",") if x.strip()]
        return c
