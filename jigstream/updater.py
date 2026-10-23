"""Self-update: check the public releases repository for a newer build, download its code, swap it in.

Every build of the main branch is published as a GitHub Release in a small public repository that
holds only releases, so no token is needed to check or update. (Developers can instead follow a
branch of the private code repository with UPDATE_SOURCE=branch and a GITHUB_TOKEN.)

Only the app code folder is replaced. Settings (.env), the database (data/) and logs live
next to it and are never touched. The old code is kept in backups/ so an update can be rolled back.
"""
import io
import json
import logging
import os
import re
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

RUNNING_DIR = Path(__file__).resolve().parent.parent  # the code running now: jigstream/, run.py, VERSION
# Where updates go. Inside BotTrack.app that's the data folder, so the app bundle itself is never changed
# and the code shipped in the bundle stays as the fallback.
APP_DIR = Path(os.environ["BOTTRACK_CODE_DIR"]) if os.environ.get("BOTTRACK_CODE_DIR") else RUNNING_DIR
relaunch = None  # set by the Mac app: how to start again after an update
API = "https://api.github.com"
KEEP_BACKUPS = 1  # only the version before the last update is kept, for "Undo last update"

state = {"checked_at": None, "available": False, "latest": None, "message": "", "error": None, "installing": False,
         "download_url": None, "apk_version": 0, "apk_url": None}


RELEASES_REPO = "Chronik36/BotTrack-releases"
CODE_ASSET = "bottrack-code.zip"
APK_ASSET = re.compile(r"^BotTrack-android-(\d+)\.apk$")  # the Android app, numbered by its versionCode


def settings():
    env = os.environ.get
    return {"source": env("UPDATE_SOURCE", "releases"), "releases": env("UPDATE_RELEASES_REPO", RELEASES_REPO),
            "repo": env("UPDATE_REPO", "Chronik36/Jigdatastream"), "branch": env("UPDATE_BRANCH", "main"),
            "token": env("GITHUB_TOKEN", "").strip()}


def build_number(version):
    """'build-42-abc1234' -> 42; anything else (an older commit-id version) -> 0."""
    parts = (version or "").split("-")
    return int(parts[1]) if len(parts) > 1 and parts[0] == "build" and parts[1].isdigit() else 0


def _latest_release(s):
    try:
        rel = json.loads(_get(f"{API}/repos/{s['releases']}/releases/latest", None))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("No BotTrack release has been published yet.") from e
        raise RuntimeError(f"GitHub answered {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach GitHub: {e.reason}") from e
    for a in rel.get("assets", []):  # the phone pages offer this to Android phones running an older app
        m = APK_ASSET.match(a["name"])
        if m:
            state.update(apk_version=int(m.group(1)), apk_url=a["browser_download_url"])
    asset = next((a for a in rel.get("assets", []) if a["name"] == CODE_ASSET), None)
    if not asset:
        raise RuntimeError("The latest release has no app code attached.")
    return rel["tag_name"], asset["browser_download_url"], (rel.get("body") or rel.get("name") or "").strip().splitlines()[0:1]


def check_release(app_dir=APP_DIR, latest_release=None):
    s = settings()
    current = installed_version(app_dir)
    state.update(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), error=None)
    try:
        tag, url, title = (latest_release or _latest_release)(s)
        newer = tag != current and build_number(tag) > build_number(current)
        state.update(available=newer, latest=tag, download_url=url,
                     message=(title[0][:120] if title else tag) if newer else "")
        logger.info("update check: installed %s, latest release %s, update %s",
                    current[:20] or "unknown", tag, "available" if newer else "not needed")
    except Exception as e:
        state.update(error=str(e))
        logger.warning("update check failed: %s", e)
    return state


def installed_version(app_dir=APP_DIR):
    for d in (app_dir, RUNNING_DIR):
        try:
            return (d / "VERSION").read_text().strip()
        except OSError:
            continue
    return ""


def can_self_update(app_dir=APP_DIR):
    """Only an installed copy updates itself; a developer's git checkout is left alone."""
    return not (app_dir / ".git").exists()


def _get(url, token, accept="application/vnd.github+json", timeout=30):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "BotTrack-updater",
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
    """Look for a newer version. Updates `state` and returns it."""
    s = settings()
    if s["source"] != "branch" and api is None:
        return check_release(app_dir)
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
    if s["source"] != "branch":
        url = state.get("download_url") or check_release().get("download_url")
        if not url:
            raise RuntimeError(state.get("error") or "No update found.")
        return _get(url, None, accept="application/octet-stream", timeout=300)
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
        raise RuntimeError("The download doesn't look like BotTrack; nothing was changed.")


