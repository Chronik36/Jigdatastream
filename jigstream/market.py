"""Market value: what each item you bought resells for now, what you'd make, and where prices are heading.

Every item on an order that isn't sold or cancelled gets a market price. First the local AI turns the name the
store's email used (Target's are often cut off or garbled) into the real product and a few search words; then
free public pages are read for that product: PriceCharting (sealed and card prices, with its monthly price
history), TCGplayer's market price, eBay sold listings and your own Tradepost payouts. The AI only names the
product: every price comes from a real page or from you. The market price is the middle of the prices found.

Pages are read one at a time with gaps between them (products.fetch_page). When a site answers with a bot check,
the Mac app tries it once in its hidden Safari window, the same way the Shipping tab reads carrier pages, and if
that shows a check too the site is left alone for a while. Nothing solves CAPTCHAs or uses proxies.

Wrong product? Fix the name: the AI learns from it (Training task "Name the product") and the prices, picture and
trend are looked up again for the fixed name. Prices can be typed in for any source (Tradepost has no website).

Profit = market price minus what you paid (jacob's rule). Projections carry each product's recent monthly trend
forward (PriceCharting's history, else BotTrack's own lookups), capped so one odd month can't run away.
"""
import json
import logging
import math
import re
import statistics
import threading
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import quote, quote_plus

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from . import db, products, tasks
from .llm import LLMError

logger = logging.getLogger("jigstream.market")
bp = Blueprint("market", __name__)

NAME_TASK = "Name the product"
STALE_HOURS = 20  # an item's prices are looked up again after this long
ROUND = 25  # most products looked up in one background round
MAX_RATE = 0.10  # projections never grow or shrink faster than 10% a month
HORIZONS = [(1, "In 30 days"), (3, "In 90 days"), (6, "In 6 months")]
DONE = ("cancelled", "returned", "refunded", "not_an_order", "expense", "sale")

SOURCES = [("pricecharting", "PriceCharting"), ("tcgplayer", "TCGplayer"), ("ebay", "eBay sold"),
           ("tradepost", "Tradepost"), ("yours", "Your sales")]
SOURCE_NAME = dict(SOURCES, market="Your price")

# Listing words that mean a different thing than the sealed product: graded, opened, a lot, a code card, a case.
BAD = {"graded", "psa", "bgs", "cgc", "lot", "lots", "empty", "opened", "used", "damaged", "code", "codes", "digital",
       "online", "proxy", "fake", "custom", "repack", "case", "sleeve", "sleeves", "only", "wrapper", "japanese",
       "korean", "chinese", "german", "french", "italian", "spanish", "portuguese", "binder", "dice", "promo"}
