"""BotTrack's window: the dashboard in a native macOS WebKit window (pywebview), not a browser tab."""
import base64
import logging
import os
import plistlib
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("jigstream.window")


def wait_for(url, seconds=30):
    for _ in range(seconds * 2):
        try:
            with urllib.request.urlopen(url + "/setup", timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def splash(title, msg):
    """The update loading screen as a page of its own, shown until the dashboard answers."""
    from jinja2 import Environment, FileSystemLoader

    here = Path(__file__).resolve().parent

    def image(name):
        return "data:image/png;base64," + base64.b64encode((here / "static/loader" / name).read_bytes()).decode()
    page = Environment(loader=FileSystemLoader(here / "templates"), autoescape=True).get_template("loader.html")
    body = page.render(logo=image("logo.png"), lines=image("lines.png"), title=title, msg=msg, shown=True)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><style>html,body{margin:0;background:#0a1230}</style>'
            f"</head><body>{body}</body></html>")


def safari_agent():
    """The user agent Safari on this Mac sends, so carrier pages see the browser they expect."""
    try:
        with open("/Applications/Safari.app/Contents/Info.plist", "rb") as f:
            version = plistlib.load(f)["CFBundleShortVersionString"]
    except (OSError, KeyError, plistlib.InvalidFileException):
        return None
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            f"Version/{version} Safari/605.1.15")


def page_reader(webview):
    """read(url, done, timeout) for browsertrack: loads a page in one hidden window, kept for the next
    page, and returns its text once done(text) says the page has what's needed (or time runs out)."""
    lock = threading.Lock()
    hidden = {}

    def text_of(window):
        try:
            return window.evaluate_js("document.body ? document.body.innerText : ''") or ""
        except Exception:  # the page is still between documents
            return ""

    def read(page, done, timeout=45):
        with lock:
            window = hidden.get("window")
            if window is None:
                window = hidden["window"] = webview.create_window("BotTrack tracking", page, hidden=True,
                                                                  width=1280, height=900)
            else:
                try:
                    window.evaluate_js("document.body && (document.body.innerText = '')")  # no stale text
                except Exception:
                    pass
                window.load_url(page)
            end, text = time.time() + timeout, ""
            time.sleep(2)
            while time.time() < end:
                text = text_of(window)
                if text and done(text):
                    break
                time.sleep(1.5)
            return text

    def release():
        with lock:
            if hidden.get("window") is not None:
                hidden["window"].load_url("about:blank")
    read.release = release
    return read


def open_window(url, icon=None, restarted=False):
    """Show the dashboard and block until the window is closed. The window opens at once on the
    loading screen and switches to the dashboard when it answers; after an update or a model switch
    it lands on Setup."""
    import webview  # pywebview: free, uses the WebKit that ships with macOS

    if icon and os.path.exists(icon):
        try:
            from AppKit import NSApplication, NSImage
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(icon)))
        except Exception:  # cosmetic only
            logger.debug("couldn't set the Dock icon", exc_info=True)
    webview.settings["ALLOW_DOWNLOADS"] = True  # logs zip, profile exports
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # Gmail / Discord setup links
    start = "/setup#updates" if restarted else "/orders" if os.environ.get("GMAIL_ACCOUNTS") else "/setup"
    try:
        html = splash("Almost done", "Opening BotTrack with the changes.") if restarted else \
            splash("Starting BotTrack", "Getting things ready.")
    except Exception:  # the loading screen is cosmetic; never let it stop the app opening
        logger.exception("couldn't build the loading screen")
        html = None
    if html is None:
        wait_for(url)
    window = webview.create_window("BotTrack", None if html else url + start, html=html or "", width=1360, height=880,
                                   min_size=(900, 600), background_color="#0a1230", text_select=True)

    def show_dashboard():
        shown_at = time.time()
        if not wait_for(url):
            logger.error("the dashboard at %s isn't answering", url)
        if restarted:
            time.sleep(max(0, 1.2 - (time.time() - shown_at)))  # let the loading screen be seen, not flash by
        if html:
            window.load_url(url + start)
        logger.info("window opened on %s", url)

    from . import browsertrack
    browsertrack.register(page_reader(webview))  # key-free package tracking
    from . import vcclock
    vcclock.register(webview)  # bank window for card locks
    from . import proxylogin
    proxylogin.register(webview)  # proxy dashboards you sign in to
    # Private: nothing a site sets (the bank sign-in included) is kept after BotTrack closes.
    webview.start(show_dashboard, user_agent=safari_agent(), private_mode=True)
