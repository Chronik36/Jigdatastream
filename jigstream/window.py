"""Order Tracker's own Mac window: the dashboard in a native WebKit window instead of a browser tab.

The background service (run.py, started by launchd) does the syncing; this is only the window,
so closing it leaves syncing running. `python -m jigstream.window` is what Order Tracker.app runs.
"""
import fcntl
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path

from .config import load_dotenv

HOME_DIR = Path(os.environ.get("ORDER_TRACKER_HOME", Path.home() / "Library/Application Support/OrderTracker"))
logger = logging.getLogger("jigstream.window")


def wait_for(url, seconds=30):
    for _ in range(seconds * 2):
        try:
            with urllib.request.urlopen(url + "/setup", timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def name_the_app(icon):
    """Show 'Order Tracker' and its icon in the Dock and menu bar rather than 'Python'."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "Order Tracker"
        if icon.exists():
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(icon)))
    except Exception:  # cosmetic only
        logger.debug("couldn't set the Dock name/icon", exc_info=True)


def main():
    if HOME_DIR.exists():
        os.chdir(HOME_DIR)
    load_dotenv()
    logging.basicConfig(filename=str(HOME_DIR / "logs" / "window.log") if (HOME_DIR / "logs").exists() else None,
                        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://127.0.0.1:{os.environ.get('PORT', 8765)}"

    # One window at a time: a second launch just exits (the Dock icon brings the first one forward).
    lock = open(HOME_DIR / ".window.lock" if HOME_DIR.exists() else "/tmp/ordertracker-window.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0

    import webview  # pywebview: free, uses the WebKit that ships with macOS

    if not wait_for(url):
        logger.error("the background service at %s isn't answering", url)
    name_the_app(HOME_DIR / "app" / "jigstream" / "static" / "icon.png")
    webview.settings["ALLOW_DOWNLOADS"] = True  # logs zip, profile exports
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # Gmail / Discord setup links
    start = "/orders" if os.environ.get("GMAIL_ACCOUNTS") else "/setup"
    webview.create_window("Order Tracker", url + start, width=1360, height=880, min_size=(900, 600),
                          background_color="#09090B", text_select=True)
    logger.info("window opened on %s", url)
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
