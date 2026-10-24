"""Backups of everything BotTrack keeps, saved as one locked file on this computer, and restoring from one.

A backup holds the database (orders, expenses, profiles, cards, training…), the settings (.env: Gmail app
passwords, keys), the card key and the Products list. It's locked with a backup password you choose, so the file
alone is no use to anyone else; without the password it can't be opened, by you either.

- Automatic: once a password is set, BotTrack saves a backup every day to Documents/BotTrack Backups (outside its own
  data folder, so removing BotTrack doesn't remove them) and keeps the last KEEP.
- By hand: Back up now on Setup saves one there and downloads a copy.
- Restore: pick a backup file and type its password. Everything is put back as it was in the backup, then
  BotTrack restarts. The current data is backed up first.
"""
import base64
import io
import json
import logging
import os
import platform
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for

from . import db

logger = logging.getLogger("jigstream.backup")
bp = Blueprint("backup", __name__)

MAGIC = b"BTBK1"
KEEP = 7
EVERY = 24 * 3600
MIN_PASSWORD = 8
# Settings that belong to this computer, not to the backup: where it listens, its link and its update state.
KEEP_HERE = {"PORT", "PHONE_SECRET_KEY", "SYNC_TOPIC", "SYNC_LINK", "BOTTRACK_KEY_GRACE"}
state = {"last": "", "error": ""}


def folder():
    return Path(os.environ.get("BACKUP_DIR") or Path.home() / "Documents" / "BotTrack Backups")


def _box(password, salt):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    key = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def lock(data, password):
    salt = os.urandom(16)
    return MAGIC + salt + _box(password, salt).encrypt(data)


def unlock(blob, password):
    from cryptography.fernet import InvalidToken
    if not blob.startswith(MAGIC):
        raise ValueError("That isn't a BotTrack backup file.")
    salt = blob[len(MAGIC):len(MAGIC) + 16]
    try:
        return _box(password, salt).decrypt(blob[len(MAGIC) + 16:])
    except InvalidToken:
        raise ValueError("That password doesn't open this backup.")


def _db_path():
    return current_app.config.get("DB_PATH") or db.DB_PATH


def make(db_path=None, env_file=".env"):
    """The backup's contents, unlocked: a zip of the database, settings, card key and Products list."""
    from . import products, wallet
    db_path = db_path or db.DB_PATH
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "jigstream.db")
        src, dst = sqlite3.connect(db_path), sqlite3.connect(copy)
        try:
            src.backup(dst)  # a consistent copy while BotTrack keeps working
        finally:
            dst.close()
            src.close()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copy, "jigstream.db")
            if os.path.exists(env_file):
                z.write(env_file, "env")
            try:
                z.writestr("card-key", wallet.key())
            except Exception as e:
                logger.warning("backup without the card key: %s", e)
            if os.path.exists(products.data_path()):
                z.write(products.data_path(), "products.json")
            from .updater import installed_version
            z.writestr("about.json", json.dumps({"made": datetime.now().isoformat(timespec="seconds"),
                                                 "computer": platform.node(), "version": installed_version()}))
    return buf.getvalue()


def save(password, db_path=None, where=None):
    """Write a locked backup into the backups folder, drop the oldest beyond KEEP. Returns its path."""
    where = Path(where or folder())
    where.mkdir(parents=True, exist_ok=True)
    path = where / f"BotTrack backup {datetime.now():%Y-%m-%d %H%M}.btbackup"
    tmp = path.with_suffix(".part")
    tmp.write_bytes(lock(make(db_path), password))
    tmp.replace(path)
    for old in sorted(where.glob("BotTrack backup *.btbackup"))[:-KEEP]:
        old.unlink(missing_ok=True)
    state.update(last=f"{datetime.now():%b %d %H:%M}: saved {path.name}", error="")
    logger.info("backup saved: %s (%d KB)", path, path.stat().st_size // 1024)
    return path


def listing(where=None):
    where = Path(where or folder())
    files = sorted(where.glob("BotTrack backup *.btbackup"), reverse=True) if where.exists() else []
    return [{"name": f.name, "size": f.stat().st_size, "at": datetime.fromtimestamp(f.stat().st_mtime)} for f in files]


def restore(blob, password, db_path=None, env_file=".env"):
    """Put a backup back: the database (copied into the open one), the settings, the card key and Products."""
    from . import products, wallet
    from .config import save_env
    data = unlock(blob, password)
    with zipfile.ZipFile(io.BytesIO(data)) as z, tempfile.TemporaryDirectory() as tmp:
        names = set(z.namelist())
        if "jigstream.db" not in names:
            raise ValueError("That backup has no database in it.")
        z.extract("jigstream.db", tmp)
        src = sqlite3.connect(os.path.join(tmp, "jigstream.db"))
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("The database in that backup is damaged.")
            dst = sqlite3.connect(db_path or db.DB_PATH, timeout=60)
            try:
                src.backup(dst)  # replaces every page of the open database in one go
            finally:
                dst.close()
        finally:
            src.close()
        if "card-key" in names:
            wallet.set_key(z.read("card-key"))
        if "products.json" in names:
            Path(products.data_path()).write_bytes(z.read("products.json"))
        if "env" in names:
            restored = {}
            for line in z.read("env").decode().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    if k.strip() not in KEEP_HERE:
                        restored[k.strip()] = v
            save_env(restored, env_file)
        about = json.loads(z.read("about.json")) if "about.json" in names else {}
    logger.info("restored a backup made %s on %s", about.get("made"), about.get("computer"))
    return about


def start(app):
    """A backup every day while a backup password is set."""
    path = app.config.get("DB_PATH")

    def loop():
        time.sleep(120)
        while True:
            pw = os.environ.get("BACKUP_PASSWORD", "")
            if pw:
                files = listing()
                newest = files[0]["at"].timestamp() if files else 0
                if time.time() - newest >= EVERY:
                    try:
                        save(pw, path)
                    except Exception as e:
                        logger.exception("automatic backup failed")
                        state.update(error=str(e))
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True, name="backup").start()


