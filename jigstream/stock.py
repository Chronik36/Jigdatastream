"""In-store stock: which nearby stores of a retailer show a product in stock.

Uses the same public store-availability lookups the retailers' own sites use (Target), or the
retailer's free official API (Best Buy). Every check is started by you, requests are spaced out,
and a retailer that refuses (403, 429, a bot check) is reported and left alone for a while.
Nothing here uses proxies, solves CAPTCHAs or tries to get around bot protection.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from html import unescape
from urllib.parse import urlencode, urlparse, parse_qs

from flask import Blueprint, redirect, render_template, request, url_for

from . import db
from .config import save_env

logger = logging.getLogger("jigstream.stock")
bp = Blueprint("stock", __name__)

TIMEOUT = 20
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
MIN_GAP = {"target": 4.0, "bestbuy": 1.0}  # seconds between requests to the same retailer
BLOCK_PAUSE = 15 * 60  # after a refusal, leave that retailer alone this long
AUTO_MIN_AGE = 10 * 60  # an automatic recheck reuses a result younger than this
RECHECK_CHOICES = (0, 15, 30, 60)  # minutes; 0 means off

# Target's website sends this key with every Redsky request; it's in the page source, not a secret.
TARGET_KEYS = ["9f36aeafbe60771e321a7cc95a78140772ab3e96", "ff457966e64d5e877fdbad070f276d18ecec4a01"]
TARGET_FIND = "https://redsky.target.com/redsky_aggregations/v1/web/fiats_v1"
TARGET_PDP = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1"
TARGET_PRODUCT = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"
TARGET_PRICING_STORE = "3991"  # any store works; prices are the same online
BESTBUY_API = "https://api.bestbuy.com/v1"

RETAILERS = {
    "target": "Target",
    "bestbuy": "Best Buy",
}
UNSUPPORTED = {
    "walmart": ("Walmart", "https://www.walmart.com/",
                "Walmart's store lookup sits behind bot protection, so BotTrack can't check it without getting around "
                "that, which it doesn't do. Use \"Pickup & delivery\" on the product page in your browser."),
    "gamestop": ("GameStop", "https://www.gamestop.com/",
                 "GameStop's store lookup sits behind bot protection, so BotTrack can't check it without getting "
                 "around that, which it doesn't do. Use \"Check store availability\" on the product page in your browser."),
}

IN_STOCK = {"IN_STOCK", "LIMITED_STOCK", "AVAILABLE", "LIMITED"}


class StockError(Exception):
    def __init__(self, message, code=None, detail=""):
        super().__init__(message)
        self.code, self.detail = code, detail


class Blocked(StockError):
    pass


_lock = threading.Lock()
_last_request = {}  # retailer -> time of last request
_paused_until = {}  # retailer -> time a block pause ends
_results = {}  # (retailer, product id, zip, radius) -> (time, result), for automatic rechecks
recent = deque(maxlen=15)


def detect_retailer(text):
    host = urlparse(text.strip()).netloc.lower() if "://" in text else text.strip().lower()
    for key, needle in (("target", "target.com"), ("bestbuy", "bestbuy.com"), ("walmart", "walmart.com"),
                        ("gamestop", "gamestop.com")):
        if needle in host:
            return key
    return ""


def parse_target_id(text):
    """TCIN from a Target link (…/A-12345678, ?preselect= for the chosen size/color) or a bare number."""
    text = text.strip()
    q = parse_qs(urlparse(text).query) if "://" in text else {}
    for key in ("preselect", "tcin"):
        if q.get(key) and q[key][0].isdigit():
            return q[key][0]
    m = re.search(r"/A-(\d{5,10})", text) or re.fullmatch(r"(?:A-)?(\d{5,10})", text, re.I)
    if not m:
        raise StockError("Paste a Target product link, or its TCIN (the number after \"A-\" in the link).")
    return m.group(1)


def parse_bestbuy_id(text):
    """Returns ("sku", n) from a Best Buy link or SKU, or ("upc", n) for a 12-13 digit barcode."""
    text = text.strip()
    q = parse_qs(urlparse(text).query) if "://" in text else {}
    if q.get("skuId") and q["skuId"][0].isdigit():
        return "sku", q["skuId"][0]
    m = re.search(r"/(\d{6,8})\.p\b", text)
    if m:
        return "sku", m.group(1)
    if re.fullmatch(r"\d{12,13}", text):
        return "upc", text
    if re.fullmatch(r"\d{6,8}", text):
        return "sku", text
    raise StockError("Paste a Best Buy product link, its SKU (7 digits, shown under the price), or the UPC barcode.")


def clean_zip(text):
    m = re.fullmatch(r"\s*(\d{5})(?:-\d{4})?\s*", text or "")
    if not m:
        raise StockError("Enter a 5-digit ZIP code.")
    return m.group(1)


def _pace(retailer):
    """Wait so requests to one retailer stay spaced out, and refuse while it's paused after a block."""
    with _lock:
        until = _paused_until.get(retailer, 0)
        if until > time.time():
            mins = max(1, round((until - time.time()) / 60))
            raise Blocked(f"{RETAILERS[retailer]} refused a request a few minutes ago, so BotTrack is leaving it alone "
                          f"for about {mins} more minute{'s' if mins != 1 else ''}.")
        wait = _last_request.get(retailer, 0) + MIN_GAP[retailer] - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request[retailer] = time.time()


