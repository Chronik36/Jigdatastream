"""Log files for troubleshooting: logs/app.log (rotating) beside the data folder.

App passwords are never written: anything matching a configured password is masked.
"""

import io
import logging
import os
import platform
import re
import smtplib
import ssl
import time
import zipfile
from datetime import datetime, timedelta
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import parse_accounts

LOG_DIR = Path(os.environ.get("JIGSTREAM_LOGS", "logs"))
# Where "Send logs to developer" emails the zip: BOTTRACK_LOGS_TO in .env, else the address the release was built
# with, else (only on the developer's own computer, the one with the signing key) the first connected inbox.
# No address is written in the code, which is public.
DEVELOPER_EMAIL = ""
SMTP_HOST = "smtp.gmail.com"
NOTE_MIN_CHARS = 20  # "Send logs" needs a short description of the problem
SEND_MAX_ZIP = 15_000_000  # Gmail refuses mail over 25 MB, and attachments grow by a third when sent
SENDS_FILE = "sends.txt"  # one line per Send logs attempt and how it went, shown on the Logs page
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def secrets():
    """Every saved password, key, token and private channel name, so none of them is ever written to a log."""
    from .config import SEALED_KEYS
    words = [pw for _, pw in parse_accounts(os.environ.get("GMAIL_ACCOUNTS", ""))]
    if os.environ.get("GMAIL_APP_PASSWORD"):
        words.append(os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
    words += [os.environ[k].strip() for k in sorted(SEALED_KEYS - {"GMAIL_ACCOUNTS", "GMAIL_APP_PASSWORD"})
              if os.environ.get(k)]
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


CARD_RE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
TOKEN_RE = re.compile(r"\b(access|public|link)-(sandbox|development|production)-[0-9a-f-]{8,}", re.I)
BEARER_RE = re.compile(r"(?i)(authorization:\s*bearer\s+|\bbearer\s+)[A-Za-z0-9._~+/=-]{12,}")
LINK_RE = re.compile(r"https?://[a-z0-9-]+\.trycloudflare\.com", re.I)  # where this computer can be reached
PAIR_RE = re.compile(r"([?&](?:k|t)=)[A-Za-z0-9_-]{12,}")  # a pairing link's key and channel


def _luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _mask_card(m):
    digits = re.sub(r"\D", "", m.group())
    return "[card]" if _luhn(digits) else m.group()


def scrub(text):
    """redact() plus anything shaped like a card number, Plaid token or bearer token. Used for every zip."""
    text = redact(text)
    text = CARD_RE.sub(_mask_card, text)
    text = TOKEN_RE.sub("[plaid token]", text)
    text = LINK_RE.sub("[phone link]", text)
    text = PAIR_RE.sub(lambda m: m.group(1) + "********", text)
    return BEARER_RE.sub(lambda m: m.group(1) + "********", text)


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


def zip_logs(log_dir=None, about=None, note="", max_bytes=None):
    """All log files in one zip, passwords and card numbers masked, for sending to whoever is helping.
    With max_bytes, the newest files go first and older ones are left out once the zip reaches that size."""
    d = Path(log_dir or LOG_DIR)
    buf = io.BytesIO()
    left_out = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(log_files(d), key=lambda n: -(d / n).stat().st_mtime):
            if max_bytes and buf.tell() >= max_bytes:
                left_out.append(name)
                continue
            z.writestr(name, scrub((d / name).read_text("utf-8", "replace")))
        if left_out:
            z.writestr("left-out.txt", "Too big to email, not included: " + ", ".join(left_out) + "\n")
        if note.strip():
            z.writestr("problem.txt", scrub(note.strip()) + "\n")
        if about:
            z.writestr("about.txt", "".join(f"{k}: {v}\n" for k, v in about.items()))
    return buf.getvalue()


RECORD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]?\d*\s+([A-Z]+)\b")
EMAIL_TAIL_LINES = 200
EMAIL_MAX_ERRORS = 50  # newest error blocks kept in the email body
EMAIL_MAX_CHARS = 60_000  # per section, so the email stays readable


def recent_errors(log_dir=None, hours=24, now=None):
    """ERROR/CRITICAL records and anything with a Traceback from the last `hours`, oldest first, scrubbed."""
    d = Path(log_dir or LOG_DIR)
    now = now or datetime.now()
    since = now - timedelta(hours=hours)
    found = []
    for name in sorted(log_files(d), key=lambda n: (d / n).stat().st_mtime):
        path = d / name
        file_recent = path.stat().st_mtime >= time.mktime(since.timetuple())
        block, when, level = [], None, ""

        def flush():
            text = "\n".join(block).rstrip()
            recent = when >= since if when else file_recent
            if text and recent and (level in ("ERROR", "CRITICAL") or "Traceback (most recent call last)" in text):
                found.append((when or datetime.fromtimestamp(path.stat().st_mtime), name, text))

        for line in path.read_text("utf-8", "replace").splitlines():
            m = RECORD_RE.match(line)
            if m:
                flush()
                block, level = [line], m.group(2)
                try:
                    when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    when = None
            else:
                block.append(line)
        flush()
    found.sort(key=lambda f: f[0])
    return [(name, scrub(text)) for _, name, text in found[-EMAIL_MAX_ERRORS:]]


