"""Settings, read from environment variables or a local .env file."""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_QUERY = (
    "(order OR shipped OR shipping OR delivered OR tracking OR receipt OR invoice"
    " OR purchase OR subscription OR payment OR whop OR proxies)"
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


@dataclass
class Config:
    gmail_address: str = ""
    gmail_app_password: str = ""
    imap_host: str = "imap.gmail.com"
    mailbox: str = '"[Gmail]/All Mail"'
    search_query: str = DEFAULT_QUERY
    days_back: int = 90
    max_emails: int = 500

    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "llama3.1:8b"
    llm_api_key: str = "ollama"
    llm_timeout: int = 180
    max_body_chars: int = 12000


    @classmethod
    def from_env(cls):
        load_dotenv()
        env = os.environ.get
        c = cls()
        c.gmail_address = env("GMAIL_ADDRESS", c.gmail_address)
        c.gmail_app_password = env("GMAIL_APP_PASSWORD", c.gmail_app_password).replace(" ", "")
        c.imap_host = env("IMAP_HOST", c.imap_host)
        c.mailbox = env("IMAP_MAILBOX", c.mailbox)
        c.search_query = env("GMAIL_QUERY", c.search_query)
        c.days_back = int(env("DAYS_BACK", c.days_back))
        c.max_emails = int(env("MAX_EMAILS", c.max_emails))
        c.llm_base_url = env("LLM_BASE_URL", c.llm_base_url).rstrip("/")
        c.llm_model = env("LLM_MODEL", c.llm_model)
        c.llm_api_key = env("LLM_API_KEY", c.llm_api_key)
        c.llm_timeout = int(env("LLM_TIMEOUT", c.llm_timeout))
        c.max_body_chars = int(env("MAX_BODY_CHARS", c.max_body_chars))
        return c
