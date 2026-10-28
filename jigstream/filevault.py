"""FileVault: BotTrack keeps inbox passwords, cards and orders on this Mac, so it asks for the Mac's disk to be encrypted.

An app can't turn FileVault on (macOS needs your password and a restart), so BotTrack checks it with `fdesetup status`
(no admin needed) and, while it's off, opens on a screen with the steps and a button to System Settings. That screen
re-checks by itself and lets you in the moment FileVault is on or turning on. Nothing here touches your data.

FileVault is required (jacob, 2026-10-28): BotTrack doesn't open past that screen until it's on, phones included.
If the Mac can't say either way, BotTrack opens as usual rather than locking anyone out. FILEVAULT_MODE in .env is
for testing only: "block" (the default), "remind" ("Remind me tomorrow" lets you in for a day), "banner", or "off".
"""
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

logger = logging.getLogger("jigstream.filevault")
bp = Blueprint("filevault", __name__)

MODES = ("remind", "block", "banner", "off")
SNOOZE = timedelta(days=1)
CHECK_EVERY = 60  # seconds a status answer is reused
SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?FileVault"
OPEN_PATHS = ("/filevault", "/static/", "/update", "/logs", "/m/manifest", "/api/changes", "/m/api/changes",
              "/activate", "/setup/inbox")

_cache = {"at": 0.0, "on": None}


def mode():
    m = os.environ.get("FILEVAULT_MODE", "block").strip().lower()
    return m if m in MODES else "block"


def _ask():
    """True when FileVault is on or turning on, False when off, None when it can't be told."""
    try:
        out = subprocess.run(["fdesetup", "status"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "FileVault is On" in out or "Encryption in progress" in out:
        return True
    if "FileVault is Off" in out:
        return False
    return None


def is_on(fresh=False):
    """On a Mac: whether FileVault is on (None if unknown). Elsewhere: None, and nothing is asked of you."""
    if sys.platform != "darwin":
        return None
    if fresh or time.time() - _cache["at"] > CHECK_EVERY:
        _cache.update(at=time.time(), on=_ask())
    return _cache["on"]


def snoozed():
    try:
        return datetime.fromisoformat(os.environ.get("FILEVAULT_SNOOZE_UNTIL", "")) > datetime.now()
    except ValueError:
        return False


def needs_screen():
    m = mode()
    if m in ("off", "banner") or is_on() is not False:
        return False
    return m == "block" or not snoozed()


def init_app(app):
    app.register_blueprint(bp)

    @app.before_request
    def need_filevault():
        if request.path.startswith(OPEN_PATHS) or not needs_screen():
            return None
        from .mobile import from_this_mac
        if not from_this_mac() and mode() != "block":
            return None  # phones aren't held up by the reminder, only by a hard block
        return redirect(url_for("filevault.page", next=request.full_path if request.method == "GET" else "/"))

    @app.context_processor
    def filevault_banner():
        return {"filevault_banner": mode() != "off" and is_on() is False
                and not request.path.startswith("/filevault")}


@bp.get("/filevault")
def page():
    nxt = request.args.get("next", "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    return render_template("filevault.html", on=is_on(fresh=True), mode=mode(), settings_url=SETTINGS_URL,
                           next=nxt.rstrip("?") or "/")


@bp.get("/filevault/status")
def status():
    return jsonify(on=is_on(fresh=True)), 200, {"Cache-Control": "no-store"}


@bp.post("/filevault/later")
def later():
    from .config import save_env
    from .mobile import from_this_mac
    if mode() == "block" or not from_this_mac():
        return redirect(url_for("filevault.page"))
    save_env({"FILEVAULT_SNOOZE_UNTIL": (datetime.now() + SNOOZE).isoformat(timespec="minutes")})
    logger.info("FileVault reminder snoozed for a day")
    nxt = request.form.get("next", "/")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")


@bp.post("/filevault/open")
def open_settings():
    """Opens System Settings at FileVault (the page's link does the same; this works inside the Mac window too)."""
    from .mobile import from_this_mac
    if from_this_mac() and sys.platform == "darwin":
        subprocess.Popen(["open", SETTINGS_URL])
    return ("", 204)
