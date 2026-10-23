"""Live package status with no keys: BotTrack opens the carrier's normal tracking page in a hidden
Safari (WebKit) window on this Mac and reads the status line, the same page you'd look at yourself.

Only the Mac app has that window (window.py registers the page reader when it opens), so this does
nothing in a plain browser tab. Pages are opened slowly, one at a time, from your own connection. If a
carrier shows a robot check, BotTrack stops asking that carrier until the next round and the order
keeps the status from its emails.
"""
import logging
import re

logger = logging.getLogger("jigstream.browsertrack")

PAGES = {"UPS": "https://www.ups.com/track?tracknum={}&loc=en_US",
         "USPS": "https://tools.usps.com/go/TrackConfirmAction?tLabels={}",
         "FedEx": "https://www.fedex.com/fedextrack/?trknbr={}"}

# The carriers' own status headlines, at the start of a short line of the page.
HEADLINE = re.compile(
    r"^(out for delivery|delivered|delivery attempted|attempted delivery|returned to sender|returning to sender|"
    r"return to sender|being returned|label created|shipping label created|shipment information sent|"
    r"pre-shipment|order processed|on the way|in transit|moving through network|we have your package|"
    r"picked up|arrived at|departed|accepted at|delivery exception|exception|delayed?|alert|action required|"
    r"held at|available for pickup|notice left)\b", re.I)
NOT_FOUND = re.compile(r"could not locate|couldn't find|could not find|unable to locate|not found|"
                       r"status not available|no record|invalid tracking|isn't valid|is not valid", re.I)
BLOCKED = re.compile(r"access denied|captcha|verify (that )?you are (a )?human|unusual (activity|traffic)|"
                     r"are you a robot|request unsuccessful|pardon our interruption", re.I)

_reader = None  # read(url, done, timeout) -> the page's text; set by window.py


class Blocked(RuntimeError):
    """The carrier showed a robot check or refused the page."""


class NoInfo(RuntimeError):
    """The page loaded but had no status for this number (yet)."""


def register(reader):
    global _reader
    _reader = reader


def ready(env):
    """True in the Mac app unless key-free checks were turned off on Setup."""
    return _reader is not None and env.get("BROWSER_TRACKING", "1") != "0"


def headline(text):
    """The status line from a tracking page's text, or None. Three or more status words close together
    are the page's progress bar (Label created, On the way, ... Delivered). Pages put the headline just
    above the bar, so the run's first line counts only when the bar repeats it or it has details
    (a comma, a colon or a long line); otherwise the bar is skipped for a headline elsewhere."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    hits = [(i, ln) for i, ln in enumerate(lines) if ln and len(ln) <= 90 and HEADLINE.match(ln)]
    if not hits:
        return None
    groups, current = [], [hits[0]]
    for hit in hits[1:]:
        if hit[0] - current[-1][0] <= 2:
            current.append(hit)
        else:
            groups.append(current)
            current = [hit]
    groups.append(current)
    for group in groups:
        first = group[0][1]
        if len(group) < 3 or re.search(r"[,:]", first) or len(first) > 30 \
                or first.lower() in (ln.lower() for _, ln in group[1:]):
            return first
    return None


def _finished(text):
    return bool(headline(text) or NOT_FOUND.search(text) or BLOCKED.search(text))


def read_status(carrier, number, timeout=45):
    """Open the carrier's page for this number and return its status line."""
    if _reader is None:
        raise NoInfo("key-free checks only run in the BotTrack app")
    text = _reader(PAGES[carrier].format(number), _finished, timeout) or ""
    line = headline(text)
    if line:
        return line
    if BLOCKED.search(text):
        raise Blocked(f"{carrier} showed a robot check")
    if NOT_FOUND.search(text):
        raise NoInfo(f"{carrier} has no details for this number yet")
    # Logged so a changed page can be fixed from the logs the Send logs button shares.
    logger.info("no status found on the %s page; it began: %r", carrier, " | ".join(text.split("\n")[:40])[:600])
    raise NoInfo(f"couldn't read the {carrier} page")
