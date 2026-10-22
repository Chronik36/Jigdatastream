"""Log files for troubleshooting: logs/app.log (rotating) beside the data folder.

App passwords are never written: anything matching a configured password is masked.
"""

import io
import logging
import os
import re
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import parse_accounts

LOG_DIR = Path(os.environ.get("JIGSTREAM_LOGS", "logs"))
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def secrets():
    words = [pw for _, pw in parse_accounts(os.environ.get("GMAIL_ACCOUNTS", ""))]
    if os.environ.get("DISCORD_BOT_TOKEN"):
        words.append(os.environ["DISCORD_BOT_TOKEN"].strip())
    if os.environ.get("GITHUB_TOKEN"):
        words.append(os.environ["GITHUB_TOKEN"].strip())
    if os.environ.get("GMAIL_APP_PASSWORD"):
        words.append(os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
    return [w for w in words if len(w) >= 6]


def redact(text):
    for word in secrets():
        if len(word) > 30:  # bot tokens: exact match is enough
            text = text.replace(word, "********")
            continue
        # App passwords are often shown in groups of four ("abcd efgh ..."), so match spaced forms too.
        pattern = r"\s?".join(re.escape(ch) for ch in word)
        text = re.sub(pattern, "********", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup_logging(log_dir=None, level=logging.INFO):
    log_dir = Path(log_dir or LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_jigstream", False) for h in root.handlers):
        return log_dir
    handler = RotatingFileHandler(log_dir / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(RedactingFormatter(FORMAT))
    handler._jigstream = True
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # skip one line per page view
    return log_dir


def tail(name="app.log", lines=300, log_dir=None):
    path = Path(log_dir or LOG_DIR) / name
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 400_000))
        text = f.read().decode("utf-8", "replace")
    return redact("\n".join(text.splitlines()[-lines:]))


def log_files(log_dir=None):
    d = Path(log_dir or LOG_DIR)
    return sorted(p.name for p in d.glob("*.log*")) if d.exists() else []


def zip_logs(log_dir=None):
    """All log files in one zip, passwords masked, for sending to whoever is helping."""
    d = Path(log_dir or LOG_DIR)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in log_files(d):
            z.writestr(name, redact((d / name).read_text("utf-8", "replace")))
    return buf.getvalue()
