"""Products: research what to buy, what it costs at retail, what it resells for, and where the profit is.

You describe the kind of product you're after; BotTrack searches what stores list right now (Target and
Shopify stores) for real names, SKUs, prices and stock, with the local AI only choosing the search words and
dropping results that don't fit. For each tracked product BotTrack
looks up resale prices on the selling sites you use, once per click, with requests spaced out. A site that
refuses (403, 429, a bot check) is reported and left alone for a while; nothing here uses proxies, solves
CAPTCHAs or tries to get around bot protection. Sites that can't be read (app-only ones like Tradepost,
or blocked ones) take a price you type in.

Profit = market price minus what it costs at a store (its MSRP or your purchase price there). When stores
differ, all of them are listed and the cheapest, most profitable one is recommended.
"""

import json
import logging
import os
import re
import statistics
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from base64 import b64encode
from datetime import datetime
from html import unescape
from urllib.parse import quote_plus, urlencode, urlparse

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from . import db, keywords, stock, tasks
from .config import Config, save_env
from .llm import LLMClient, LLMError

logger = logging.getLogger("jigstream.products")
bp = Blueprint("products", __name__)

MIN_GAP = 3.0  # seconds between requests to the same site
BLOCK_PAUSE = 15 * 60  # after a refusal, leave that site alone this long
PAGE_TEXT = 6000  # characters of a results page shown to the local AI
READER_TASK = "Read resale prices"  # Training page task the page reader learns from

# Selling sites. kind: "ebay" (sold-listings page, or eBay's free API with a key), "web" (a search page read
# once), "app" (phone app only, so prices are typed in). {q} is replaced by the search words.
PLATFORMS = [
    {"key": "stockx", "name": "StockX", "kind": "web", "url": "https://stockx.com/search?s={q}"},
    {"key": "goat", "name": "GOAT", "kind": "web", "url": "https://www.goat.com/search?query={q}"},
    {"key": "ebay", "name": "eBay", "kind": "ebay",
     "url": "https://www.ebay.com/sch/i.html?_nkw={q}&LH_Sold=1&LH_Complete=1&LH_ItemCondition=1000&_ipg=120"},
    {"key": "tradepost", "name": "Tradepost", "kind": "app", "url": ""},
    {"key": "mercari", "name": "Mercari", "kind": "web",
     "url": "https://www.mercari.com/search/?keyword={q}&itemStatuses=2&itemConditions=1"},
    {"key": "vinted", "name": "Vinted", "kind": "web", "url": "https://www.vinted.com/catalog?search_text={q}"},
    {"key": "alias", "name": "Alias", "kind": "app", "url": ""},
]
APP_NOTE = {"tradepost": "Tradepost is a phone app with no website to read, so type its offer in.",
            "alias": "Alias is GOAT's seller app; it has no website of its own, so type its price in "
                     "(GOAT's prices are usually close)."}
TEST_QUERY = "pokemon elite trainer box"

# Listing words that mean it isn't a new, complete, genuine copy of the item.
BAD_LISTING = {"used", "empty", "opened", "damaged", "read", "proxy", "replica", "fake", "custom", "digital", "code",
               "codes", "only", "parts", "broken", "lot", "lots", "wrapper", "wrappers", "repack", "graded", "psa"}

_lock = threading.Lock()
_last = {}  # host -> time of last request
_paused = {}  # host -> time a block pause ends
_page_inputs = {}  # (product id, platform key) -> what the local AI was shown, so a correction can teach it
update_state = {"running": False, "done": 0, "total": 0, "current": "", "finished": None}
last_research = {"query": "", "searches": [], "items": [], "errors": {}, "error": None, "for": None}


class SourceError(Exception):
    pass


class Blocked(SourceError):
    pass


# --- Storage ----------------------------------------------------------------------------------

def data_path():
    return os.environ.get("PRODUCTS_FILE") or os.path.join(os.path.dirname(db.DB_PATH) or ".", "products.json")