def backups(app_dir=APP_DIR):
    d = app_dir.parent / "backups"
    return sorted((p for p in d.glob("app-*") if p.is_dir() and not p.name.endswith("-undone")),
                  reverse=True) if d.exists() else []


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
    if pip and _needs_packages(new / "requirements.txt"):
        target = os.environ.get("BOTTRACK_PIP_TARGET")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            *(["--upgrade", "--target", target] if target else []),
                            "-r", str(new / "requirements.txt")], capture_output=True, text=True)
        if r.returncode:
            shutil.rmtree(new, ignore_errors=True)
            logger.error("pip failed during update: %s", r.stderr[-2000:])
            raise RuntimeError("Installing the new version's Python packages failed; nothing was changed. See Logs.")
    old_version = installed_version(app_dir)
    bdir = app_dir.parent / "backups"
    bdir.mkdir(exist_ok=True)
    backup = bdir / f"app-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{(installed_version(app_dir) or 'unknown')[:7]}"
    if app_dir.exists():
        app_dir.rename(backup)
    new.rename(app_dir)
    try:  # after the restart, Setup shows what changed since this version
        whats_new_file(app_dir).write_text(json.dumps({"from": old_version}))
    except OSError:
        logger.warning("couldn't save the note for the patch notes")
    for old in backups(app_dir)[KEEP_BACKUPS:]:
        shutil.rmtree(old, ignore_errors=True)
    state.update(available=False, message="")
    logger.info("update %s installed; previous version saved in %s", sha[:7], backup.name)
    return backup


def whats_new_file(app_dir=APP_DIR):
    return app_dir.parent / "whats-new.json"


def whats_new_pending(app_dir=APP_DIR):
    """The version BotTrack was updated from, while its patch notes haven't been seen yet; else None."""
    try:
        old = json.loads(whats_new_file(app_dir).read_text()).get("from", "")
    except (OSError, ValueError):
        return None
    return old if build_number(installed_version(app_dir)) > build_number(old) else None


_notes_cache = {}


def release_notes(since, upto, fetch=None):
    """Patch notes of every release after build `since` up to build `upto`, newest first:
    [{"build": 66, "date": "2026-10-23", "notes": ["Stop sync button ...", ...]}]."""
    key = (since, upto)
    if key not in _notes_cache:
        s = settings()
        rels = (fetch or (lambda: json.loads(_get(f"{API}/repos/{s['releases']}/releases?per_page=50", None))))()
        out = []
        for r in rels:
            n = build_number(r.get("tag_name"))
            if since < n <= upto:
                lines = [ln.strip().lstrip("-* ").strip() for ln in (r.get("body") or "").splitlines()]
                out.append({"build": n, "date": (r.get("published_at") or "")[:10], "notes": [ln for ln in lines if ln]})
        _notes_cache[key] = sorted(out, key=lambda r: -r["build"])
    return _notes_cache[key]


def seen_whats_new(app_dir=APP_DIR):
    whats_new_file(app_dir).unlink(missing_ok=True)


def _needs_packages(requirements):
    """Only run pip when the new version's package list differs from the running one."""
    try:
        return requirements.read_text() != (RUNNING_DIR / "requirements.txt").read_text()
    except OSError:
        return requirements.exists()


def rollback(app_dir=APP_DIR):
    """Put the most recent backup back. The current code becomes a backup itself.
    In the Mac app, undoing the first update simply goes back to the code inside the app."""
    seen_whats_new(app_dir)
    found = backups(app_dir)
    bundled = bool(os.environ.get("BOTTRACK_CODE_DIR")) and app_dir.exists()
    if not found and not bundled:
        raise RuntimeError("There's no earlier version to go back to.")
    keep = app_dir.parent / "backups" / f"app-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{(installed_version(app_dir) or 'unknown')[:7]}-undone"
    keep.parent.mkdir(exist_ok=True)
    app_dir.rename(keep)
    if not found:
        logger.info("rolled back to the version inside the app")
        return keep
    target = found[0]
    target.rename(app_dir)
    logger.info("rolled back to %s", target.name)
    return target


def restart_soon(delay=1.5):
    """Exit after the response is sent, then start again with the new code (the Mac app relaunches
    itself; the older installer's launchd job restarts it)."""
    def go():
        time.sleep(delay)
        logger.info("restarting to load the new version")
        if relaunch:
            relaunch()
        logging.shutdown()
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()


def check_on_a_timer(hours):
    time.sleep(120)
    while True:
        check()
        time.sleep(hours * 3600)