FILLER = {"the", "and", "for", "with", "new", "sealed", "tcg", "trading", "card", "cards", "game", "pokemon",
          "pok", "mon", "of", "a", "an", "in", "set", "item", "factory", "english"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_items (
    key TEXT PRIMARY KEY,
    raw TEXT,
    store TEXT,
    name TEXT,
    search TEXT,
    name_how TEXT,
    image TEXT,
    image_manual INTEGER DEFAULT 0,
    images TEXT,
    prices TEXT,
    matched TEXT,
    history TEXT,
    chart TEXT,
    market_manual REAL,
    ignored INTEGER DEFAULT 0,
    errors TEXT,
    updated_at TEXT,
    created_at TEXT
);
"""

state = {"running": False, "done": 0, "total": 0, "current": "", "finished": None, "last": ""}
_run_lock = threading.Lock()


def ensure(con):
    return db.setup_once(con, "market", lambda c: (c.executescript(SCHEMA), c.commit()))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key(name):
    """The same item however a store spells or spaces it: lower-case letters and numbers only."""
    return re.sub(r"[^a-z0-9]+", "", unescape(str(name or "")).lower().replace("é", "e"))[:160]


def words(text):
    t = unescape(str(text or "")).lower().replace("é", "e").replace("&", " and ")
    return [w for w in re.findall(r"[a-z0-9]+", t)]


def core(text):
    return {w for w in words(text) if w not in FILLER and (len(w) > 1 or w.isdigit())}


# --- Naming the product --------------------------------------------------------------------------

def cleanup(raw):
    """Search words without the AI: the name minus store filler, cut-off endings and brackets."""
    s = unescape(str(raw or ""))
    s = re.sub(r"(\.\.\.|…)\s*$", "", s)
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s)
    s = re.sub(r"(?i)pok[eé]mon\s+(trading\s+card\s+game|tcg)\s*[:\-—–]?\s*", "", s)
    s = re.sub(r"(?i)\b(scarlet\s*(&|and)\s*violet|sword\s*(&|and)\s*shield|sun\s*(&|and)\s*moon)\s*[—–:\-]\s*", "", s)
    s = re.sub(r"[—–:|/]+", " ", s)
    return " ".join(s.split()[:9])


def name_task(c):
    """The Training page task that names a product from a store's email (made the first time it's needed)."""
    row = c.execute("SELECT * FROM ai_tasks WHERE name = ?", (NAME_TASK,)).fetchone()
    if row:
        return dict(row)
    c.execute("INSERT INTO ai_tasks (name, instructions, answer, created_at) VALUES (?,?,?,?)",
              (NAME_TASK, "You get an item name exactly as a store's order email wrote it, and the store. Store "
               "emails often cut names off, abbreviate them or garble them (Target especially). Say which real "
               "product it is, as the maker sells it, and give the few words a reseller would search for it on "
               "eBay or PriceCharting (set or series name plus product type, e.g. \"Surging Sparks Elite Trainer "
               "Box\"). Keep the product type exact: Elite Trainer Box, Booster Bundle, Booster Box, Booster Pack, "
               "blister, tin, collection box and so on are different products. Never make up a set: if you can't "
               "tell, give the name cleaned up and sure false. Reply "
               '{"name": "full product name", "search": "search words", "sure": true or false}.',
               "json", tasks.now()))
    c.commit()
    return name_task(c)


def _name_input(raw, store):
    return json.dumps({"store": store or "", "item": raw}, ensure_ascii=False)


def identify(c, raw, store="", llm=None):
    """(name, search words, how). The local AI names it; without the AI the cleaned name is used."""
    fallback = cleanup(raw) or raw
    if llm is None:
        return raw, fallback, "as written"
    try:
        answer = json.loads(tasks.answer(llm, c, name_task(c), _name_input(raw, store)))
    except (LLMError, ValueError, TypeError) as e:
        logger.info("market: the local AI couldn't name %r (%s); searching the name as written", raw[:80], e)
        return raw, fallback, "as written"
    if not isinstance(answer, dict):
        return raw, fallback, "as written"
    name = " ".join(str(answer.get("name") or "").split())[:200] or raw
    search = " ".join(str(answer.get("search") or "").split())[:120] or cleanup(name)
    return name, search, "AI" if answer.get("sure", True) else "AI, unsure"


def teach_name(c, raw, store, name, search=""):
    """A fixed name becomes an example for the namer."""
    out = {"name": name, "search": search or cleanup(name), "sure": True}
    tasks.add_examples(c, name_task(c)["id"], [(_name_input(raw, store), json.dumps(out, ensure_ascii=False))])
    logger.info("market: the AI learned a product name (%s)", name[:80])


# --- Reading prices -------------------------------------------------------------------------------

def best_match(candidates, want):
    """The candidate whose title is closest to the wanted words: most of them, few extra, no bad words."""
    want_core = core(want)
    if not want_core:
        return None
    best, top = None, 0.0
    for cand in candidates:
        have = core(cand["title"])
        if not have or (have & BAD) - want_core:
            continue
        hit = len(want_core & have)
        if hit < max(1, math.ceil(0.6 * len(want_core))):
            continue
        score = hit / len(want_core | have)
        if score > top:
            best, top = cand, score
    return best


def _text(html):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html or "")).split())


def _price(text):
    m = products.PRICE.search(text or "")
    return products.money(m.group(1)) if m else None


def browser_html(url, ready_word=""):
    """The page from the Mac app's hidden Safari window, or None outside the app. Raises products.Blocked if it
    also shows a bot check."""
    from . import browsertrack
    reader = browsertrack._reader
    if reader is None:
        return None
    try:
        html = reader(url, lambda t: bool(t) and (ready_word.lower() in t.lower() or
                                                  products.BOT_PAGE.search(t[:20000])), 40, html=True)
    except TypeError:  # an older window without page source reading
        return None
    if not html or (len(html) < 20000 and products.BOT_PAGE.search(html)):
        raise products.Blocked(f"{url.split('/')[2]} showed a bot check in the app's window too, so it's left alone "
                               "for now.")
    return html


def fetch(url, ready_word=""):
    """A page, plainly; when the site answers with a refusal or a bot check, once more in the app's window."""
    try:
        return products.fetch_page(url)
    except products.Blocked:
        html = browser_html(url, ready_word)
        if html is None:
            raise
        logger.info("market: read %s through the app's window", url.split("/")[2])
        return html


PC_ROW = re.compile(r'<tr[^>]*id="product-\d+"[^>]*>(.*?)</tr>', re.S)


def parse_pricecharting_search(html):
    out = []
    for row in PC_ROW.findall(html or ""):
        t = re.search(r'<td class="title"[^>]*>(.*?)</td>', row, re.S)
        link = re.search(r'href="((?:https://www\.pricecharting\.com)?/game/[^"]+)"', row)
        pr = re.search(r'class="price[^"]*used_price[^"]*"[^>]*>(.*?)</td>', row, re.S)
        img = re.search(r'<img[^>]+src="([^"]+)"', row)
        if not t or not link:
            continue
        href = link.group(1)
        out.append({"title": _text(t.group(1)), "price": _price(_text(pr.group(1))) if pr else None,
                    "link": href if href.startswith("http") else "https://www.pricecharting.com" + href,
                    "image": re.sub(r"/\d+\.jpg$", "/240.jpg", img.group(1)) if img else ""})
    return out