def load():
    try:
        with open(data_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("products", [])
    data.setdefault("sources", [])  # your own selling sites: {"key", "name", "url", "kind": "web"}
    data.setdefault("off", [])  # platform keys turned off
    data.setdefault("status", {})  # platform key -> last test result
    return data


def save(data):
    path = data_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def platforms(data=None):
    data = data or load()
    return PLATFORMS + [dict(s, custom=True) for s in data["sources"]]


def platform(key, data=None):
    return next((p for p in platforms(data) if p["key"] == key), None)


def find(data, pid):
    return next((p for p in data["products"] if p["id"] == pid), None)


def today():
    return datetime.now().strftime("%b %-d")


def money(v):
    try:
        v = float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return round(v, 2) if 0 < v < 1_000_000 else None


def new_product(name, ptype="", sku="", msrp=None, retailers=(), in_production=None, why="", offers=(), image="",
                sku_label=""):
    """retailers + msrp: stores typed in by hand. offers: real store listings from research."""
    p = {"id": uuid.uuid4().hex[:10], "name": name.strip()[:200], "type": ptype.strip()[:80], "sku": sku.strip()[:60],
         "sku_label": sku_label, "in_production": in_production, "why": why.strip()[:300], "added": today(),
         "image": image, "retail": [], "market": {}}
    for o in offers:
        p["retail"].append({"store": o["store"], "price": o["price"], "kind": "msrp", "link": o.get("link", ""),
                            "from": "store listing" + (", sold out" if o.get("available") is False else ""),
                            "when": today()})
    for store in retailers:
        if str(store).strip():
            p["retail"].append({"store": str(store).strip()[:60], "price": money(msrp), "kind": "msrp", "link": "",
                                "from": "typed in", "when": today()})
    if msrp is not None and not p["retail"] and money(msrp):
        p["retail"].append({"store": "MSRP", "price": money(msrp), "kind": "msrp", "link": "", "from": "typed in",
                            "when": today()})
    return p


# --- Math -------------------------------------------------------------------------------------

def summarize(prices):
    """Median, low, high and count of a price list, with far-off outliers (a third / triple the median) dropped."""
    prices = [p for p in (money(x) for x in prices) if p]
    if not prices:
        return None
    mid = statistics.median(prices)
    kept = [p for p in prices if mid / 3 <= p <= mid * 3] or prices
    return {"median": round(statistics.median(kept), 2), "low": min(kept), "high": max(kept), "count": len(kept)}


def market_price(p):
    """The going resale price: the median across the selling sites that have a price."""
    vals = [m["price"] for m in p.get("market", {}).values() if m.get("price")]
    return round(statistics.median(vals), 2) if vals else None


def analysis(p):
    """Profit per store (market minus that store's price), cheapest store first; the best one is recommended."""
    market = market_price(p)
    stores = []
    for i, r in enumerate(p.get("retail", [])):
        if not r.get("price"):
            continue
        profit = round(market - r["price"], 2) if market is not None else None
        stores.append(dict(r, idx=i, profit=profit,
                           margin=round(100 * profit / r["price"]) if profit is not None and r["price"] else None))
    stores.sort(key=lambda s: s["price"])
    best = stores[0] if stores else None
    sell = max(((k, m) for k, m in p.get("market", {}).items() if m.get("price")), key=lambda km: km[1]["price"],
               default=None)
    return {"market": market, "stores": stores, "best": best, "profit": best and best["profit"],
            "margin": best and best["margin"], "sell_on": sell and sell[0], "sell_price": sell and sell[1]["price"],
            "sources": sum(1 for m in p.get("market", {}).values() if m.get("price"))}


def focus(products, top=5):
    """Products worth focusing on: positive profit, still being made, ranked by profit per unit, then margin."""
    rows = []
    for p in products:
        a = analysis(p)
        if a["profit"] is None or a["profit"] <= 0 or p.get("in_production") is False:
            continue
        rows.append((p, a))
    rows.sort(key=lambda pa: (pa[1]["profit"], pa[1]["margin"] or 0), reverse=True)
    out = []
    for p, a in rows[:top]:
        others = [s for s in a["stores"][1:] if s["price"] > a["best"]["price"]]
        reason = f"Buy at {a['best']['store']} for ${a['best']['price']:,.2f}"
        if others:
            reason += f" (${others[0]['price'] - a['best']['price']:,.2f} less than {others[0]['store']})"
        reason += f", resells around ${a['market']:,.2f}"
        if a["sell_on"]:
            reason += f", best on {platform_name(a['sell_on'])} at ${a['sell_price']:,.2f}"
        if a["sources"] < 2:
            reason += ". Only one selling site has a price, so check it before buying in bulk"
        out.append({"product": p, "a": a, "reason": reason + "."})
    return out


def platform_name(key):
    p = platform(key)
    return p["name"] if p else key


# --- Fetching ---------------------------------------------------------------------------------

def _pace(host, gap=None):
    """Keeps requests to one site spaced out (other sites aren't held up), and refuses while it's paused."""
    gap = MIN_GAP if gap is None else min(gap, MIN_GAP)
    with _lock:
        until = _paused.get(host, 0)
        if until > time.time():
            mins = max(1, round((until - time.time()) / 60))
            raise Blocked(f"{host} refused a request a few minutes ago, so BotTrack is leaving it alone for about "
                          f"{mins} more minute{'s' if mins != 1 else ''}.")
        slot = max(time.time(), _last.get(host, 0) + gap)
        _last[host] = slot
    if slot > time.time():
        time.sleep(slot - time.time())


def _pause(host, why):
    _paused[host] = time.time() + BLOCK_PAUSE
    logger.warning("%s refused a price lookup (%s); leaving it alone for %d minutes", host, why, BLOCK_PAUSE // 60)


BOT_PAGE = re.compile(r"captcha|are you a (human|robot)|access denied|press (and|&) hold|verify you are human|"
                      r"pardon our interruption|just a moment\.\.\.|enable javascript and cookies", re.I)


def fetch_page(url):
    """One plain page load. Returns html; raises Blocked on a refusal or bot check, SourceError otherwise."""
    host = urlparse(url).netloc
    _pace(host)
    try:
        _, html = keywords.fetch(url)
    except keywords.FetchError as e:
        if re.search(r"HTTP (401|403|429|503)", str(e)):
            _pause(host, str(e))
            raise Blocked(f"{host} refused the request. That usually means it limits automated visits; BotTrack "
                          f"doesn't work around this and will wait {BLOCK_PAUSE // 60} minutes before trying "
                          f"again.") from e
        raise SourceError(str(e)) from e
    if len(html) < 20000 and BOT_PAGE.search(html):
        _pause(host, "bot check page")
        raise Blocked(f"{host} answered with a bot check instead of results. BotTrack doesn't solve those; it will "
                      f"wait {BLOCK_PAUSE // 60} minutes before trying again.")
    return html


def _get_json(url, headers=None, data=None, gap=None):
    host = urlparse(url).netloc
    _pace(host, gap)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": keywords.USER_AGENT,
                                                          "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=keywords.TIMEOUT) as resp:
            return json.loads(resp.read(3_000_000).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            _pause(host, f"HTTP {e.code}")
            raise Blocked(f"{host} refused the request (HTTP {e.code}); BotTrack will leave it alone for "
                          f"{BLOCK_PAUSE // 60} minutes.") from e
        raise SourceError(f"{host} answered HTTP {e.code}.") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SourceError(f"Couldn't reach {host}: {e}") from e


def search_words(p):
    """What to search for on a selling site: the name, without words that only add noise."""
    name = re.sub(r"\s*[\(\[].*?[\)\]]", "", p["name"])
    return " ".join(name.split()[:10])


def core_words(name):
    return {w for w in keywords.words(name) if keywords._meaningful(w) and w not in keywords.GENERIC}


def matches(title, name):
    """Is a listing title the same product? Most of the name's main words, and no used/empty/fake words."""
    core = core_words(name)
    toks = set(keywords.words(title))
    # Other sizing runs, cuts, accessories and used/empty/fake listings, unless the product's own name says so.
    if not core or (toks & (BAD_LISTING | set(keywords.VARIANT_TERMS))) - set(keywords.words(name)):
        return False
    return len(core & toks) >= max(1, round(0.6 * len(core)))


def _text(html):
    return unescape(re.sub(r"<[^>]+>", " ", html)).split()


PRICE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")


def parse_ebay(html, name):
    """Sold prices from eBay's search results page (both its older s-item and newer s-card markup)."""
    prices = []
    for block in re.split(r"<li[^>]+class=\"[^\"]*\bs-(?:item|card)\b", html)[1:]:
        t = re.search(r'class="[^"]*s-(?:item|card)__title[^"]*"[^>]*>(.*?)</(?:div|span|h3)>', block, re.S)
        pr = re.search(r'class="[^"]*s-(?:item|card)__price[^"]*"[^>]*>(.*?)</span>', block, re.S)
        if not t or not pr:
            continue
        title = " ".join(_text(t.group(1)))
        if "shop on ebay" in title.lower() or " to " in " ".join(_text(pr.group(1))):  # placeholders and price ranges
            continue
        m = PRICE.search(" ".join(_text(pr.group(1))))
        if m and matches(title, name):
            prices.append(m.group(1))
    return prices


def parse_ld_listings(html, name):
    """Prices of matching listings from a page's schema.org data (ItemList / Product / Offer)."""
    p = keywords.PageParser()
    p.feed(html)
    p.close()
    prices = []

    def offer_price(node):
        offers = node.get("offers")
        for o in offers if isinstance(offers, list) else [offers]:
            if isinstance(o, dict):
                v = o.get("price") or o.get("lowPrice")
                if v is not None:
                    return v
        return None

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            kinds = node.get("@type") if isinstance(node.get("@type"), list) else [node.get("@type")]
            if "Product" in kinds and isinstance(node.get("name"), str):
                v = offer_price(node)
                if v is not None and matches(node["name"], name):
                    prices.append(v)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)

    for blob in p.ld:
        try:
            walk(json.loads(blob))
        except ValueError:
            continue
    return prices, " ".join(p.text)


# --- The local AI -----------------------------------------------------------------------------

def _client(config=None):
    config = config or Config.from_env()
    return LLMClient(config.llm_base_url, config.llm_model, config.llm_api_key, timeout=config.llm_timeout)


def _con():
    return db.connect(current_app.config.get("DB_PATH"))


def reader_task(c):
    """The Training page task that reads prices off a results page (made the first time it's needed)."""
    row = c.execute("SELECT * FROM ai_tasks WHERE name = ?", (READER_TASK,)).fetchone()
    if row:
        return dict(row)
    c.execute("INSERT INTO ai_tasks (name, instructions, answer, created_at) VALUES (?,?,?,?)",
              (READER_TASK, "You get a product name, a selling site and the text of that site's search results "
               "page. List the prices of listings that are the same product: new, sealed or deadstock, the same "
               "version and size run. Skip used, empty, opened, graded, replica and accessory listings. Reply "
               '{"prices": [numbers in US dollars]}; an empty list if no listing matches.', "json", tasks.now()))
    c.commit()
    return reader_task(c)


def ai_read_prices(c, p, plat, page_text, config=None):
    t = reader_task(c)
    text = json.dumps({"product": p["name"], "sku": p.get("sku", ""), "site": plat["name"],
                       "page": " ".join(page_text.split())[:PAGE_TEXT]}, ensure_ascii=False)
    _page_inputs[(p["id"], plat["key"])] = text
    answer = json.loads(tasks.answer(_client(config), c, t, text))
    prices = answer.get("prices") if isinstance(answer, dict) else answer
    return [x for x in prices if money(x)] if isinstance(prices, list) else []


def teach(c, pid, key, prices):
    """Save a correction as an example for the page reader. False if there's no page read to attach it to."""
    text = _page_inputs.get((pid, key))
    if not text:
        return False
    tasks.add_examples(c, reader_task(c)["id"], [(text, json.dumps({"prices": prices}))])
    logger.info("products: saved a page-reading example for %s", key)
    return True


# Research only shows what stores actually list right now: names, SKUs, prices and stock come from the stores'
# own product search (Target's, and Shopify stores' public search). The local AI only turns your description
# into search words and drops results that don't fit it; it never supplies a price or SKU. Unknown stays unknown.
DEFAULT_SHOPS = [("Kith", "kith.com"), ("Undefeated", "undefeated.com"), ("Bodega", "bdgastore.com"),
                 ("Concepts", "cncpts.com"), ("Shoe Palace", "shoepalace.com"), ("A Ma Maniere", "amamaniere.com"),
                 ("Jimmy Jazz", "jimmyjazz.com")]
TARGET_SEARCH = "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
SHOP_GAP = 1.0  # Shopify product pages are light; still one request a second per store
SKU_LOOKUPS = 3  # per store and search: how many results get their product page read for SKUs

WORDS_PROMPT = (
    "You turn a reseller's description of what they want to buy into store search words. Reply with JSON only: "
    '{"searches": ["2 to 4 short searches a store search box understands, most specific first"], '
    '"skip_words": ["words in listing names that mean it is NOT what they want, e.g. sticker, case, used"]}. '
    "Use product names, lines and model names people search for. No prices, SKUs or dates.")
FILTER_PROMPT = (
    "You check store search results against what a reseller asked for. Reply with JSON only: "
    '{"keep": [numbers of the results that are the kind of product asked for]}. '
    "Drop accessories, toys, clothing and other things that only share a word with the request.")


def shops(data=None):
    data = data or load()
    if "shops" not in data:
        data["shops"] = [{"name": n, "domain": d} for n, d in DEFAULT_SHOPS]
    return data["shops"]


def search_words_for(query, config=None):
    """(searches, skip words) for a description, from the local AI; the description itself if the AI is off."""
    try:
        answer = _client(config).chat_json(WORDS_PROMPT, query)
        searches = [str(s).strip()[:80] for s in answer.get("searches") or [] if str(s).strip()][:4]
        skip = {str(w).strip().lower() for w in answer.get("skip_words") or [] if str(w).strip()}
        if searches:
            return searches, skip
    except (LLMError, ValueError, AttributeError) as e:
        logger.info("products: local AI didn't make search words (%s); searching the description as typed", e)
    return [" ".join(query.split()[:8])], set()


def _offer(store, name, price, sku="", sku_label="SKU", available=None, link="", image="", brand="", ptype=""):
    return {"store": store, "name": " ".join(unescape(str(name)).split())[:200], "price": money(price),
            "sku": str(sku or "").strip()[:60], "sku_label": sku_label, "available": available, "link": link,
            "image": image, "brand": str(brand or "")[:60], "type": str(ptype or "")[:80]}


def target_search(words):
    params = {"keyword": words, "channel": "WEB", "count": 24, "offset": 0, "default_purchasability_filter": "true",
              "page": "/s/" + words, "pricing_store_id": stock.TARGET_PRICING_STORE, "platform": "desktop",
              "visitor_id": uuid.uuid4().hex.upper()}
    try:
        data = stock._target_request(TARGET_SEARCH, params)
    except stock.StockError as e:
        raise SourceError(f"Target: {e}") from e
    found = (((data.get("data") or {}).get("search") or {}).get("products")) or []
    out = []
    for p in found:
        item = p.get("item") or {}
        tcin = p.get("tcin") or item.get("tcin")
        title = stock._find(item, ["title"])
        if not tcin or not title:
            continue
        price = stock._find(p.get("price") or {}, ["current_retail", "current_retail_min", "reg_retail"])
        status = str(stock._find(p.get("fulfillment") or {}, ["availability_status"]) or "")
        out.append(_offer("Target", title, price, tcin, "Target TCIN",
                          None if not status else status.upper() in stock.IN_STOCK or "IN_STOCK" in status.upper(),
                          f"https://www.target.com/p/-/A-{tcin}", stock._find(item, ["primary_image_url"]) or "",
                          stock._find(item.get("primary_brand") or {}, ["name"]) or "",
                          stock._find(item.get("product_classification") or {}, ["name"]) or ""))
    return out


def shop_search(shop, words, lookups=SKU_LOOKUPS):
    """A Shopify store's public product search, then each top result's product page for its SKU and price."""
    base = "https://" + shop["domain"].strip("/")
    data = _get_json(f"{base}/search/suggest.json?" + urlencode(
        {"q": words, "resources[type]": "product", "resources[limit]": 8,
         "resources[options][unavailable_products]": "last"}), gap=SHOP_GAP)
    found = (((data.get("resources") or {}).get("results") or {}).get("products")) or []
    out = []
    for i, p in enumerate(found):
        if not p.get("title") or not p.get("handle"):
            continue
        link = base + "/products/" + p["handle"]
        image = p.get("image") or p.get("featured_image") or ""
        image = image.get("url", "") if isinstance(image, dict) else image
        offer = _offer(shop["name"], p["title"], p.get("price") or p.get("price_min"), "", "SKU", p.get("available"),
                       link, ("https:" + image) if str(image).startswith("//") else image, p.get("vendor"),
                       p.get("type") or p.get("product_type"))
        if i < lookups:
            try:
                full = _get_json(link + ".js", gap=SHOP_GAP)
                variants = full.get("variants") or []
                sku = next((v.get("sku") for v in variants if v.get("sku")), "")
                offer["sku"] = style_code(sku, p["title"]) or str(sku)[:60]
                if full.get("price") is not None:
                    offer["price"] = money(full["price"] / 100)  # product pages give cents
                offer["available"] = full.get("available", offer["available"])
            except SourceError as e:
                logger.info("products: %s product page %s: %s", shop["name"], p["handle"], e)
        out.append(offer)
    return out


def style_code(sku, title=""):
    """A sneaker style code like FV5029-100 out of a size-level SKU ("FV5029-100-10.5") or the title."""
    for text in (sku or "", title or ""):
        m = keywords.CODE.search(str(text).upper())
        if m:
            return m.group(0)
    return ""


def _key(o):
    code = style_code(o["sku"], o["name"])
    return code or " ".join(sorted(core_words(o["name"])))


def group_offers(offers, skip=()):
    """One row per product: the same style code, or the same main words, at any number of stores."""
    groups = {}
    for o in offers:
        if not o["price"]:
            continue
        toks = set(keywords.words(o["name"]))
        if toks & set(skip):
            continue
        g = groups.setdefault(_key(o), [])
        if all(x["store"] != o["store"] for x in g):
            g.append(o)
    items = []
    for g in groups.values():
        g.sort(key=lambda o: (o["available"] is False, o["price"]))
        first = g[0]
        sku_offer = next((o for o in g if o["sku"]), None)
        selling = [o for o in g if o["available"] is not False]
        facts = ", ".join(f"{o['store']} ${o['price']:,.2f}" + (" (sold out)" if o["available"] is False else "")
                          for o in g)
        items.append({"name": first["name"], "type": next((o["type"] for o in g if o["type"]), ""),
                      "brand": next((o["brand"] for o in g if o["brand"]), ""),
                      "image": next((o["image"] for o in g if o["image"]), ""),
                      "sku": sku_offer["sku"] if sku_offer else "",
                      "sku_label": sku_offer["sku_label"] if sku_offer else "",
                      "offers": g, "low": min(o["price"] for o in (selling or g)), "selling": len(selling),
                      "why": f"Listed at {facts}."})
    items.sort(key=lambda it: (-it["selling"], -len(it["offers"]), it["low"]))
    return items


def ai_filter(query, items, config=None):
    """Asks the local AI which results fit the request; keeps everything if it can't answer."""
    if len(items) < 2:
        return items
    listing = "\n".join(f"{i + 1}. {it['name']}" + (f" ({it['type']})" if it["type"] else "")
                        for i, it in enumerate(items[:40]))
    try:
        keep = _client(config).chat_json(FILTER_PROMPT, f"Request: {query}\n\nResults:\n{listing}").get("keep")
        idx = {int(k) - 1 for k in keep if str(k).strip().isdigit()} if isinstance(keep, list) else set()
    except (LLMError, ValueError, AttributeError, TypeError) as e:
        logger.info("products: local AI didn't filter results (%s); showing all", e)
        return items
    kept = [it for i, it in enumerate(items) if i in idx or i >= 40]
    return kept or items


def research(query, config=None, data=None, use_ai=True):
    """Real listings for a description: (search words used, products, store errors)."""
    data = data or load()
    searches, skip = search_words_for(query, config) if use_ai else ([" ".join(query.split()[:8])], set())
    offers, errors = [], {}
    jobs = [("Target", lambda w: target_search(w))] + [
        (s["name"], lambda w, s=s: shop_search(s, w)) for s in shops(data) if not s.get("off")]
    for words in searches:
        with ThreadPoolExecutor(max_workers=6) as pool:  # different stores at once; each store stays paced
            futures = {pool.submit(fn, words): name for name, fn in jobs if name not in errors}
            for fut, name in futures.items():
                try:
                    offers += fut.result()
                except Exception as e:  # one store's failure or odd answer leaves the others' results
                    errors[name] = str(e) if isinstance(e, SourceError) else f"unexpected answer ({e})"
                    logger.info("products: research search at %s: %s", name, e)
    items = group_offers(offers, skip)
    if use_ai:
        items = ai_filter(query, items, config)
    logger.info("products: research “%s” searched %s -> %d products from %d listings", query[:80], searches,
                len(items), len(offers))
    return searches, items[:30], errors


def similar(p, config=None, data=None):
    q = f"products similar to {p['name']}" + (f", {p['type']}" if p.get("type") else "") + ", sold now"
    searches, items, errors = research(q, config, data)
    own = core_words(p["name"])
    return searches, [it for it in items if core_words(it["name"]) != own], errors


# --- Looking up prices ------------------------------------------------------------------------

def ebay_keys():
    return os.environ.get("EBAY_CLIENT_ID", "").strip(), os.environ.get("EBAY_CLIENT_SECRET", "").strip()


_ebay_token = {"value": "", "until": 0}


def ebay_api_prices(name):
    """Asking prices of new listings from eBay's free Browse API (needs a free developer key)."""
    cid, secret = ebay_keys()
    if time.time() > _ebay_token["until"]:
        auth = b64encode(f"{cid}:{secret}".encode()).decode()
        tok = _get_json("https://api.ebay.com/identity/v1/oauth2/token",
                        {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
                        urlencode({"grant_type": "client_credentials",
                                   "scope": "https://api.ebay.com/oauth/api_scope"}).encode())
        _ebay_token.update(value=tok.get("access_token", ""), until=time.time() + int(tok.get("expires_in", 3600)) - 60)
    data = _get_json("https://api.ebay.com/buy/browse/v1/item_summary/search?" +
                     urlencode({"q": name, "filter": "conditions:{NEW},buyingOptions:{FIXED_PRICE}", "limit": 50}),
                     {"Authorization": f"Bearer {_ebay_token['value']}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
    return [(i.get("price") or {}).get("value") for i in data.get("itemSummaries") or []
            if matches(i.get("title", ""), name)]


def lookup(p, plat, use_ai=True, c=None, config=None):
    """Market price of one product on one selling site. Returns a market entry; raises SourceError/Blocked."""
    if plat["kind"] == "app":
        raise SourceError(APP_NOTE.get(plat["key"], f"{plat['name']} can't be read automatically; type its price in."))
    q = search_words(p)
    url = plat["url"].replace("{q}", quote_plus(q))
    if plat["kind"] == "ebay":
        try:
            prices, how = parse_ebay(fetch_page(url), p["name"]), "sold listings"
        except Blocked:
            if not all(ebay_keys()):
                raise
            prices = []
        if not prices and all(ebay_keys()):  # eBay's free API only has current listings, not sold ones
            prices, how = ebay_api_prices(q), "current listings (eBay API)"
    else:
        html = fetch_page(url)
        prices, page_text = parse_ld_listings(html, p["name"])
        how = "listings on the page"
    if not prices and use_ai and plat["kind"] != "ebay":
        try:
            prices = ai_read_prices(c, p, plat, page_text, config)
            how = "read by the local AI"
        except (LLMError, ValueError) as e:
            raise SourceError(f"Couldn't find prices on the page, and the local AI couldn't read it either "
                              f"({e}).") from e
    s = summarize(prices)
    if not s:
        raise SourceError(f"No matching listings found on {plat['name']} for “{q}”.")
    return {"price": s["median"], "low": s["low"], "high": s["high"], "count": s["count"], "how": how, "link": url,
            "when": today(), "manual": False}


def update_product(data, p, use_ai=True, c=None, config=None, keys=None):
    """Looks up every selling site that's on. Returns {platform key: error} for the ones that failed."""
    errors = {}
    for plat in platforms(data):
        if plat["key"] in data["off"] or plat["kind"] == "app" or (keys and plat["key"] not in keys):
            continue
        try:
            entry = lookup(p, plat, use_ai, c, config)
        except SourceError as e:
            errors[plat["key"]] = str(e)
            old = p["market"].get(plat["key"])
            if old:
                old["error"] = str(e)
            logger.info("products: %s on %s: %s", p["name"], plat["name"], e)
            continue
        p["market"][plat["key"]] = entry
        logger.info("products: %s on %s: $%.2f from %d %s", p["name"], plat["name"], entry["price"], entry["count"],
                    entry["how"])
    p["updated"] = datetime.now().strftime("%b %-d, %-I:%M %p")
    return errors


def retail_price(link):
    """(store, price) from a retailer product link: Target and Best Buy via the Stock tab's lookups, anything
    else from the page's schema.org price."""
    retailer = stock.detect_retailer(link)
    try:
        if retailer == "target":
            info = stock.target_info(stock.parse_target_id(link))
            return "Target", money(info.get("price")), info.get("name")
        if retailer == "bestbuy" and stock.bestbuy_key():
            kind, ident = stock.parse_bestbuy_id(link)
            info = stock.bestbuy_info(ident)
            return "Best Buy", money(info.get("price")), info.get("name")
    except stock.StockError as e:
        raise SourceError(str(e)) from e
    url = keywords.normalize_url(link)
    html = fetch_page(url)
    p = keywords.PageParser()
    p.feed(html)
    p.close()
    found, price = None, None
    for blob in p.ld:
        m = re.search(r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)', blob)
        if m:
            price = money(m.group(1))
            found = (re.search(r'"name"\s*:\s*"([^"]{3,200})"', blob) or [None, None])[1]
            break
    if not price:
        m = re.search(r'(?:product:price:amount|og:price:amount)"\s+content="([0-9.]+)"', html)
        price = money(m.group(1)) if m else None
    if not price:
        raise SourceError("Couldn't find a price on that page; type it in instead.")
    host = urlparse(url).netloc.replace("www.", "")
    return host.split(".")[0].title(), price, found


def test_sources(data, use_ai=False, c=None):
    """Tries each selling site once with a sample search and records whether it answered."""
    sample = {"id": "test", "name": TEST_QUERY, "sku": ""}
    for plat in platforms(data):
        if plat["kind"] == "app":
            data["status"][plat["key"]] = {"state": "app", "note": APP_NOTE.get(plat["key"], "Typed in."),
                                           "when": today()}
            continue
        try:
            entry = lookup(sample, plat, use_ai=use_ai, c=c)
            state = {"state": "ok", "note": f"{entry['count']} prices from {entry['how']}"}
        except Blocked as e:
            state = {"state": "blocked", "note": str(e)}
        except SourceError as e:
            state = {"state": "error", "note": str(e)}
        state["when"] = datetime.now().strftime("%b %-d, %-I:%M %p")
        data["status"][plat["key"]] = state
        logger.info("products: source test %s -> %s (%s)", plat["name"], state["state"], state["note"][:120])
    return data["status"]


# --- Pages ------------------------------------------------------------------------------------

def _back(anchor="", **args):
    return redirect(url_for("products.products_page", **args) + (f"#{anchor}" if anchor else ""))


@bp.get("/products")
def products_page():
    data = load()
    rows = [(p, analysis(p)) for p in data["products"]]
    sort = request.args.get("sort", "profit")
    if sort == "profit":
        rows.sort(key=lambda pa: (pa[1]["profit"] is None, -(pa[1]["profit"] or 0)))
    elif sort == "margin":
        rows.sort(key=lambda pa: (pa[1]["margin"] is None, -(pa[1]["margin"] or 0)))
    return render_template("products.html", rows=rows, focus=focus(data["products"]), research=last_research,
                           shops=shops(data),
                           platforms=platforms(data), off=data["off"], status=data["status"], sort=sort,
                           state=update_state, has_ebay=all(ebay_keys()), error=request.args.get("error"),
                           note=request.args.get("note"), open=request.args.get("open"), app_note=APP_NOTE,
                           tracked={p["name"].lower() for p in data["products"]})


def _run_research(query, pid=None, run=None):
    last_research.update(query=query, searches=[], items=[], errors={}, error=None, **{"for": pid})
    try:
        last_research["searches"], last_research["items"], last_research["errors"] = (run or research)(query)
    except Exception as e:  # a store's odd answer should never take the page down
        last_research["error"] = str(e)
        logger.exception("products: research failed")


@bp.post("/products/research")
def research_route():
    query = (request.form.get("query") or "").strip()
    if not query:
        return _back(error="Describe what you're looking for first.")
    _run_research(query)
    return _back("research")


@bp.post("/products/<pid>/similar")
def similar_route(pid):
    data = load()
    p = find(data, pid)
    if not p:
        return _back()
    _run_research(f"Similar to {p['name']}", pid, lambda q: similar(p, data=data))
    return _back("research")


@bp.post("/products/add")
def add_route():
    f = request.form
    data = load()
    picks = f.getlist("pick")
    items = [last_research["items"][int(i)] for i in picks if i.isdigit() and int(i) < len(last_research["items"])]
    have = {p["name"].lower() for p in data["products"]}
    if not picks:
        name = (f.get("name") or "").strip()
        if not name:
            return _back(error="Give the product a name.")
        new = [new_product(name, f.get("type", ""), f.get("sku", ""), money(f.get("msrp")),
                           [f.get("store")] if f.get("store") else [])]
    else:
        new = [new_product(it["name"], it["type"], it["sku"], offers=it["offers"], image=it["image"],
                           sku_label=it["sku_label"], why=it["why"],
                           in_production=True if it["selling"] else None) for it in items]
    added = 0
    for p in new:
        if p["name"].lower() not in have:
            data["products"].insert(0, p)
            added += 1
    save(data)
    logger.info("products: tracking %d new product%s", added, "" if added == 1 else "s")
    return _back("tracked", note=f"Added {added} product{'' if added == 1 else 's'}." if added else
                 "Those products are already tracked.")


@bp.post("/products/shops/add")
def shop_add_route():
    data = load()
    name, domain = (request.form.get("name") or "").strip()[:40], (request.form.get("domain") or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0]
    if not name or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        return _back("research", error="Give the store a name and its web address, e.g. kith.com.")
    if any(s["domain"] == domain for s in shops(data)):
        return _back("research", error="That store is already searched.")
    shops(data).append({"name": name, "domain": domain})
    save(data)
    logger.info("products: research also searches %s (%s)", name, domain)
    return _back("research")


@bp.post("/products/shops/<domain>/remove")
def shop_remove_route(domain):
    data = load()
    data["shops"] = [s for s in shops(data) if s["domain"] != domain]
    save(data)
    return _back("research")


@bp.post("/products/<pid>/edit")
def edit_route(pid):
    data = load()
    p = find(data, pid)
    if p:
        f = request.form
        p["name"] = (f.get("name") or p["name"]).strip()[:200]
        p["type"], p["sku"] = f.get("type", "").strip()[:80], f.get("sku", "").strip()[:60]
        p["in_production"] = {"yes": True, "no": False}.get(f.get("in_production"))
        save(data)
    return _back(f"p-{pid}", open=pid)


@bp.post("/products/<pid>/remove")
def remove_route(pid):
    data = load()
    data["products"] = [p for p in data["products"] if p["id"] != pid]
    save(data)
    return _back("tracked")


@bp.post("/products/<pid>/retail")
def retail_route(pid):
    data = load()
    p = find(data, pid)
    if not p:
        return _back()
    f = request.form
    store, price, link = (f.get("store") or "").strip(), money(f.get("price")), (f.get("link") or "").strip()
    if link and not price:
        try:
            found_store, price, _ = retail_price(link)
            store = store or found_store
        except (SourceError, keywords.FetchError) as e:
            return _back(f"p-{pid}", open=pid, error=f"Couldn't read the price from that link: {e}")
    if not store or not price:
        return _back(f"p-{pid}", open=pid, error="Give the store and its price (or a product link to read it from).")
    p["retail"] = [r for r in p["retail"] if r["store"].lower() != store.lower() or r.get("from") != "AI guess"]
    p["retail"].append({"store": store[:60], "price": price, "kind": "cost" if f.get("kind") == "cost" else "msrp",
                        "link": link, "from": "link" if link else "typed in", "when": today()})
    save(data)
    return _back(f"p-{pid}", open=pid)


@bp.post("/products/<pid>/retail/<int:idx>/remove")
def retail_remove_route(pid, idx):
    data = load()
    p = find(data, pid)
    if p and 0 <= idx < len(p["retail"]):
        p["retail"].pop(idx)
        save(data)
    return _back(f"p-{pid}", open=pid)


@bp.post("/products/<pid>/market")
def market_route(pid):
    """A resale price typed in (app-only or blocked sites). With teach=1, also corrects the page reader."""
    data = load()
    p = find(data, pid)
    key = request.form.get("platform", "")
    if not p or not platform(key, data):
        return _back()
    raw = request.form.get("price", "")
    prices = [money(x) for x in re.split(r"[,\s]+", raw) if money(x)]
    if not prices:
        p["market"].pop(key, None)  # an empty price clears it
    else:
        s = summarize(prices)
        p["market"][key] = {"price": s["median"], "low": s["low"], "high": s["high"], "count": s["count"],
                            "how": "typed in", "link": request.form.get("link", ""), "when": today(), "manual": True}
        if request.form.get("teach"):
            teach(_con(), pid, key, prices)
    save(data)
    return _back(f"p-{pid}", open=pid)


@bp.post("/products/<pid>/update")
def update_route(pid):
    if update_state["running"]:
        return _back("tracked", error="Prices are already being updated; wait for that to finish.")
    data = load()
    p = find(data, pid)
    if not p:
        return _back()
    errors = update_product(data, p, use_ai=bool(request.form.get("ai")), c=_con())
    save(data)
    msg = "; ".join(f"{platform_name(k)}: {v}" for k, v in errors.items())
    return _back(f"p-{pid}", open=pid, **({"error": msg} if msg else {}))


@bp.post("/products/update-all")
def update_all_route():
    if update_state["running"]:
        return _back("tracked")
    use_ai, db_path = bool(request.form.get("ai")), current_app.config.get("DB_PATH")
    total = len(load()["products"])
    update_state.update(running=True, done=0, total=total, current="", finished=None)

    def run():
        c = db.connect(db_path)
        try:
            for pid in [p["id"] for p in load()["products"]]:
                data = load()  # re-read each time so edits made meanwhile aren't lost
                p = find(data, pid)
                if p:
                    update_state["current"] = p["name"]
                    update_product(data, p, use_ai, c)
                    save(data)
                update_state["done"] += 1
        except Exception:
            logger.exception("products: updating all prices failed")
        finally:
            update_state.update(running=False, current="", finished=datetime.now().strftime("%-I:%M %p"))

    threading.Thread(target=run, daemon=True).start()
    return _back("tracked")


@bp.post("/products/sources/test")
def test_route():
    data = load()
    test_sources(data, use_ai=bool(request.form.get("ai")), c=_con())
    save(data)
    return _back("sources")


@bp.post("/products/sources/add")
def source_add_route():
    data = load()
    name, url = (request.form.get("name") or "").strip()[:40], (request.form.get("url") or "").strip()
    if not name or not url:
        return _back("sources", error="Give the selling site a name and its search link.")
    try:
        url = keywords.normalize_url(url)
    except keywords.FetchError as e:
        return _back("sources", error=str(e))
    if "{q}" not in url:
        return _back("sources", error="Put {q} in the search link where the search words go, e.g. "
                                      "https://example.com/search?q={q}")
    key = "x-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if platform(key, data):
        return _back("sources", error="There's already a selling site with that name.")
    data["sources"].append({"key": key, "name": name, "url": url, "kind": "web"})
    save(data)
    logger.info("products: added selling site %s", name)
    return _back("sources")


@bp.post("/products/sources/<key>/remove")
def source_remove_route(key):
    data = load()
    data["sources"] = [s for s in data["sources"] if s["key"] != key]
    data["status"].pop(key, None)
    save(data)
    return _back("sources")


@bp.post("/products/sources/<key>/toggle")
def source_toggle_route(key):
    data = load()
    if key in data["off"]:
        data["off"].remove(key)
    elif platform(key, data):
        data["off"].append(key)
    save(data)
    return _back("sources")


@bp.post("/products/ebay-key")
def ebay_key_route():
    cid, secret = (request.form.get("client_id") or "").strip(), (request.form.get("client_secret") or "").strip()
    if (cid or secret) and not (re.fullmatch(r"[\w.-]{8,128}", cid) and re.fullmatch(r"[\w.-]{8,128}", secret)):
        return _back("sources", error="That doesn't look like an eBay App ID and Cert ID.")
    save_env({"EBAY_CLIENT_ID": cid, "EBAY_CLIENT_SECRET": secret})
    _ebay_token.update(value="", until=0)
    logger.info("products: eBay API key %s", "saved" if cid else "removed")
    return _back("sources")
