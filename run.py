import os
import threading
import time

from jigstream import db, emails, updater
from jigstream.app import create_app


def auto_sync(minutes):
    """Poll Gmail in the background so cancellations show up without pressing Sync."""
    while True:
        try:
            emails.sync_gmail(db.connect())
        except Exception as e:  # keep the loop alive; the Sync button shows errors too
            print("auto-sync failed:", e)
        time.sleep(minutes * 60)


def auto_update(minutes):
    """Opt-in: fast-forward to new commits on the tracked branch and relaunch."""
    while True:
        time.sleep(minutes * 60)
        try:
            if updater.apply().get("updated"):
                updater.restart()
        except Exception as e:
            print("auto-update failed:", e)


if __name__ == "__main__":
    mins = int(os.environ.get("JIGSTREAM_AUTO_SYNC_MIN", "0"))
    if mins and os.environ.get("GMAIL_ADDRESS"):
        threading.Thread(target=auto_sync, args=(mins,), daemon=True).start()
    upd = int(os.environ.get("JIGSTREAM_AUTO_UPDATE_MIN", "0"))
    if upd:
        threading.Thread(target=auto_update, args=(upd,), daemon=True).start()
    create_app().run(host="127.0.0.1", port=5000, debug=False)
