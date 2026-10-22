"""BotTrack's window: the dashboard in a native macOS WebKit window (pywebview), not a browser tab."""
import logging
import os
import time
import urllib.request

logger = logging.getLogger("jigstream.window")


def wait_for(url, seconds=30):
    for _ in range(seconds * 2):
        try:
            with urllib.request.urlopen(url + "/setup", timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def open_window(url, icon=None):
    """Show the dashboard and block until the window is closed."""
    import webview  # pywebview: free, uses the WebKit that ships with macOS

    if not wait_for(url):
        logger.error("the dashboard at %s isn't answering", url)
    if icon and os.path.exists(icon):
        try:
            from AppKit import NSApplication, NSImage
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(icon)))
        except Exception:  # cosmetic only
            logger.debug("couldn't set the Dock icon", exc_info=True)
    webview.settings["ALLOW_DOWNLOADS"] = True  # logs zip, profile exports
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # Gmail / Discord setup links
    start = "/orders" if os.environ.get("GMAIL_ACCOUNTS") else "/setup"
    webview.create_window("BotTrack", url + start, width=1360, height=880, min_size=(900, 600),
                          background_color="#132048", text_select=True)
    logger.info("window opened on %s", url)
    webview.start()
