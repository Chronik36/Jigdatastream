import logging
import os
import threading
import time

from jigstream.app import create_app
from jigstream.config import load_dotenv
from jigstream.logs import setup_logging
from jigstream import updater


def sync_on_a_timer(app, hours):
    """Check every inbox shortly after start, then every few hours."""
    time.sleep(60)
    while True:
        try:
            app.start_sync()
        except Exception:
            logging.getLogger("jigstream").exception("scheduled sync failed to start")
        time.sleep(hours * 3600)


if __name__ == "__main__":
    load_dotenv()
    setup_logging()
    logging.getLogger("jigstream").info("Order Tracker starting (port %s)", os.environ.get("PORT", 5000))
    app = create_app()
    hours = float(os.environ.get("SYNC_EVERY_HOURS", 4))
    if hours > 0:
        threading.Thread(target=sync_on_a_timer, args=(app, hours), daemon=True).start()
    update_hours = float(os.environ.get("UPDATE_CHECK_HOURS", 6))
    if update_hours > 0 and updater.can_self_update():
        threading.Thread(target=updater.check_on_a_timer, args=(update_hours,), daemon=True).start()
    # macOS uses port 5000 for AirPlay, so the installer sets PORT=8765.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