def _pause(retailer, why):
    _paused_until[retailer] = time.time() + BLOCK_PAUSE
    logger.warning("%s refused a stock check (%s); pausing checks for %d minutes", RETAILERS[retailer], why,
                   BLOCK_PAUSE // 60)


def get_json(retailer, url, headers=None):
    """One plain GET. Returns parsed JSON; raises Blocked on a refusal, StockError otherwise."""
    _pace(retailer)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                               "Accept-Language": "en-US,en;q=0.9", **(headers or {})})
    name = RETAILERS[retailer]
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(2_000_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read(2000).decode("utf-8", errors="replace") if e.fp else ""
        if e.code == 429 or (e.code == 403 and "key" not in detail.lower()):
            _pause(retailer, f"HTTP {e.code}")
            raise Blocked(f"{name} refused the request (HTTP {e.code}). That usually means it's limiting automated "
                          f"checks from your network. BotTrack doesn't work around this; it will wait "
                          f"{BLOCK_PAUSE // 60} minutes before trying {name} again.") from e
        raise StockError(f"{name} answered HTTP {e.code}.", e.code, detail) from e
    except urllib.error.URLError as e:
        raise StockError(f"Couldn't reach {name}: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise StockError(f"{name} didn't answer in time ({e}).") from e
    try:
        return json.loads(body)
    except ValueError:
        if re.search(r"captcha|are you a (human|robot)|access denied|press (and|&) hold", body, re.I):
            _pause(retailer, "bot check page")
            raise Blocked(f"{name} answered with a bot check instead of stock data. BotTrack doesn't solve those; "
                          f"it will wait {BLOCK_PAUSE // 60} minutes before trying {name} again.")
        raise StockError(f"{name} sent something that isn't stock data.")


# --- Target -----------------------------------------------------------------------------------

def _target_key_from_site():
    """Read the current Redsky key from target.com's own page source, in case the built-in ones changed."""
    _pace("target")
    req = urllib.request.Request("https://www.target.com/", headers={"User-Agent": USER_AGENT,
                                                                     "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            html = resp.read(3_000_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None
    m = re.search(r'apiKey\\?"\s*:\s*\\?"([0-9a-f]{40})', html)
    return m.group(1) if m else None


def _target_request(base, params):
    keys = ([os.environ["TARGET_REDSKY_KEY"]] if os.environ.get("TARGET_REDSKY_KEY") else []) + TARGET_KEYS
    headers = {"Origin": "https://www.target.com", "Referer": "https://www.target.com/"}
    last = None
    for key in list(dict.fromkeys(keys)) + ["site"]:
        if key == "site":
            key = _target_key_from_site()
            if not key or key in keys:
                break
            logger.info("Using the Redsky key from target.com's page")
        try:
            data = get_json("target", base + "?" + urlencode({"key": key, **params}), headers)
        except Blocked:
            raise
        except StockError as e:
            last = e
            if e.code in (400, 401, 403) and "key" in e.detail.lower():
                continue  # this key was retired; try the next one
            raise
        if key != keys[0]:
            os.environ["TARGET_REDSKY_KEY"] = key
        return data
    raise last or StockError("Target's lookup didn't accept any known key.")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _addr(a):
    if not isinstance(a, dict):
        return a or ""
    line = a.get("address_line1") or a.get("street") or ""
    city = ", ".join(x for x in (a.get("city"), a.get("state") or a.get("region")) if x)
    return ", ".join(x for x in (line, city) if x)


def parse_target(data):
    """Pull store rows out of a fiats_v1 or pdp_fulfillment_v1 answer."""
    rows = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        store = node.get("store") if isinstance(node.get("store"), dict) else node
        sid = store.get("store_id") or store.get("location_id") or node.get("location_id")
        has_stock_info = any(k in node for k in ("location_available_to_promise_quantity", "in_store_only",
                                                  "order_pickup"))
        if sid and has_stock_info:
            qty = _num(node.get("location_available_to_promise_quantity"))
            statuses = [(node.get(k) or {}).get("availability_status", "") for k in ("in_store_only", "order_pickup")
                        if isinstance(node.get(k), dict)]
            in_stock = any(s in IN_STOCK for s in statuses) or bool(qty and qty > 0)
            limited = any(s.startswith("LIMITED") for s in statuses)
            rows.append({
                "store_id": str(sid),
                "name": store.get("location_name") or node.get("location_name") or f"Store {sid}",
                "address": _addr(store.get("mailing_address") or store.get("location_address")
                                 or node.get("location_address")),
                "distance": _num(store.get("distance") or node.get("distance")),
                "quantity": int(qty) if qty is not None else None,
                "in_stock": in_stock,
                "note": "Limited stock" if limited else
                        ("Not sold in this store" if "NOT_SOLD_IN_STORE" in statuses else ""),
                "status": ", ".join(s.replace("_", " ").lower() for s in statuses if s),
            })
            return
        for v in node.values():
            walk(v)

    walk(data.get("data", data))
    seen, out = set(), []
    for r in rows:
        if r["store_id"] not in seen:
            seen.add(r["store_id"])
            out.append(r)
    return out


def check_target(product, zip_code, radius):
    tcin = parse_target_id(product)
    params = {"tcin": tcin, "nearby": zip_code, "radius": radius, "limit": 20, "requested_quantity": 1,
              "include_only_available_stores": "false", "channel": "WEB", "page": f"/p/A-{tcin}"}
    try:
        stores = parse_target(_target_request(TARGET_FIND, params))
    except Blocked:
        raise
    except StockError as e:
        logger.info("Target nearby-store lookup failed (%s); trying the single-store lookup", e)
        stores = []
    if not stores:
        data = _target_request(TARGET_PDP, {"tcin": tcin, "zip": zip_code, "is_bot": "false"})
        stores = parse_target(data)
    if not stores:
        raise StockError("Target didn't return any stores for that item. Check the TCIN, or the item may be "
                         "online-only.")
    stores = [s for s in stores if s["distance"] is None or s["distance"] <= radius]
    stores.sort(key=lambda s: (not s["in_stock"], s["distance"] if s["distance"] is not None else 9e9))
    return {"retailer": "target", "product_id": tcin, "title": f"TCIN {tcin}",
            "link": f"https://www.target.com/p/-/A-{tcin}", "stores": stores, "quantities": True,
            "only_in_stock": False}


# --- Best Buy ---------------------------------------------------------------------------------

def bestbuy_key():
    return os.environ.get("BESTBUY_API_KEY", "").strip()


def _bb(path, **params):
    key = bestbuy_key()
    if not key:
        raise StockError("Best Buy needs a free API key first (see the box below).")
    try:
        return get_json("bestbuy", f"{BESTBUY_API}/{path}?" + urlencode({**params, "apiKey": key}))
    except Blocked as e:
        raise Blocked(str(e) + " If this keeps happening, your free key may have hit its daily limit.") from e
    except StockError as e:
        if e.code in (400, 401, 403):
            raise StockError("Best Buy didn't accept the API key. Check it was activated from the email Best Buy "
                             "sent, then save it again below.") from e
        if e.code == 404:
            raise StockError("Best Buy doesn't know that SKU.") from e
        raise


BB_SHOW = "sku,name,url,salePrice,regularPrice,image,thumbnailImage"


def _bb_info(p):
    price = _num(p.get("salePrice") or p.get("regularPrice"))
    return {"name": p.get("name") or "", "image": p.get("image") or p.get("thumbnailImage") or "",
            "price": f"${price:,.2f}" if price is not None else "", "link": p.get("url") or ""}


def check_bestbuy(product, zip_code, radius):
    kind, ident = parse_bestbuy_id(product)
    if kind == "upc":
        found = _bb(f"products(upc={ident})", show=BB_SHOW, format="json").get("products") or []
        if not found:
            raise StockError("Best Buy has no product with that UPC.")
        info = found[0]
    else:
        found = _bb(f"products(sku={ident})", show=BB_SHOW, format="json").get("products") or []
        info = found[0] if found else {"sku": ident}
    sku = str(info.get("sku") or ident)
    data = _bb(f"products/{sku}/stores.json", postalCode=zip_code)
    stores = []
    for s in data.get("stores") or []:
        dist = _num(s.get("distance"))
        if dist is not None and dist > radius:
            continue
        stores.append({
            "store_id": str(s.get("storeID") or s.get("storeId") or ""), "name": s.get("name") or "Best Buy",
            "address": ", ".join(x for x in (s.get("address"), s.get("city"), s.get("state")) if x),
            "distance": dist, "quantity": None, "in_stock": True,
            "note": "Low stock" if s.get("lowStock") else "",
            "status": "in stock" + (f", ready in {s['minPickupHours']} h" if s.get("minPickupHours") else ""),
        })
    stores.sort(key=lambda s: s["distance"] if s["distance"] is not None else 9e9)
    return {"retailer": "bestbuy", "product_id": sku, "title": info.get("name") or f"SKU {sku}",
            "link": info.get("url") or f"https://www.bestbuy.com/site/{sku}.p?skuId={sku}", "stores": stores,
            "quantities": False, "only_in_stock": True,
            "pickup_ok": data.get("ispuEligible"), "info": _bb_info(info) if info.get("name") else None}


CHECKERS = {"target": check_target, "bestbuy": check_bestbuy}


# --- Product details (picture, name, price) for saved products --------------------------------

def _find(node, keys):
    """First non-empty value under any of `keys`, searching nested dicts and lists."""
    if isinstance(node, dict):
        for k in keys:
            if node.get(k) not in (None, "", [], {}):
                return node[k]
        node = list(node.values())
    if isinstance(node, list):
        for v in node:
            if isinstance(v, (dict, list)):
                found = _find(v, keys)
                if found not in (None, ""):
                    return found
    return None


def target_info(tcin):
    params = {"tcin": tcin, "pricing_store_id": TARGET_PRICING_STORE, "store_id": TARGET_PRICING_STORE,
              "has_pricing_store_id": "true", "is_bot": "false"}
    data = _target_request(TARGET_PRODUCT, params)
    product = (data.get("data") or {}).get("product") or data
    title = _find(product, ["title"]) or ""
    price = _find(product, ["formatted_current_price"])
    if not price:
        amount = _num(_find(product, ["current_retail", "reg_retail"]))
        price = f"${amount:,.2f}" if amount is not None else ""
    return {"name": unescape(str(title)), "image": _find(product, ["primary_image_url"]) or "",
            "price": str(price or ""), "link": f"https://www.target.com/p/-/A-{tcin}"}


def bestbuy_info(sku):
    found = _bb(f"products(sku={sku})", show=BB_SHOW, format="json").get("products") or []
    if not found:
        raise StockError("Best Buy doesn't know that SKU.")
    return _bb_info(found[0])


def product_id(retailer, product):
    if retailer == "target":
        return parse_target_id(product)
    if retailer == "bestbuy":
        kind, ident = parse_bestbuy_id(product)
        if kind == "upc":
            found = _bb(f"products(upc={ident})", show="sku", format="json").get("products") or []
            if not found:
                raise StockError("Best Buy has no product with that UPC.")
            return str(found[0]["sku"])
        return ident
    raise StockError("Only Target and Best Buy products can be saved.")


INFO = {"target": target_info, "bestbuy": bestbuy_info}


# --- Saved products ---------------------------------------------------------------------------

def saved_path():
    return os.environ.get("STOCK_SAVED_FILE") or os.path.join(os.path.dirname(db.DB_PATH) or ".", "stock_saved.json")


def load_saved():
    try:
        with open(saved_path()) as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _write_saved(items):
    path = saved_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f, indent=1)
    os.replace(tmp, path)


def find_saved(retailer, pid):
    return next((p for p in load_saved() if p["retailer"] == retailer and p["id"] == str(pid)), None)


def save_product(retailer, product):
    """Saves (or refreshes) a product with its picture, name and price. Returns the saved entry."""
    pid = product_id(retailer, product)
    try:
        info = INFO[retailer](pid)
    except Blocked:
        raise
    except StockError as e:
        logger.info("Couldn't load details for %s %s: %s", RETAILERS[retailer], pid, e)
        info = {}
    items = load_saved()
    old = next((p for p in items if p["retailer"] == retailer and p["id"] == pid), None)
    entry = old or {"retailer": retailer, "id": pid, "saved": datetime.now().strftime("%b %-d, %Y")}
    for k in ("name", "image", "price", "link"):
        if info.get(k):
            entry[k] = info[k]
    entry.setdefault("link", "")
    if info.get("price"):
        entry["price_when"] = datetime.now().strftime("%b %-d")
    if not old:
        items.insert(0, entry)
    _write_saved(items)
    logger.info("%s %s %s (%s)", "Refreshed" if old else "Saved", RETAILERS[retailer], pid, entry.get("name") or "no details")
    return entry


def remove_saved(retailer, pid):
    _write_saved([p for p in load_saved() if not (p["retailer"] == retailer and p["id"] == str(pid))])
    logger.info("Removed saved %s %s", RETAILERS.get(retailer, retailer), pid)


def _update_saved_from_check(result):
    """A check that came back with fresh details (Best Buy does) keeps the saved name and price current."""
    info = result.get("info")
    if not info:
        return
    items = load_saved()
    for p in items:
        if p["retailer"] == result["retailer"] and p["id"] == result["product_id"]:
            changed = any(info.get(k) and p.get(k) != info[k] for k in ("name", "image", "price"))
            if changed:
                p.update({k: v for k, v in info.items() if v}, price_when=datetime.now().strftime("%b %-d"))
                _write_saved(items)
            return


def check(retailer, product, zip_code, radius, auto=False):
    """Runs one check. auto=True (the optional recheck) reuses a result younger than AUTO_MIN_AGE."""
    if retailer in UNSUPPORTED:
        raise StockError(UNSUPPORTED[retailer][2])
    if retailer not in CHECKERS:
        raise StockError("Pick a retailer, or paste a link from Target or Best Buy.")
    zip_code = clean_zip(zip_code)
    cache_key = (retailer, product.strip(), zip_code, radius)
    if auto and cache_key in _results and time.time() - _results[cache_key][0] < AUTO_MIN_AGE:
        return _results[cache_key][1]
    started = time.time()
    try:
        result = CHECKERS[retailer](product, zip_code, radius)
    except StockError as e:
        logger.info("Stock check %s %s near %s failed: %s", RETAILERS[retailer], product.strip()[:120], zip_code, e)
        _remember(retailer, product, zip_code, radius, error=str(e))
        raise
    result.update(zip=zip_code, radius=radius, checked=datetime.now().strftime("%-I:%M %p"),
                  in_stock=sum(1 for s in result["stores"] if s["in_stock"]))
    prev = _results.get(cache_key)
    if prev:
        before = {s["store_id"] for s in prev[1]["stores"] if s["in_stock"]}
        for s in result["stores"]:
            s["new"] = s["in_stock"] and s["store_id"] not in before
    _results[cache_key] = (time.time(), result)
    saved = find_saved(retailer, result["product_id"])
    if saved and saved.get("name") and not result.get("info"):
        result["title"] = saved["name"]
    logger.info("Stock check %s %s near %s (%d mi%s): %d of %d stores in stock, %.1fs", RETAILERS[retailer],
                result["product_id"], zip_code, radius, ", auto" if auto else "", result["in_stock"],
                len(result["stores"]), time.time() - started)
    _remember(retailer, product, zip_code, radius, result=result)
    _update_saved_from_check(result)
    return result


def _remember(retailer, product, zip_code, radius, result=None, error=None):
    entry = {"retailer": retailer, "name": RETAILERS.get(retailer, retailer), "product": product.strip(),
             "zip": zip_code, "radius": radius, "when": datetime.now().strftime("%b %-d, %-I:%M %p"),
             "title": result["title"] if result else "", "in_stock": result["in_stock"] if result else None,
             "error": error}
    for old in list(recent):
        if (old["retailer"], old["product"], old["zip"], old["radius"]) == (retailer, entry["product"], zip_code,
                                                                            radius):
            recent.remove(old)
    recent.appendleft(entry)


# --- Page -------------------------------------------------------------------------------------

def _form():
    src = request.form if request.method == "POST" else request.args
    product = (src.get("product") or "").strip()
    retailer = src.get("retailer") or ""
    if retailer in ("", "auto"):
        retailer = detect_retailer(product)
    try:
        radius = max(1, min(100, int(src.get("radius") or 25)))
    except ValueError:
        radius = 25
    try:
        recheck = int(src.get("recheck") or 0)
    except ValueError:
        recheck = 0
    return {"product": product, "retailer": retailer, "zip": (src.get("zip") or os.environ.get("STOCK_ZIP", "")).strip(),
            "radius": radius, "recheck": recheck if recheck in RECHECK_CHOICES else 0,
            "auto": src.get("auto") == "1"}


@bp.route("/stock", methods=["GET", "POST"])
def stock_page():
    form = _form()
    result = error = blocked = None
    run = request.method == "POST" or request.args.get("run") == "1"
    if run and form["product"]:
        try:
            result = check(form["retailer"], form["product"], form["zip"], form["radius"], auto=form["auto"])
            if form["zip"] != os.environ.get("STOCK_ZIP"):
                save_env({"STOCK_ZIP": clean_zip(form["zip"])})
        except Blocked as e:
            error, blocked = str(e), True
        except StockError as e:
            error = str(e)
    elif run:
        error = "Paste a product link or ID first."
    unsupported = UNSUPPORTED.get(form["retailer"])
    saved = load_saved()
    current = result and next((p for p in saved if p["retailer"] == result["retailer"]
                               and p["id"] == result["product_id"]), None)
    return render_template("stock.html", form=form, result=result, error=error or request.args.get("error"),
                           blocked=blocked, retailers=RETAILERS, unsupported=unsupported, recent=list(recent),
                           has_bb_key=bool(bestbuy_key()), recheck_choices=RECHECK_CHOICES, saved=saved,
                           current=current)


def _back(**extra):
    keep = {k: request.form.get(k) for k in ("product", "retailer", "zip", "radius") if request.form.get(k)}
    return redirect(url_for("stock.stock_page", **keep, **extra) + "#saved")


@bp.route("/stock/save", methods=["POST"])
def save_route():
    retailer = request.form.get("retailer") or detect_retailer(request.form.get("product", ""))
    if retailer not in RETAILERS:
        return _back(error="Only Target and Best Buy products can be saved.")
    try:
        save_product(retailer, request.form.get("product", ""))
    except StockError as e:
        return _back(error=f"Couldn't save that product: {e}")
    return _back()


@bp.route("/stock/saved/<retailer>/<pid>/refresh", methods=["POST"])
def refresh_saved(retailer, pid):
    if retailer not in RETAILERS:
        return _back()
    try:
        save_product(retailer, pid)
    except StockError as e:
        return _back(error=f"Couldn't refresh that product: {e}")
    return _back()


@bp.route("/stock/saved/<retailer>/<pid>/remove", methods=["POST"])
def remove_route(retailer, pid):
    remove_saved(retailer, pid)
    return _back()


@bp.route("/stock/bestbuy-key", methods=["POST"])
def save_bestbuy_key():
    key = (request.form.get("key") or "").strip()
    if key and not re.fullmatch(r"[A-Za-z0-9]{10,64}", key):
        return redirect(url_for("stock.stock_page", keyerror="1"))
    save_env({"BESTBUY_API_KEY": key})
    logger.info("Best Buy API key %s", "saved" if key else "removed")
    return redirect(url_for("stock.stock_page", retailer="bestbuy"))
