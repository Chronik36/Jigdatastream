"""Self-update from GitHub: check the update branch for a newer commit, download it, swap it in.

Only the app code folder is replaced. Settings (.env), the database (data/) and logs live
next to it and are never touched. The old code is kept in backups/ so an update can be rolled back.
"""
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("jigstream.update")

APP_DIR = Path(__file__).resolve().parent.parent  # holds jigstream/, run.py, VERSION
API = "https://api.github.com"
KEEP_BACKUPS = 3

state = {"checked_at": None, "available": False, "latest": None, "message": "", "error": None, "installing": False}


def settings():
    env = os.environ.get
    return {"repo": env("UPDATE_REPO", "Chronik36/Jigdatastream"), "branch": env("UPDATE_BRANCH", "main"),
            "token": env("GITHUB_TOKEN", "").strip()}


def installed_version(app_dir=APP_DIR):
    try:
        return (app_dir / "VERSION").read_text().strip()
    except OSError:
        return ""


def can_self_update(app_dir=APP_DIR):
    """Only an installed copy updates itself; a developer's git checkout is left alone."""
    return not (app_dir / ".git").exists()


def _get(url, token, accept="application/vnd.github+json", timeout=30):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "OrderTracker-updater",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _api(path, s):
    try:
        return json.loads(_get(f"{API}/repos/{s['repo']}/{path}", s["token"]))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("Can't see the update source. The repository is private, so add a GitHub "
                               "access token on the Setup page (or make the repository public).") from e
        if e.code in (401, 403):
            raise RuntimeError(f"GitHub refused the request ({e.code}). Check the GitHub token on the Setup page.") from e
        raise RuntimeError(f"GitHub answered {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach GitHub: {e.reason}") from e


def check(app_dir=APP_DIR, api=None):
    """Compare the installed commit with the update branch. Updates `state` and returns it."""
    s = settings()
    api = api or (lambda path: _api(path, s))
    current = installed_version(app_dir)
    state.update(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), error=None)
    try:
        head = api(f"commits/{s['branch']}")
        latest = head["sha"]
        newer = False
        if latest != current:
            if not current:
                newer = True
            else:
                cmp = api(f"compare/{current}...{latest}")
                # "ahead": the branch has new commits on top of ours. "diverged" happens after a
                # squash merge; then trust the dates so an older branch never replaces newer code.
                newer = cmp.get("status") == "ahead" or (
                    cmp.get("status") == "diverged"
                    and head["commit"]["committer"]["date"] > cmp["base_commit"]["commit"]["committer"]["date"])
        state.update(available=newer, latest=latest,
                     message=head["commit"]["message"].splitlines()[0][:120] if newer else "")
        logger.info("update check: installed %s, %s is at %s, update %s",
                    current[:7] or "unknown", s["branch"], latest[:7], "available" if newer else "not needed")
    except Exception as e:
        state.update(error=str(e))
        logger.warning("update check failed: %s", e)
    return state


def _download(sha, s):
    return _get(f"{API}/repos/{s['repo']}/zipball/{sha}", s["token"], timeout=120)


def unpack(blob, dest):
    """Extract the app files (jigstream/, run.py, requirements.txt) from a GitHub zipball into dest."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        root = names[0].split("/")[0] + "/"
        for n in names:
            rel = n[len(root):]
            if not rel or not (rel.startswith("jigstream/") or rel in ("run.py", "requirements.txt")):
                continue
            target = dest / rel
            if n.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(n))
    if not (dest / "jigstream" / "app.py").exists() or not (dest / "run.py").exists():
        raise RuntimeError("The download doesn't look like Order Tracker; nothing was changed.")


def backups(app_dir=APP_DIR):
    d = app_dir.parent / "backups"
    return sorted((p for p in d.glob("app-*") if p.is_dir()), reverse=True) if d.exists() else []


def install(app_dir=APP_DIR, sha=None, blob=None, pip=True):
    """Download sha (default: the latest found by check), swap it in, keep a backup. Returns the backup path."""
    s = settings()
    sha = sha or state.get("latest") or check(app_dir)["latest"]
    if not sha:
        raise RuntimeError(state.get("error") or "No update found.")
    logger.info("installing update %s", sha[:7])
    blob = blob if blob is not None else _download(sha, s)
    new = app_dir.parent / "app.new"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    unpack(blob, new)
    (new / "VERSION").write_text(sha + "\n")
    if pip and (new / "requirements.txt").exists():
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            "-r", str(new / "requirements.txt")], capture_output=True, text=True)
        if r.returncode:
            shutil.rmtree(new, ignore_errors=True)
            logger.error("pip failed during update: %s", r.stderr[-2000:])
            raise RuntimeError("Installing the new version's Python packages failed; nothing was changed. See Logs.")
    bdir = app_dir.parent / "backups"
    bdir.mkdir(exist_ok=True)
    backup = bdir / f"app-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{(installed_version(app_dir) or 'unknown')[:7]}"
    app_dir.rename(backup)
    new.rename(app_dir)
    for old in backups(app_dir)[KEEP_BACKUPS:]:
        shutil.rmtree(old, ignore_errors=True)
    state.update(available=False, message="")
    logger.info("update %s installed; previous version saved in %s", sha[:7], backup.name)
    return backup


def rollback(app_dir=APP_DIR):
    """Put the most recent backup back. The current code becomes a backup itself."""
    found = backups(app_dir)
    if not found:
        raise RuntimeError("There's no earlier version to go back to.")
    target = found[0]
    keep = app_dir.parent / "backups" / f"app-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{(installed_version(app_dir) or 'unknown')[:7]}-undone"
    app_dir.rename(keep)
    target.rename(app_dir)
    logger.info("rolled back to %s", target.name)
    return target


def restart_soon(delay=1.5):
    """Exit after the response is sent; launchd (KeepAlive) starts the new code."""
    def go():
        time.sleep(delay)
        logger.info("restarting to load the new version")
        logging.shutdown()
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()


def check_on_a_timer(hours):
    time.sleep(120)
    while True:
        check()
        time.sleep(hours * 3600)