def parse_pricecharting_product(html, link=""):
    """{title, price, image, chart, link} from a PriceCharting product page."""
    title = re.search(r'<h1[^>]*id="product_name"[^>]*>(.*?)</h1>', html or "", re.S)
    if not title:
        title = re.search(r"<title>(.*?)</title>", html or "", re.S)
    t = _text(title.group(1)) if title else ""
    t = re.sub(r"\s*\|\s*", " ", re.sub(r"\s+Prices?\b", "", t)).replace("Pokemon Cards", "").strip()
    pr = re.search(r'id="used_price"[^>]*>(.*?)</td>', html or "", re.S)
    price = _price(_text(pr.group(1))) if pr else None
    img = re.search(r'(https://storage\.googleapis\.com/images\.pricecharting\.com/[^"\'/]+/)\d+\.jpg', html or "")
    chart = []
    m = re.search(r"VGPC\.chart_data\s*=\s*(\{.*?\});", html or "", re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            series = next((data[k] for k in ("used", "new", "cib") if any(v for _, v in data.get(k) or [])), [])
            for ms, cents in series:
                if cents:
                    chart.append([datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m"), cents / 100])
        except (ValueError, TypeError, KeyError):
            chart = []
    return {"title": t, "price": price, "image": (img.group(1) + "240.jpg") if img else "", "chart": chart[-12:],
            "link": link}


def pricecharting(search):
    url = "https://www.pricecharting.com/search-products?" + f"q={quote_plus(search)}&type=prices"
    html = fetch(url, "price")
    if 'id="product_name"' in html or "VGPC.chart_data" in html:  # one match: the site went straight to it
        found = parse_pricecharting_product(html, url)
    else:
        pick = best_match(parse_pricecharting_search(html), search)
        if not pick:
            raise products.SourceError(f"PriceCharting has nothing matching “{search}”.")
        found = parse_pricecharting_product(fetch(pick["link"], "ungraded"), pick["link"])
        found["title"] = found["title"] or pick["title"]
        found["price"] = found["price"] or pick["price"]
        found["image"] = found["image"] or pick["image"]
    if not found["price"]:
        raise products.SourceError("PriceCharting's page had no ungraded price.")
    return {"price": found["price"], "count": 1, "how": "PriceCharting ungraded price", "link": found["link"],
            "title": found["title"], "image": found["image"], "chart": found["chart"]}


TCG_SEARCH = "https://mp-search-api.tcgplayer.com/v1/search/request?q={}&isList=false"


def tcgplayer(search):
    """TCGplayer's market price, from the search its own site uses."""
    body = {"algorithm": "sales_synonym_v2", "from": 0, "size": 24, "filters": {"term": {}, "range": {}, "match": {}},
            "listingSearch": {"context": {"cart": {}}, "filters": {"term": {"sellerStatus": "Live", "channelId": 0},
                                                                    "range": {"quantity": {"gte": 1}},
                                                                    "exclude": {"channelExclusion": 0}}},
            "context": {"cart": {}, "shippingCountry": "US", "userProfile": {}},
            "settings": {"useFuzzySearch": True, "didYouMean": {}}, "sort": {}}
    data = products._get_json(TCG_SEARCH.format(quote(search)), {"Content-Type": "application/json",
                                                                  "Origin": "https://www.tcgplayer.com"},
                              json.dumps(body).encode())
    return pick_tcgplayer(data, search)


def pick_tcgplayer(data, search):
    found = []
    for block in (data or {}).get("results") or []:
        for r in block.get("results") or []:
            price = products.money(r.get("marketPrice")) or products.money(r.get("lowestPrice"))
            if not price or not r.get("productName"):
                continue
            pid = int(r.get("productId") or 0)
            found.append({"title": f"{r['productName']} {r.get('setName') or ''}".strip(), "price": price,
                          "link": f"https://www.tcgplayer.com/product/{pid}" if pid else "https://www.tcgplayer.com",
                          "image": f"https://tcgplayer-cdn.tcgplayer.com/product/{pid}_200w.jpg" if pid else ""})
    pick = best_match(found, search)
    if not pick:
        raise products.SourceError(f"TCGplayer has nothing matching “{search}”.")
    return dict(pick, count=1, how="TCGplayer market price")


def ebay(search):
    url = ("https://www.ebay.com/sch/i.html?_nkw=" + quote_plus(search) +
           "&LH_Sold=1&LH_Complete=1&LH_ItemCondition=1000&_ipg=120")
    prices = parse_ebay(fetch(url, "sold"), search)
    s = products.summarize(prices)
    if not s:
        raise products.SourceError(f"No eBay sold listings matched “{search}”.")
    return {"price": s["median"], "low": s["low"], "high": s["high"], "count": s["count"],
            "how": f"middle of {s['count']} sold listing{'s' if s['count'] != 1 else ''}", "link": url}


def parse_ebay(html, search):
    """Sold prices of listings that are this product (graded, lots, cases and empties left out)."""
    prices = []
    for block in re.split(r"<li[^>]+class=\"[^\"]*\bs-(?:item|card)\b", html or "")[1:]:
        t = re.search(r'class="[^"]*s-(?:item|card)__title[^"]*"[^>]*>(.*?)</(?:div|span|h3)>', block, re.S)
        pr = re.search(r'class="[^"]*s-(?:item|card)__price[^"]*"[^>]*>(.*?)</span>', block, re.S)
        if not t or not pr:
            continue
        title, price_text = _text(t.group(1)), _text(pr.group(1))
        if "shop on ebay" in title.lower() or " to " in price_text:
            continue
        if best_match([{"title": title}], search):
            p = _price(price_text)
            if p:
                prices.append(p)
    return prices


def tradepost(c, name, search):
    """What Tradepost paid you for this product before (sale emails and typed-in sales), per item."""
    from . import sales
    sales.ensure(c)
    want = core(search) | core(name)
    got = []
    for r in c.execute("SELECT item, payout, sale_price, fees FROM sales WHERE lower(platform) LIKE '%trade%post%'"
                       " AND item IS NOT NULL ORDER BY date DESC LIMIT 400"):
        if best_match([{"title": r["item"]}], search) or (want and len(want & core(r["item"])) >= max(2, len(want) - 1)):
            v = sales.money_in(dict(r))
            if v and v > 0:
                got.append(v)
    s = products.summarize(got[:20])
    if not s:
        raise products.SourceError("No Tradepost sales of this product yet. Type Tradepost's offer in.")
    return {"price": s["median"], "low": s["low"], "high": s["high"], "count": s["count"],
            "how": f"your last {s['count']} Tradepost payout{'s' if s['count'] != 1 else ''}", "link": ""}


def yours(c, name, search):
    """What you really sold this product for, on any platform except Tradepost (which has its own line above): the
    sale price, else what you got. These are real sales, so they correct the market price."""
    from . import sales
    sales.ensure(c)
    want = core(search) | core(name)
    got = []
    for r in c.execute("SELECT item, payout, sale_price, fees, platform FROM sales WHERE item IS NOT NULL"
                       " AND COALESCE(counted, 1) = 1 ORDER BY date DESC, id DESC LIMIT 400"):
        if re.search(r"trade\s*post", r["platform"] or "", re.I):
            continue
        if best_match([{"title": r["item"]}], search) or (want and len(want & core(r["item"])) >= max(2, len(want) - 1)):
            v = r["sale_price"] if r["sale_price"] else sales.money_in(dict(r))
            if v and v > 0:
                got.append(v)
    s = products.summarize(got[:20])
    if not s:
        raise products.SourceError("You haven't recorded a sale of this product yet. Use Sold on its order.")
    return {"price": s["median"], "low": s["low"], "high": s["high"], "count": s["count"],
            "how": f"what you sold it for ({s['count']} sale{'s' if s['count'] != 1 else ''})", "link": ""}


def after_sale(c, item, store="", price=None, set_market=False):
    """A sale was just recorded: add it to this product's Your sales line (no web lookups) and, when asked, make the
    sale price your market price. Creates the market row when the product has none yet."""
    k = key(item)
    if not k:
        return None
    ensure(c)
    ensure_rows(c, [{"key": k, "raw": item, "store": store or ""}])
    m = get(c, k)
    lookup(c, m, None, sources={"yours"})
    if set_market and price:
        m["market_manual"] = price
        value = market_of(m)
        m["history"] = [h for h in m["history"] if h[0] != date.today().isoformat()] + [[date.today().isoformat(), value]]
    save(c, m)
    return m


# --- Items and their market rows ----------------------------------------------------------------

def _load(row):
    d = dict(row)
    for f, empty in (("prices", {}), ("matched", {}), ("history", []), ("chart", []), ("errors", {}), ("images", [])):
        try:
            d[f] = json.loads(d.get(f) or "null") or empty
        except ValueError:
            d[f] = empty
    d["market"] = market_of(d)
    d["trend"] = monthly_rate(d)
    return d


def market_of(m):
    """Your typed market price, else the middle of the sources' prices."""
    if m.get("market_manual"):
        return m["market_manual"]
    vals = [p["price"] for p in (m.get("prices") or {}).values() if isinstance(p, dict) and p.get("price")]
    return round(statistics.median(vals), 2) if vals else None


def monthly_rate(m):
    """Price change per month from the last 6 months of history (PriceCharting's, else BotTrack's own lookups),
    as a fraction, capped at MAX_RATE. None when there's too little history."""
    pts = []
    for month, price in (m.get("chart") or [])[-7:]:
        try:
            y, mo = int(month[:4]), int(month[5:7])
        except (TypeError, ValueError):
            continue
        if price and price > 0:
            pts.append((y * 12 + mo, math.log(price)))
    if len(pts) < 3:
        pts = []
        for day, price in (m.get("history") or [])[-60:]:
            try:
                d = date.fromisoformat(day)
            except (TypeError, ValueError):
                continue
            if price and price > 0:
                pts.append((d.toordinal() / 30.44, math.log(price)))
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 0.45:  # under two weeks of lookups says nothing yet
            return None
    n = len(pts)
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    if not den:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / den
    return max(-MAX_RATE, min(MAX_RATE, math.exp(slope) - 1))


def get(c, k):
    ensure(c)
    r = c.execute("SELECT * FROM market_items WHERE key = ?", (k,)).fetchone()
    return _load(r) if r else None


def all_items(c):
    ensure(c)
    return {r["key"]: _load(r) for r in c.execute("SELECT * FROM market_items")}


def save(c, m):
    ensure(c)
    cols = ("raw", "store", "name", "search", "name_how", "image", "image_manual", "images", "prices", "matched",
            "history", "chart", "market_manual", "ignored", "errors", "updated_at")
    vals = [json.dumps(m.get(f)) if f in ("prices", "matched", "history", "chart", "errors", "images") else m.get(f)
            for f in cols]
    c.execute(f"INSERT INTO market_items (key, {', '.join(cols)}, created_at) VALUES (?, {', '.join('?' * len(cols))}, ?)"
              f" ON CONFLICT(key) DO UPDATE SET {', '.join(f'{f}=excluded.{f}' for f in cols)}",
              [m["key"], *vals, now()])
    c.commit()


def held(c):
    """Items you still have (or have coming): on orders not cancelled, refunded or moved out, less the units already sold.
    [{key, raw, store, order_id, qty, cost (each, or None), image, day, status}]"""
    out = []
    _sales_ready(c)
    # Each sale linked to an order uses up one unit of it. A sale that names another item of a multi-item order
    # can't be placed, so that order stays out entirely (as before).
    sold = {}
    for r in c.execute("SELECT order_id, item FROM sales WHERE order_id IS NOT NULL AND COALESCE(counted, 1) = 1"):
        sold.setdefault(r["order_id"], []).append(key(r["item"] or ""))
    for o in c.execute(
            "SELECT id, retailer, items, total, shipping_status, COALESCE(order_date, substr(last_email_date, 1, 10)) AS day"
            " FROM tracked_orders WHERE COALESCE(shipping_status, '') NOT IN (" + ",".join("?" * len(DONE)) + ")", DONE):
        try:
            items = [i for i in json.loads(o["items"] or "[]") if isinstance(i, dict) and (i.get("name") or "").strip()]
        except ValueError:
            continue
        mine = sold.get(o["id"], [])
        names = {key(i["name"]) for i in items}
        if mine and len(items) > 1 and any(k not in names for k in mine):
            continue
        for i in items:
            try:
                qty = max(1, int(float(i.get("quantity") or 1)))
            except (TypeError, ValueError):
                qty = 1
            each = products.money(i.get("price"))
            if each is None and len(items) == 1 and o["total"]:
                each = round(o["total"] / qty, 2)
            elif each is not None and qty > 1 and len(items) == 1 and o["total"] and abs(each - o["total"]) < 0.01:
                each = round(each / qty, 2)  # the line price was the whole line, not one item
            used = len(mine) if len(items) == 1 else sum(1 for k in mine if k == key(i["name"]))
            if used >= qty:
                continue
            out.append({"key": key(i["name"]), "raw": i["name"].strip(), "store": o["retailer"] or "",
                        "order_id": o["id"], "qty": qty - used, "cost": each, "image": i.get("image") or "",
                        "day": o["day"] or "", "status": o["shipping_status"] or ""})
    return [h for h in out if h["key"]]


def _sales_ready(c):
    from . import sales
    sales.ensure(c)


def lookup(c, m, llm=None, sources=None):
    """Names the product (unless it's been named or fixed) and reads every source. Changes m in place."""
    if not m.get("name") or (llm is not None and m.get("name_how") in (None, "", "as written")):
        m["name"], m["search"], m["name_how"] = identify(c, m["raw"], m.get("store"), llm)
    search = m.get("search") or cleanup(m["name"])
    prices, errors = dict(m.get("prices") or {}), {}
    images = list(m.get("images") or [])
    for src, label in SOURCES:
        if sources and src not in sources:
            continue
        if (prices.get(src) or {}).get("manual"):
            continue  # a price you typed stays until you clear it
        try:
            if src == "pricecharting":
                got = pricecharting(search)
                m["chart"] = got.pop("chart", []) or m.get("chart") or []
            elif src == "tcgplayer":
                got = tcgplayer(search)
            elif src == "ebay":
                got = ebay(search)
            elif src == "yours":
                got = yours(c, m["name"], search)
            else:
                got = tradepost(c, m["name"], search)
        except products.SourceError as e:
            errors[src] = str(e)
            if src in ("tradepost", "yours"):  # no such sales any more (one was unlinked): nothing to show
                prices.pop(src, None)
            logger.info("market: %s on %s: %s", search[:60], label, e)
            continue
        except Exception as e:  # an odd answer from one site never stops the others
            errors[src] = f"Couldn't read {label}: {e}"
            logger.warning("market: %s on %s failed: %s", search[:60], label, e)
            continue
        img = got.pop("image", "")
        title = got.pop("title", "")
        if img and img not in images:
            images.append(img)
        if title:
            m["matched"] = dict(m.get("matched") or {}, **{src: {"title": title, "link": got.get("link", "")}})
        prices[src] = dict(got, when=date.today().isoformat(), manual=False)
        logger.info("market: %s on %s: $%.2f (%s)", search[:60], label, got["price"], got["how"])
    m["prices"], m["errors"], m["images"] = prices, errors, images[:8]
    if not m.get("image_manual"):
        m["image"] = images[0] if images else m.get("image") or ""
    value = market_of(m)
    hist = [h for h in (m.get("history") or []) if h[0] != date.today().isoformat()]
    if value:
        hist.append([date.today().isoformat(), value])
    m["history"] = hist[-120:]
    m["updated_at"] = now()
    return m


def ensure_rows(c, items=None):
    """A market row for every item you hold; new ones get no prices until looked up."""
    have = {r[0] for r in c.execute("SELECT key FROM market_items")}
    for h in items if items is not None else held(c):
        if h["key"] not in have:
            save(c, {"key": h["key"], "raw": h["raw"], "store": h["store"], "name": "", "search": "", "name_how": "",
                     "image": "", "image_manual": 0, "images": [], "prices": {}, "matched": {}, "history": [],
                     "chart": [], "market_manual": None, "ignored": 0, "errors": {}, "updated_at": None})
            have.add(h["key"])


def stale(m):
    if not m.get("updated_at"):
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(m["updated_at"])
    except ValueError:
        return True
    return age.total_seconds() > STALE_HOURS * 3600


def refresh(db_path, llm=None, keys=None, limit=ROUND, stop=None):
    """Looks up held items (or just keys) one by one. Returns how many were looked up."""
    if not _run_lock.acquire(blocking=False):
        return 0
    stop = stop or (lambda: False)
    c = db.connect(db_path)
    try:
        ensure(c)
        _sales_ready(c)
        items = held(c)
        ensure_rows(c, items)
        rows = all_items(c)
        if keys:
            todo = [k for k in keys if k in rows]
        else:
            counts = {}
            for h in items:
                counts[h["key"]] = counts.get(h["key"], 0) + h["qty"]
            todo = sorted((k for k in counts if not rows[k]["ignored"] and stale(rows[k])),
                          key=lambda k: (rows[k].get("updated_at") is not None, -counts[k]))[:limit]
        state.update(running=True, done=0, total=len(todo), current="", finished=None)
        for k in todo:
            if stop():
                break
            m = rows[k]
            state["current"] = m.get("name") or m["raw"]
            try:
                save(c, lookup(c, m, llm))
            except Exception:
                logger.exception("market: looking up %r failed", m["raw"][:80])
            state["done"] += 1
        state["last"] = f"Looked up {state['done']} product{'s' if state['done'] != 1 else ''}."
        return state["done"]
    finally:
        try:
            from . import browsertrack
            browsertrack.release()
        except Exception:
            pass
        state.update(running=False, current="", finished=datetime.now().strftime("%-I:%M %p"))
        c.close()
        _run_lock.release()


def start(db_path, keys=None, limit=ROUND):
    """refresh() in the background, with the local AI when it's set up."""
    if state["running"]:
        return False
    try:
        from .config import Config
        from .llm import from_config
        llm = from_config(Config.from_env())
    except Exception:
        llm = None
    threading.Thread(target=refresh, args=(db_path, llm, keys, limit), daemon=True, name="market").start()
    return True


# --- Value, profit and projections -----------------------------------------------------------------

def projected(value, rate, months):
    return round(value * (1 + (rate or 0)) ** months, 2) if value is not None else None


def summary(c):
    """Everything the Market value page and the order sheets show: one row per product, plus the totals."""
    ensure(c)
    _sales_ready(c)
    items = held(c)
    ensure_rows(c, items)
    rows = all_items(c)
    from . import sales
    fee = sales.fee_pct(c, "eBay") or 0
    by = {}
    for h in items:
        m = rows.get(h["key"])
        r = by.setdefault(h["key"], {"m": m, "qty": 0, "cost": 0.0, "costed": 0, "orders": [], "image": ""})
        r["qty"] += h["qty"]
        if h["cost"] is not None:
            r["cost"] += h["cost"] * h["qty"]
            r["costed"] += h["qty"]
        r["orders"].append(h["order_id"])
        r["image"] = r["image"] or h["image"]
    out = []
    for k, r in by.items():
        m = r["m"]
        each = round(r["cost"] / r["costed"], 2) if r["costed"] else None
        market = m["market"]
        rate = m["trend"]
        row = {"key": k, "m": m, "name": m.get("name") or m["raw"], "raw": m["raw"], "qty": r["qty"],
               "orders": sorted(set(r["orders"]), reverse=True), "each": each, "cost": round(r["cost"], 2),
               "costed": r["costed"], "market": market, "image": (m.get("image") if m.get("image_manual") else "")
               or r["image"] or m.get("image") or "", "rate": rate, "ignored": bool(m.get("ignored"))}
        row["value"] = round(market * r["qty"], 2) if market is not None else None
        row["profit_each"] = round(market - each, 2) if market is not None and each is not None else None
        row["profit"] = round(row["profit_each"] * r["qty"], 2) if row["profit_each"] is not None else None
        row["after_fees"] = round((market * (1 - fee / 100) - each) * r["qty"], 2) \
            if market is not None and each is not None else None
        row["future"] = [projected(row["value"], rate, mo) for mo, _ in HORIZONS]
        out.append(row)
    live = [r for r in out if not r["ignored"]]
    priced = [r for r in live if r["market"] is not None]
    both = [r for r in priced if r["each"] is not None]
    cost_both = round(sum(r["each"] * r["qty"] for r in both), 2)
    tot = {"items": sum(r["qty"] for r in live), "products": len(live), "priced": len(priced),
           "unpriced": len(live) - len(priced), "cost": round(sum(r["cost"] for r in live), 2),
           "value": round(sum(r["value"] for r in priced), 2), "profit": round(sum(r["profit"] for r in both), 2),
           "after_fees": round(sum(r["after_fees"] for r in both), 2), "fee": fee, "cost_priced": cost_both,
           "losing": sum(1 for r in both if r["profit"] < 0), "rising": sum(1 for r in priced if (r["rate"] or 0) > 0.005),
           "falling": sum(1 for r in priced if (r["rate"] or 0) < -0.005), "ignored": len(out) - len(live),
           "future": []}
    for i, (mo, label) in enumerate(HORIZONS):
        val = round(sum(r["future"][i] for r in priced), 2)
        tot["future"].append({"label": label, "months": mo, "value": val,
                              "profit": round(sum(r["future"][i] - r["each"] * r["qty"] for r in both), 2)})
    tot["trended"] = sum(1 for r in priced if r["rate"] is not None)
    return out, tot


def orders_value(orders, rows):
    """(market value, potential profit) of these orders' items, leaving out cancelled and moved-out ones."""
    value = profit = 0.0
    for o in orders:
        if (o.get("shipping_status") or "") in DONE:
            continue
        got = for_order(None, o, rows)
        value += got["value"] or 0
        profit += got["profit"] or 0
    return round(value, 2), round(profit, 2)


def for_order(c, order, rows=None):
    """The order's items with their market rows, and the order's market value and potential profit."""
    items, value, profit, gotall = [], 0.0, 0.0, True
    rows = all_items(c) if rows is None else rows
    order_items = [i for i in order.get("items") or [] if isinstance(i, dict) and i.get("name")]
    for i in order_items:
        k = key(i["name"])
        m = rows.get(k)
        try:
            qty = max(1, int(float(i.get("quantity") or 1)))
        except (TypeError, ValueError):
            qty = 1
        each = products.money(i.get("price"))
        if each is None and len(order_items) == 1 and order.get("total"):
            each = round(order["total"] / qty, 2)
        market = m["market"] if m and not m.get("ignored") else None
        if market is None:
            gotall = False
        else:
            value += market * qty
            if each is not None:
                profit += (market - each) * qty
        items.append({"key": k, "raw": i["name"], "qty": qty, "each": each, "m": m, "market": market})
    priced = [i for i in items if i["market"] is not None]
    return {"items": items, "value": round(value, 2) if priced else None, "profit": round(profit, 2) if priced else None,
            "complete": gotall and bool(items), "sources": SOURCES}


def image_for(c, name):
    """A product picture found while looking up prices (or picked by you), or ""."""
    try:
        ensure(c)
        r = c.execute("SELECT image FROM market_items WHERE key = ?", (key(name),)).fetchone()
    except Exception:
        return ""
    return (r[0] or "") if r else ""


def chips(c):
    """{item key: market row} for the Orders table."""
    try:
        return all_items(c)
    except Exception:
        logger.exception("market: couldn't read market prices")
        return {}


# --- Pages -----------------------------------------------------------------------------------------

def _con():
    return db.connect(current_app.config.get("DB_PATH"))


def _back(anchor=""):
    ref = request.form.get("back") or ""
    if ref.startswith("/") and not ref.startswith("//"):
        return redirect(ref)
    return redirect(url_for("market.page", **({"show": request.form["show"]} if request.form.get("show") else {}))
                    + (f"#{anchor}" if anchor else ""))


SHOWS = {"all": "Every product you're holding", "priced": "Products with a market price",
         "unpriced": "Products still without a market price", "profit": "Products by potential profit",
         "losing": "Products worth less than you paid", "rising": "Products whose price is going up",
         "falling": "Products whose price is going down", "ignored": "Products you said you don't resell",
         "value": "Products by market value", "cost": "Products by what you paid"}


@bp.get("/market")
def page():
    c = _con()
    rows, tot = summary(c)
    show = request.args.get("show", "")
    keep = {"priced": lambda r: r["market"] is not None and not r["ignored"],
            "unpriced": lambda r: r["market"] is None and not r["ignored"],
            "losing": lambda r: r["profit"] is not None and r["profit"] < 0 and not r["ignored"],
            "rising": lambda r: (r["rate"] or 0) > 0.005 and not r["ignored"],
            "falling": lambda r: (r["rate"] or 0) < -0.005 and not r["ignored"],
            "ignored": lambda r: r["ignored"]}.get(show, lambda r: not r["ignored"])
    view = [r for r in rows if keep(r)]
    order = {"value": lambda r: -(r["value"] or 0), "cost": lambda r: -(r["cost"] or 0)}.get(
        show, lambda r: (r["profit"] is None, -(r["profit"] or 0), -(r["value"] or 0)))
    view.sort(key=order)
    one = request.args.get("key")
    return render_template("market.html", rows=view, tot=tot, show=show if show in SHOWS else "", shows=SHOWS,
                           state=state, sources=SOURCES, source_name=SOURCE_NAME, horizons=HORIZONS, open=one,
                           llm_task=name_task(c)["id"])


@bp.post("/market/update")
def update_route():
    keys = [k for k in request.form.getlist("key") if k]
    every = request.form.get("all") == "1"
    if keys:
        c = _con()
        for k in keys:  # looked up again now, even if it was looked up today
            c.execute("UPDATE market_items SET updated_at = NULL WHERE key = ?", (k,))
        c.commit()
    start(current_app.config.get("DB_PATH"), keys or None, limit=10_000 if every else ROUND)
    return _back(f"m-{keys[0]}" if len(keys) == 1 else "")


@bp.get("/market/state")
def state_route():
    return dict(state)


@bp.post("/market/<k>/name")
def name_route(k):
    """Fix the product name: the AI learns it, and prices and the picture are looked up again for it."""
    c = _con()
    m = get(c, k)
    name = " ".join((request.form.get("name") or "").split())[:200]
    if not m or not name:
        return _back(f"m-{k}")
    search = " ".join((request.form.get("search") or "").split())[:120] or cleanup(name)
    teach_name(c, m["raw"], m.get("store"), name, search)
    m.update(name=name, search=search, name_how="fixed", matched={}, chart=[], history=[], errors={}, images=[],
             prices={s: p for s, p in (m.get("prices") or {}).items() if p.get("manual")}, updated_at=None)
    if not m.get("image_manual"):
        m["image"] = ""
    save(c, m)
    logger.info("market: product name fixed by hand (%s)", name[:80])
    start(current_app.config.get("DB_PATH"), [k])
    return _back(f"m-{k}")


@bp.post("/market/<k>/price")
def price_route(k):
    """Type a price for one source (or your own market price); an empty box clears it."""
    c = _con()
    m = get(c, k)
    src = request.form.get("source", "")
    if not m or src not in SOURCE_NAME:
        return _back(f"m-{k}")
    vals = [products.money(x) for x in re.split(r"[,\s]+", request.form.get("price") or "") if products.money(x)]
    if src == "market":
        m["market_manual"] = vals[0] if vals else None
    elif vals:
        s = products.summarize(vals)
        m["prices"][src] = {"price": s["median"], "low": s["low"], "high": s["high"], "count": s["count"],
                            "how": "typed in", "link": "", "when": date.today().isoformat(), "manual": True}
    else:
        m["prices"].pop(src, None)
    value = market_of(m)
    m["history"] = [h for h in m["history"] if h[0] != date.today().isoformat()] + \
        ([[date.today().isoformat(), value]] if value else [])
    save(c, m)
    return _back(f"m-{k}")


@bp.post("/market/<k>/image")
def image_route(k):
    """Pick one of the pictures found, or paste a picture link; empty goes back to automatic."""
    c = _con()
    m = get(c, k)
    if m:
        url = (request.form.get("image_url") or request.form.get("image") or "").strip()
        if url and not re.match(r"^https://[^\s\"'<>]+$", url):
            return _back(f"m-{k}")
        m["image"], m["image_manual"] = (url, 1) if url else ((m.get("images") or [""])[0], 0)
        save(c, m)
    return _back(f"m-{k}")


@bp.post("/market/<k>/ignore")
def ignore_route(k):
    c = _con()
    m = get(c, k)
    if m:
        m["ignored"] = 0 if request.form.get("undo") else 1
        save(c, m)
    return _back(f"m-{k}")