def _cap(text, limit=EMAIL_MAX_CHARS):
    return text if len(text) <= limit else "[... cut, the full logs are in the zip ...]\n" + text[-limit:]


def email_body(note, info, log_dir=None):
    """The email text: the note, versions, recent errors and the end of app.log, so it can be read without the zip."""
    d = Path(log_dir or LOG_DIR)
    errors = recent_errors(d)
    tail_text = scrub(tail("app.log", EMAIL_TAIL_LINES, d))
    parts = ["What went wrong:", note, "", "".join(f"{k}: {v}\n" for k, v in info.items())]
    parts += [f"===== Errors and tracebacks, last 24 hours ({len(errors)}) =====",
              _cap("\n\n".join(f"[{name}] {text}" for name, text in errors)) if errors else "None.", ""]
    parts += [f"===== Last {EMAIL_TAIL_LINES} lines of app.log =====", _cap(tail_text) or "app.log is empty.", "",
              "The full logs are in the attached zip."]
    return "\n".join(parts)


def _built_in_address():
    """The developer address a release was built with (developer.txt, from a build secret; never in the code)."""
    try:
        return (Path(__file__).with_name("developer.txt").read_text().strip().splitlines() or [""])[0]
    except OSError:
        return ""


def developer_email():
    to = (os.environ.get("BOTTRACK_LOGS_TO") or DEVELOPER_EMAIL or _built_in_address()).strip()
    # A friend's copy without a built-in address used to email its own inbox, so the logs never reached anyone.
    if not to and os.environ.get("BOTTRACK_SIGNING_KEY", "").strip():
        accounts = parse_accounts(os.environ.get("GMAIL_ACCOUNTS", ""))
        to = accounts[0][0] if accounts else ""
    return to


def about(version="", engine="", model=""):
    """What's sent beside the logs: versions only, nothing about the user."""
    return {"BotTrack": version or "unknown", "macOS": platform.mac_ver()[0] or platform.platform(),
            "Machine": platform.machine(), "Python": platform.python_version(), "AI engine": engine, "AI model": model}


def note_problem(note):
    """Why a note can't be sent yet, or None when it's fine."""
    if len((note or "").strip()) < NOTE_MIN_CHARS:
        return f"Describe what went wrong in at least {NOTE_MIN_CHARS} characters before sending."
    return None


def send_logs(sender, password, note, about_info=None, log_dir=None, to=None, smtp=None):
    """Email the logs zip from the user's own Gmail (the app password they already use for syncing)."""
    problem = note_problem(note)
    if problem:
        raise ValueError(problem)
    note = scrub(note.strip())
    to = to or developer_email()
    if not to:
        raise RuntimeError("No developer address is set.")
    info = about_info or {}
    msg = EmailMessage()
    msg["Subject"] = f"BotTrack logs ({info.get('BotTrack', 'unknown build')})"
    msg["From"], msg["To"], msg["Reply-To"] = sender, to, sender
    msg.set_content(email_body(note, info, log_dir))
    msg.add_attachment(zip_logs(log_dir, info, note, SEND_MAX_ZIP), maintype="application", subtype="zip",
                       filename="bottrack-logs.zip")
    if smtp is None:
        smtp = smtplib.SMTP_SSL(SMTP_HOST, 465, context=ssl.create_default_context(), timeout=30)
    with smtp as s:
        s.login(sender, password)
        s.send_message(msg)
    return to


def record_send(sender, to, error=None, log_dir=None):
    """Note a Send logs attempt and how it went, so the person can see it was sent (or why not)."""
    d = Path(log_dir or LOG_DIR)
    line = "\t".join([datetime.now().strftime("%Y-%m-%d %H:%M"), sender or "", to or "",
                      "failed: " + redact(str(error))[:300].replace("\n", " ") if error else "sent"])
    try:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / SENDS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def recent_sends(n=5, log_dir=None):
    """The last n Send logs attempts, newest first: [{when, sender, to, result}]."""
    try:
        lines = (Path(log_dir or LOG_DIR) / SENDS_FILE).read_text("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-n:]):
        parts = (line.split("\t") + ["", "", "", ""])[:4]
        out.append(dict(zip(("when", "sender", "to", "result"), parts)))
    return out