# ---- pages ----

def _local():
    from .mobile import from_this_mac
    return from_this_mac()


@bp.get("/backups")
def page():
    if not _local():
        return redirect("/")
    return render_template("backups.html", files=listing(), where=str(folder()), on=bool(os.environ.get("BACKUP_PASSWORD")),
                           state=state)


@bp.post("/backups/password")
def set_password():
    from .config import save_env
    if not _local():
        return redirect("/")
    pw, again = request.form.get("password", ""), request.form.get("again", "")
    if len(pw) < MIN_PASSWORD or pw != again:
        flash(f"Type the same password twice, at least {MIN_PASSWORD} characters.")
        return redirect(url_for("backup.page"))
    save_env({"BACKUP_PASSWORD": pw})
    try:
        save(pw, _db_path())
        flash("Backups are on. The first one is saved; a new one is made every day. Keep the password somewhere safe: "
              "a backup can't be opened without it.")
    except Exception as e:
        logger.exception("backup failed")
        flash(f"The password is saved, but the first backup failed: {e}")
    return redirect(url_for("backup.page"))


@bp.post("/backups/now")
def now():
    if not _local():
        return redirect("/")
    pw = os.environ.get("BACKUP_PASSWORD", "")
    if not pw:
        flash("Set a backup password first.")
        return redirect(url_for("backup.page"))
    try:
        path = save(pw, _db_path())
    except Exception as e:
        logger.exception("backup failed")
        flash(f"The backup failed: {e}")
        return redirect(url_for("backup.page"))
    return send_file(path, as_attachment=True, download_name=path.name, mimetype="application/octet-stream")


@bp.get("/backups/file/<name>")
def download(name):
    if not _local():
        return redirect("/")
    path = folder() / Path(name).name
    if not path.name.endswith(".btbackup") or not path.exists():
        flash("That backup is gone.")
        return redirect(url_for("backup.page"))
    return send_file(path, as_attachment=True, download_name=path.name, mimetype="application/octet-stream")


@bp.post("/backups/restore")
def restore_page():
    from . import updater
    if not _local():
        return redirect("/")
    f = request.files.get("file")
    pw = request.form.get("password", "")
    if not f or not pw:
        flash("Pick a backup file and type its password.")
        return redirect(url_for("backup.page"))
    blob = f.read()
    try:
        unlock(blob, pw)  # check before touching anything
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("backup.page"))
    current = os.environ.get("BACKUP_PASSWORD") or pw
    try:
        save(current, _db_path())  # what's here now, in case the restore was a mistake
    except Exception:
        logger.exception("couldn't back up before restoring")
    try:
        about = restore(blob, pw, _db_path())
    except Exception as e:
        logger.exception("restore failed")
        flash(f"Couldn't restore: {e}")
        return redirect(url_for("backup.page"))
    if current_app.config.get("PHONE_LINK"):  # the Mac or PC app: start again with the restored settings
        updater.restart_soon()
        return render_template("restarting.html", what=f"Restored the backup from {about.get('made', '')[:16].replace('T', ' ')}")
    flash("Restored. Restart BotTrack to load the restored settings.")
    return redirect(url_for("backup.page"))
