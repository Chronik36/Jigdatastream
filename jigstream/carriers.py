"""Live package status from USPS, UPS and FedEx.

Every few hours BotTrack asks the carrier about each open order that has a tracking number and moves
its status forward (In transit, Out for delivery, Delivered, problems). With no keys, it reads the
carrier's normal tracking page in a hidden window on this Mac (browsertrack.py). A free developer key
pasted on the Setup page is optional; that carrier is then asked through its API instead, which is
faster. Nothing costs money. Keys and tokens are never written to the logs.
"""
import base64
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from . import browsertrack, notify, ordersheet
from .extract import carrier_for, status_advances
from .orderstore import Store

logger = logging.getLogger("jigstream.carriers")

KEYS = {"USPS": ("USPS_CLIENT_ID", "USPS_CLIENT_SECRET"),
        "UPS": ("UPS_CLIENT_ID", "UPS_CLIENT_SECRET"),
        "FedEx": ("FEDEX_API_KEY", "FEDEX_SECRET_KEY")}
OPEN = ("delivered", "cancelled", "returned", "refunded")
_tokens = {}
state = {"running": False, "last": None}


class CarrierError(RuntimeError):
    pass


def configured(env):
    """Carriers whose key and secret are both filled in."""
    return [c for c, (k, s) in KEYS.items() if env.get(k) and env.get(s)]


def available(env):
    """Carriers BotTrack can check now: those with keys, plus all three when key-free checks are on."""
    keyed = configured(env)
    if browsertrack.ready(env):
        return keyed + [c for c in browsertrack.PAGES if c not in keyed]
    return keyed


def _call(req, timeout=20):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode(errors="replace")
        raise CarrierError(f"HTTP {e.code}: {body}") from None
    except (urllib.error.URLError, OSError) as e:
        raise CarrierError(f"couldn't reach the carrier ({e})") from None


def _token(carrier, env):
    cached = _tokens.get(carrier)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    key, secret = (env.get(k, "").strip() for k in KEYS[carrier])
    form = {"grant_type": "client_credentials"}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if carrier == "USPS":
        url = "https://apis.usps.com/oauth2/v3/token"
        form.update(client_id=key, client_secret=secret)
    elif carrier == "UPS":
        url = "https://onlinetools.ups.com/security/v1/oauth/token"
        headers["Authorization"] = "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()
    else:
        url = "https://apis.fedex.com/oauth/token"
        form.update(client_id=key, client_secret=secret)
    data = _call(urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), headers=headers))
    token = data.get("access_token")
    if not token:
        raise CarrierError("the carrier didn't accept the key")
    _tokens[carrier] = (token, time.time() + int(data.get("expires_in") or 3600))
    return token


def status_from_text(text):
    """A carrier's own wording -> one of BotTrack's statuses (None when it says nothing useful)."""
    t = (text or "").lower()
    if not t:
        return None
    if "out for delivery" in t:
        return "out_for_delivery"
    if re.search(r"return(ed|ing)? to (sender|shipper)|being returned", t):
        return "returned"
    if re.search(r"\bdeliver(ed|y attempted)?\b", t) and "attempt" not in t and "expected" not in t \
            and "scheduled" not in t and "estimated" not in t:
        return "delivered"
    if re.search(r"exception|alert|undeliverable|delay|attempt|held|action required|problem", t):
        return "exception"
    if re.search(r"label created|pre-shipment|shipment information|shipping label|manifest|"
                 r"awaiting|ready for ups|order processed|shipment ready", t):
        return "processing"
    return "in_transit"


def track_usps(number, env):
    data = _call(urllib.request.Request(
        f"https://apis.usps.com/tracking/v3/tracking/{urllib.parse.quote(number)}?expand=SUMMARY",
        headers={"Authorization": f"Bearer {_token('USPS', env)}", "Accept": "application/json"}))
    text = data.get("statusCategory") or data.get("status") or ""
    return status_from_text(text), data.get("statusSummary") or data.get("status") or text


def track_ups(number, env):
    data = _call(urllib.request.Request(
        f"https://onlinetools.ups.com/api/track/v1/details/{urllib.parse.quote(number)}?locale=en_US",
        headers={"Authorization": f"Bearer {_token('UPS', env)}", "transId": uuid.uuid4().hex[:32],
                 "transactionSrc": "BotTrack", "Accept": "application/json"}))
    try:
        package = data["trackResponse"]["shipment"][0]["package"][0]
    except (KeyError, IndexError, TypeError):
        raise CarrierError(f"UPS has no details for this number yet: {str(data)[:200]}") from None
    current = package.get("currentStatus") or {}
    text = current.get("simplifiedTextDescription") or current.get("description") or ""
    if not text and package.get("activity"):
        text = (package["activity"][0].get("status") or {}).get("description", "")
    return status_from_text(text), text


