"""How much room BotTrack takes on this computer, and freeing what it doesn't need.

The AI model is most of it (several GB; a smaller model takes less). Everything else is measured too, so the
Storage page shows where the space goes. Free up space removes only what BotTrack can do without: the database's
empty pages, change records no linked computer is waiting for, older backups beyond the newest few, the copy of
old logs and update leftovers. The copy kept for "Undo last update" stays.
"""
import logging
import os
import sqlite3
import sys
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, url_for

logger = logging.getLogger("jigstream.storage")
bp = Blueprint("storage", __name__)

KEEP_BACKUPS = 3  # newest backup files kept when freeing space


def size(path):
    path = Path(path)
    try:
        if path.is_file():
            return path.stat().st_size
        total = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def app_bundle():
    """The installed app itself (BotTrack.app, or the Windows install folder), when running from one."""
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    if sys.platform == "win32" and (exe.parent.parent / "app").exists():
        return exe.parent.parent
    return None


def report(data, db_path):
    """[(what, bytes, note)] biggest first, plus the total."""
    from . import backup
    parts = []
    models = Path(data) / "models"
    parts.append(("AI model", size(models), "The model that reads your emails. Most of the room BotTrack takes."))
    parts.append(("Your data", size(Path(db_path).parent),
                  "Orders, expenses, profiles, training and settings."))
    parts.append(("Backups", size(backup.folder()), f"Locked backups in {backup.folder()}."))
    parts.append(("Logs", size(Path(data) / "logs"), "Kept 30 days, for troubleshooting."))
    parts.append(("Undo last update", size(Path(data) / "backups") + size(Path(data) / "app.new"),
                  "A copy of the version before the last update."))
    parts.append(("App updates", size(Path(data) / "app"), "Code from in-app updates."))
    bundle = app_bundle()
    if bundle:
        parts.append(("The app itself", size(bundle), "BotTrack with its own Python and AI engine."))
    parts = [p for p in parts if p[1]]
    parts.sort(key=lambda p: -p[1])
    return parts, sum(p[1] for p in parts)


def tidy(data, db_path):
    """Free what BotTrack can do without. Returns bytes freed."""
    from . import backup, macapp, peersync
    before = sum(p[1] for p in report(data, db_path)[0])
    con = sqlite3.connect(db_path, timeout=60)
    try:
        try:
            linked = con.execute("SELECT COUNT(*) FROM sync_peers WHERE site IS NOT NULL").fetchone()[0]
        except sqlite3.Error:
            linked = 0
        if not linked:  # nothing to send to: drop the record of changes, and stop keeping one
            try:
                con.execute("DELETE FROM sync_log")
                if not peersync.link_wanted(db_path):
                    peersync.uninstall(con)
                con.commit()
            except sqlite3.Error:
                pass
        con.execute("VACUUM")
    finally:
        con.close()
    for old in [Path(f["name"]) for f in backup.listing()][KEEP_BACKUPS:]:
        (backup.folder() / old).unlink(missing_ok=True)
    macapp.cleanup(Path(data))
    after = sum(p[1] for p in report(data, db_path)[0])
    logger.info("freed %d MB", (before - after) // 1048576)
    return max(0, before - after)


def _data():
    from . import macapp
    return macapp.DATA


def _db():
    from . import db
    return current_app.config.get("DB_PATH") or db.DB_PATH


@bp.get("/storage")
def page():
    from .mobile import from_this_mac
    if not from_this_mac():
        return redirect("/")
    parts, total = report(_data(), _db())
    return render_template("storage.html", parts=parts, total=total, model=os.environ.get("LLM_MODEL", ""))


@bp.post("/storage/tidy")
def tidy_page():
    from .mobile import from_this_mac
    if not from_this_mac():
        return redirect("/")
    try:
        freed = tidy(_data(), _db())
        flash(f"Freed {freed / 1048576:,.0f} MB." if freed >= 1048576 else "Nothing more to free: everything left is in use.")
    except Exception as e:
        logger.exception("freeing space failed")
        flash(f"Couldn't free space: {e}")
    return redirect(url_for("storage.page"))