def track_fedex(number, env):
    body = {"includeDetailedScans": False, "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": number}}]}
    data = _call(urllib.request.Request(
        "https://apis.fedex.com/track/v1/trackingnumbers", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_token('FedEx', env)}", "Content-Type": "application/json",
                 "X-locale": "en_US"}))
    try:
        result = data["output"]["completeTrackResults"][0]["trackResults"][0]
    except (KeyError, IndexError, TypeError):
        raise CarrierError(f"FedEx has no details for this number yet: {str(data)[:200]}") from None
    if result.get("error"):
        raise CarrierError(result["error"].get("message", "FedEx couldn't find this number"))
    latest = result.get("latestStatusDetail") or {}
    code, text = latest.get("code", ""), latest.get("description") or latest.get("statusByLocale") or ""
    by_code = {"DL": "delivered", "OD": "out_for_delivery", "IT": "in_transit", "DP": "in_transit",
               "AR": "in_transit", "PU": "in_transit", "OC": "processing", "DE": "exception",
               "SE": "exception", "CA": "cancelled", "RS": "returned"}
    return by_code.get(code) or status_from_text(text), text


TRACKERS = {"USPS": track_usps, "UPS": track_ups, "FedEx": track_fedex}


def carrier_of(order):
    name = (order["carrier"] or "").lower()
    for c in TRACKERS:
        if c.lower() in name:
            return c
    found = carrier_for(order["tracking_number"])
    if found in TRACKERS:
        return found
    if re.fullmatch(r"\d{12}|\d{15}|\d{20}", order["tracking_number"] or ""):
        return "FedEx"
    return None


def refresh(con, env, only_id=None, statuses=None):
    """Ask the carriers about every open order with a tracking number (only those in `statuses`, if given).
    Returns (checked, changed)."""
    keyed, ready = set(configured(env)), set(available(env))
    if not ready:
        return 0, 0
    page_limit = int(env.get("BROWSER_TRACK_MAX") or 40)  # tracking pages opened per round, at most
    pages, blocked = 0, set()
    Store(con)  # makes sure the carrier columns exist on older databases
    q = ("SELECT * FROM tracked_orders WHERE tracking_number IS NOT NULL AND tracking_number != ''"
         " AND COALESCE(shipping_status, '') NOT IN (%s)" % ",".join("?" * len(OPEN)))
    args = list(OPEN)
    if only_id is not None:
        q += " AND id = ?"
        args.append(only_id)
    if statuses:
        q += " AND shipping_status IN (%s)" % ",".join("?" * len(statuses))
        args += list(statuses)
    checked = changed = 0
    for order in con.execute(q, args).fetchall():
        carrier = carrier_of(order)
        if carrier not in ready or carrier in blocked:
            continue
        by_page = carrier not in keyed
        if by_page:
            if pages >= page_limit:
                continue
            if pages:
                time.sleep(random.uniform(6, 12))  # one page at a time, at a person's pace
            pages += 1
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            if by_page:
                text = browsertrack.read_status(carrier, order["tracking_number"])
                status = status_from_text(text)
            else:
                status, text = TRACKERS[carrier](order["tracking_number"], env)
        except browsertrack.Blocked as e:
            logger.warning("%s tracking page: %s; skipping %s until the next round", carrier, e, carrier)
            blocked.add(carrier)
            continue
        except (CarrierError, browsertrack.NoInfo) as e:
            logger.warning("%s tracking for order %s: %s", carrier, order["id"], e)
            con.execute("UPDATE tracked_orders SET carrier_status=?, carrier_checked_at=? WHERE id=?",
                        (f"Couldn't check: {e}"[:300], now, order["id"]))
            con.commit()
            continue
        checked += 1
        con.execute("UPDATE tracked_orders SET carrier_status=?, carrier_checked_at=? WHERE id=?",
                    (text[:300], now, order["id"]))
        ordersheet.note_carrier(con, order["id"], text)  # the order's timeline
        if status and status_advances(order["shipping_status"], status):
            con.execute("UPDATE tracked_orders SET shipping_status=? WHERE id=?", (status, order["id"]))
            changed += 1
            logger.info("order %s: %s says %r, status %s -> %s", order["id"], carrier, text,
                        order["shipping_status"], status)
            notify.order_moved(dict(order), order["shipping_status"], status, now)  # tell the phone
        con.commit()  # save each order as it's checked, never holding the database across a page load
        if not by_page:
            time.sleep(0.3)  # stay well inside the carriers' free rate limits
    con.commit()
    logger.info("carrier tracking: checked %d order(s), %d changed", checked, changed)
    return checked, changed


def out_for_delivery(con):
    """How many open orders are out for delivery right now."""
    Store(con)
    return con.execute("SELECT COUNT(*) FROM tracked_orders WHERE shipping_status = 'out_for_delivery'"
                       " AND tracking_number IS NOT NULL AND tracking_number != ''").fetchone()[0]


def run(con, env, statuses=None):
    """Background refresh for the timer and the Check now button; one at a time. `statuses` limits it,
    like the quick look at packages out for delivery."""
    if state["running"]:
        return
    state["running"] = True
    try:
        checked, changed = refresh(con, env, statuses=statuses)
        state["last"] = (f"{datetime.now().strftime('%b %d %H:%M')}: checked {checked} package(s), "
                         f"{changed} status change(s)")
    except Exception as e:
        logger.exception("carrier tracking failed")
        state["last"] = f"Tracking check failed: {e}"
    finally:
        state["running"] = False
        browsertrack.release()
