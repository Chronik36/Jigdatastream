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
           ("tradepost", "Tradepost"), ("site", "Your link")]
TYPED = [s for s in SOURCES if s[0] != "site"]  # sources you can type a price for
SOURCE_NAME = dict(SOURCES, market="Your price")
PICK_TASK = "Pick the market listing"

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


NEW_COLUMNS = ("links", "rejected", "options")  # your pinned listings, matches you said were wrong, what was found


def _setup(c):
    c.executescript(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(market_items)")}
    for col in NEW_COLUMNS:
        if col not in have:
            c.execute(f"ALTER TABLE market_items ADD COLUMN {col} TEXT")
    c.commit()


def ensure(con):
    return db.setup_once(con, "market", _setup)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key(name):
    """The same item however a store spells or spaces it: lower-case letters and numbers only."""
    return re.sub(r"[^a-z0-9]+", "", unescape(str(name or "")).lower().replace("é", "e"))[:160]


NUMS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "eight": "8", "nine": "9",
        "ten": "10", "single": "1", "triple": "3", "tri": "3"}


def words(text):
    t = unescape(str(text or "")).lower().replace("é", "e").replace("&", " and ")
    return [NUMS.get(w, w) for w in re.findall(r"[a-z0-9]+", t)]


# What kind of sealed product a title is. A 3-pack blister and a booster display share a set name but not a price,
# so a listing of another kind is never the match. Most specific first; checked on the words with numbers as digits.
KINDS = [
    ("ultra premium collection", r"\bultra premium collection\b|\bupc\b"),
    ("Pokemon Center elite trainer box", r"\bpokemon center\b.*\b(elite trainer box|etb)\b|\b(elite trainer box|etb)\b.*\bpokemon center\b"),
    ("elite trainer box", r"\belite trainer box\b|\betb\b"),
    ("booster bundle", r"\bbooster bundle\b"),
    ("build and battle", r"\bbuild (and )?battle\b"),
    ("half booster box", r"\bhalf booster box\b"),
    ("booster box", r"\bbooster (box|display)\b|\bdisplay box\b|\b(24|36) (booster )?packs?\b"),
    ("case", r"\bcase\b"),
    ("mini tin", r"\bmini tins?\b"),
    ("tin", r"\btins?\b"),
    ("collection", r"\bcollection\b|\bpremium\b|\bposter\b|\bbinder\b|\bsurprise box\b|\bgift box\b"),
    ("3-pack blister", r"\b3 (pack|booster|packs|boosters)\b|\btripack\b"),
    ("2-pack blister", r"\b2 (pack|booster|packs|boosters)\b"),
    ("checklane blister", r"\bcheck ?lane\b|\b1 (pack|booster) blister\b"),
    ("blister", r"\bblister\b"),
    ("booster pack", r"\bsleeved booster\b|\bbooster pack\b|\bbooster\b"),
]
BLISTERS = {"3-pack blister", "2-pack blister", "checklane blister", "blister"}


def kind_of(text):
    t = " ".join(words(text))
    return next((k for k, pat in KINDS if re.search(pat, t)), None)


def same_kind(a, b):
    """Could these be the same product? Unknown kinds can; a plain "blister" goes with any blister."""
    if not a or not b or a == b:
        return True
    return {a, b} <= BLISTERS and "blister" in (a, b)


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

def rank(candidates, want, hints="", skip=()):
    """[(score, candidate)] best first: titles with most of the wanted words, few extra, the same kind of product
    (never a booster box for a blister), no graded/lot/case words, and a bonus for words only the store's name had
    (a variant like Kangaskhan). Links in skip are matches you said were wrong."""
    want_core = core(want)
    if not want_core:
        return []
    kind = kind_of(want) or kind_of(hints)
    extra = core(hints) - want_core
    out = []
    for cand in candidates:
        if cand.get("link") and cand["link"] in skip:
            continue
        have = core(cand["title"])
        if not have or (have & BAD) - want_core - extra:
            continue
        ck = kind_of(cand["title"])
        if not same_kind(kind, ck):
            continue
        hit = len(want_core & have)
        if hit < max(1, math.ceil(0.6 * len(want_core))):
            continue
        score = hit / len(want_core | have)
        if kind and ck == kind:
            score += 0.2
        elif kind and ck:
            score += 0.1  # a blister of unknown size
        elif kind:
            score -= 0.1  # the listing doesn't say what it is
        score += 0.08 * len(extra & have)
        out.append((round(score, 4), cand))
    out.sort(key=lambda sc: -sc[0])
    return out


def best_match(candidates, want, hints="", skip=()):
    """The candidate closest to the wanted words (see rank), or None."""
    ranked = rank(candidates, want, hints, skip)
    return ranked[0][1] if ranked else None


def default_pick(src, candidates, search):
    return best_match(candidates, search)


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


def pricecharting(search, pick=default_pick):
    url = "https://www.pricecharting.com/search-products?" + f"q={quote_plus(search)}&type=prices"
    html = fetch(url, "price")
    if 'id="product_name"' in html or "VGPC.chart_data" in html:  # one match: the site went straight to it
        found = parse_pricecharting_product(html, url)
    else:
        pick = pick("pricecharting", parse_pricecharting_search(html), search)
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


def tcgplayer(search, pick=default_pick):
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
    return pick_tcgplayer(data, search, pick)


def pick_tcgplayer(data, search, pick=default_pick):
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
    pick = pick("tcgplayer", found, search)
    if not pick:
        raise products.SourceError(f"TCGplayer has nothing matching “{search}”.")
    return dict(pick, count=1, how="TCGplayer market price")


def ebay(search, hints=""):
    url = ("https://www.ebay.com/sch/i.html?_nkw=" + quote_plus(search) +
           "&LH_Sold=1&LH_Complete=1&LH_ItemCondition=1000&_ipg=120")
    prices = parse_ebay(fetch(url, "sold"), search, hints)
    s = products.summarize(prices)
    if not s:
        raise products.SourceError(f"No eBay sold listings matched “{search}”.")
    return {"price": s["median"], "low": s["low"], "high": s["high"], "count": s["count"],
            "how": f"middle of {s['count']} sold listing{'s' if s['count'] != 1 else ''}", "link": url}


def parse_ebay(html, search, hints=""):
    """Sold prices of listings that are this product (graded, lots, cases, empties and other kinds of product left
    out). No search: every listing counts (a search you picked yourself)."""
    prices = []
    for block in re.split(r"<li[^>]+class=\"[^\"]*\bs-(?:item|card)\b", html or "")[1:]:
        t = re.search(r'class="[^"]*s-(?:item|card)__title[^"]*"[^>]*>(.*?)</(?:div|span|h3)>', block, re.S)
        pr = re.search(r'class="[^"]*s-(?:item|card)__price[^"]*"[^>]*>(.*?)</span>', block, re.S)
        if not t or not pr:
            continue
        title, price_text = _text(t.group(1)), _text(pr.group(1))
        if "shop on ebay" in title.lower() or " to " in price_text:
            continue
        if not search or best_match([{"title": title}], search, hints):
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


# --- Your own listing links ------------------------------------------------------------------------

TCG_DETAILS = "https://mp-search-api.tcgplayer.com/v1/product/{}/details"
TCG_POINTS = "https://mpapi.tcgplayer.com/v2/product/{}/pricepoints"


def source_of(url):
    """Which source a pasted listing link belongs to: pricecharting, tcgplayer, ebay, or site (any other)."""
    host = (re.match(r"^https?://([^/?#]+)", url or "") or [None, ""])[1].lower()
    for src, domain in (("pricecharting", "pricecharting.com"), ("tcgplayer", "tcgplayer.com"), ("ebay", "ebay.")):
        if domain in host:
            return src
    return "site"


def _meta(html, *names):
    for n in names:
        m = re.search(r'<meta[^>]+(?:property|name|itemprop)="%s"[^>]+content="([^"]*)"' % re.escape(n), html or "", re.I) \
            or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name|itemprop)="%s"' % re.escape(n), html or "", re.I)
        if m and m.group(1).strip():
            return unescape(m.group(1).strip())
    return ""


def page_price(html):
    """(price, title, image) from any shop's product page: its price tags, else the first $ price on it."""
    price = products.money(_meta(html, "product:price:amount", "og:price:amount", "price"))
    if not price:
        m = re.search(r'itemprop="price"[^>]*content="([0-9][0-9.,]*)"', html or "") or \
            re.search(r'"price"\s*:\s*"?([0-9][0-9,]*(?:\.[0-9]+)?)', html or "")
        price = products.money(m.group(1)) if m else None
    if not price:
        body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
        price = _price(_text(body))
    title = _meta(html, "og:title", "twitter:title")
    if not title:
        t = re.search(r"<title>(.*?)</title>", html or "", re.S)
        title = _text(t.group(1)) if t else ""
    return price, title[:200], _meta(html, "og:image", "twitter:image")


def from_link(src, url):
    """The price from a listing you linked yourself (no searching, no guessing which result)."""
    if src == "pricecharting":
        found = parse_pricecharting_product(fetch(url, "ungraded"), url)
        if not found["price"]:
            raise products.SourceError("Your PriceCharting link had no ungraded price.")
        return {"price": found["price"], "count": 1, "how": "PriceCharting ungraded price, your link", "link": url,
                "title": found["title"], "image": found["image"], "chart": found["chart"]}
    if src == "tcgplayer":
        pid = re.search(r"/product/(\d+)", url)
        if not pid:
            raise products.SourceError("That TCGplayer link has no product number (it should look like "
                                       "tcgplayer.com/product/123456/...).")
        pid = pid.group(1)
        head = {"Origin": "https://www.tcgplayer.com"}
        price, title = None, ""
        try:
            d = products._get_json(TCG_DETAILS.format(pid), head) or {}
            d = d.get("results", [d])[0] if isinstance(d.get("results"), list) and d.get("results") else d
            price = products.money(d.get("marketPrice")) or products.money(d.get("lowestPrice"))
            title = f"{d.get('productName') or ''} {d.get('setName') or ''}".strip()
        except products.Blocked:
            raise
        except products.SourceError:
            pass
        if not price:
            pts = products._get_json(TCG_POINTS.format(pid), head) or []
            vals = [products.money(p.get("marketPrice")) for p in pts if isinstance(p, dict)]
            price = next((v for v in vals if v), None)
        if not price:
            raise products.SourceError("TCGplayer has no market price for your link yet.")
        return {"price": price, "count": 1, "how": "TCGplayer market price, your link", "title": title,
                "link": f"https://www.tcgplayer.com/product/{pid}",
                "image": f"https://tcgplayer-cdn.tcgplayer.com/product/{pid}_200w.jpg"}
    if src == "ebay" and "/sch/" in url:
        for flag in ("LH_Sold=1", "LH_Complete=1"):
            if flag.split("=")[0] not in url:
                url += ("&" if "?" in url else "?") + flag
        s = products.summarize(parse_ebay(fetch(url, "sold"), None))
        if not s:
            raise products.SourceError("Your eBay search showed no sold listings.")
        return {"price": s["median"], "low": s["low"], "high": s["high"], "count": s["count"],
                "how": f"middle of {s['count']} sold listing{'s' if s['count'] != 1 else ''} in your eBay search",
                "link": url}
    price, title, image = page_price(fetch(url, "$"))
    if not price:
        raise products.SourceError(f"Couldn't find a price on {url.split('/')[2]}. Type the price in instead.")
    return {"price": price, "count": 1, "how": f"price on your {url.split('/')[2]} link", "link": url,
            "title": title, "image": image if image.startswith("https://") else ""}


# --- Picking the right listing (rules first, then the AI, which learns from your picks) --------------------

def pick_task(c):
    """The Training page task that picks which search result is the product (made the first time it's needed)."""
    row = c.execute("SELECT * FROM ai_tasks WHERE name = ?", (PICK_TASK,)).fetchone()
    if row:
        return dict(row)
    c.execute("INSERT INTO ai_tasks (name, instructions, answer, created_at) VALUES (?,?,?,?)",
              (PICK_TASK, "You get an item from a store's order email (as the email wrote it, and the product it was "
               "named as) and numbered listings from a price site. Say which listing is exactly that product: the "
               "same set and the same kind of product and size (a 3-pack blister is not a booster box, a booster "
               "bundle is not an Elite Trainer Box, a Pokemon Center ETB is not a regular ETB), and the same "
               "variant when the email names one. If none is that product, pick 0. Reply {\"pick\": number}.",
               "json", tasks.now()))
    c.commit()
    return pick_task(c)


def _pick_input(m, search, listings):
    return json.dumps({"item": m.get("raw") or "", "named": m.get("name") or "", "search": search,
                       "listings": [{"n": i + 1, "title": x["title"]} for i, x in enumerate(listings)]},
                      ensure_ascii=False)


def ai_pick(c, llm, m, search, listings):
    """The listing the local AI picks, None when it says none fit, or False when it couldn't answer."""
    try:
        got = json.loads(tasks.answer(llm, c, pick_task(c), _pick_input(m, search, listings)))
        n = int(got.get("pick")) if isinstance(got, dict) else -1
    except (LLMError, ValueError, TypeError) as e:
        logger.info("market: the local AI couldn't pick a listing (%s)", e)
        return False
    if n == 0:
        return None
    return listings[n - 1] if 1 <= n <= len(listings) else False


def teach_pick(c, m, search, listings, chosen):
    """Your pick (or "none of these", chosen None) becomes an example for the listing picker."""
    n = next((i + 1 for i, x in enumerate(listings) if chosen and x.get("link") == chosen.get("link")), 0)
    tasks.add_examples(c, pick_task(c)["id"], [(_pick_input(m, search, listings), json.dumps({"pick": n}))])


def picker(c, m, llm, options):
    """The pick function the sources use for this item: skips matches you said were wrong, keeps what was found
    (so you can pick another), and asks the local AI when two results are close."""
    hints = " ".join(x for x in (m.get("raw"), m.get("name")) if x)
    rejected = m.get("rejected") or {}

    def pick(src, candidates, search):
        skip = set(rejected.get(src) or [])
        ranked = rank(candidates, search, hints, skip)
        seen = {id(cand) for _, cand in ranked}
        shown = [cand for _, cand in ranked] + [x for x in candidates if id(x) not in seen and x.get("link") not in skip]
        options[src] = [{k: x.get(k) for k in ("title", "link", "price", "image")} for x in shown[:8]]
        if not ranked:
            return None
        if llm is not None and len(ranked) > 1 and ranked[1][0] >= ranked[0][0] - 0.15:
            close = [cand for _, cand in ranked[:6]]
            chosen = ai_pick(c, llm, m, search, close)
            if chosen is None:
                return None
            if chosen:
                return chosen
        return ranked[0][1]
    return pick


def shared_links(c, m):
    """Links you pinned on another row for the same product (another store's spelling of it)."""
    name = (m.get("name") or "").strip().lower()
    if not name:
        return {}
    out = {}
    for r in c.execute("SELECT links FROM market_items WHERE lower(name) = ? AND key != ? AND links IS NOT NULL"
                       " AND links NOT IN ('', '{}', 'null')", (name, m["key"])):
        try:
            for src, url in (json.loads(r[0]) or {}).items():
                out.setdefault(src, url)
        except ValueError:
            continue
    return out


# --- Items and their market rows ----------------------------------------------------------------

def _load(row):
    d = dict(row)
    for f, empty in (("prices", {}), ("matched", {}), ("history", []), ("chart", []), ("errors", {}), ("images", []),
                     ("links", {}), ("rejected", {}), ("options", {})):
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
            "history", "chart", "market_manual", "ignored", "errors", "updated_at", "links", "rejected", "options")
    js = ("prices", "matched", "history", "chart", "errors", "images", "links", "rejected", "options")
    vals = [json.dumps({} if f in NEW_COLUMNS and m.get(f) is None else m.get(f)) if f in js else m.get(f)
            for f in cols]
    c.execute(f"INSERT INTO market_items (key, {', '.join(cols)}, created_at) VALUES (?, {', '.join('?' * len(cols))}, ?)"
              f" ON CONFLICT(key) DO UPDATE SET {', '.join(f'{f}=excluded.{f}' for f in cols)}",
              [m["key"], *vals, now()])
    c.commit()


def held(c):
    """Items you still have (or have coming): on orders not cancelled, refunded, moved out or sold.
    [{key, raw, store, order_id, qty, cost (each, or None), image, day, status}]"""
    out = []
    for o in c.execute(
            "SELECT id, retailer, items, total, shipping_status, COALESCE(order_date, substr(last_email_date, 1, 10)) AS day"
            " FROM tracked_orders WHERE COALESCE(shipping_status, '') NOT IN (" + ",".join("?" * len(DONE)) + ")"
            " AND id NOT IN (SELECT order_id FROM sales WHERE order_id IS NOT NULL)", DONE):
        try:
            items = [i for i in json.loads(o["items"] or "[]") if isinstance(i, dict) and (i.get("name") or "").strip()]
        except ValueError:
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
            out.append({"key": key(i["name"]), "raw": i["name"].strip(), "store": o["retailer"] or "",
                        "order_id": o["id"], "qty": qty, "cost": each, "image": i.get("image") or "",
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
    links = dict(shared_links(c, m), **(m.get("links") or {}))
    options = {}
    pick = picker(c, m, llm, options)
    hints = " ".join(x for x in (m.get("raw"), m.get("name")) if x)
    for src, label in SOURCES:
        if sources and src not in sources:
            continue
        if (prices.get(src) or {}).get("manual"):
            continue  # a price you typed stays until you clear it
        if src == "site" and not links.get(src):
            prices.pop(src, None)
            continue
        if src == "ebay" and not links.get(src) and "*" in ((m.get("rejected") or {}).get("ebay") or []):
            prices.pop(src, None)
            errors[src] = "You said eBay's listings were the wrong product. Paste an eBay sold search to use."
            continue
        try:
            if links.get(src):
                got = from_link(src, links[src])
                if src == "pricecharting":
                    m["chart"] = got.pop("chart", []) or m.get("chart") or []
            elif src == "pricecharting":
                got = pricecharting(search, pick)
                m["chart"] = got.pop("chart", []) or m.get("chart") or []
            elif src == "tcgplayer":
                got = tcgplayer(search, pick)
            elif src == "ebay":
                got = ebay(search, hints)
            else:
                got = tradepost(c, m["name"], search)
        except products.SourceError as e:
            errors[src] = str(e)
            if src == "tradepost" or (src in ("pricecharting", "tcgplayer") and not links.get(src)
                                      and "nothing matching" in str(e)):
                prices.pop(src, None)  # no longer matches (a wrong match was turned down): nothing to show
                m["matched"] = {k: v for k, v in (m.get("matched") or {}).items() if k != src}
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
    m["options"] = dict(m.get("options") or {}, **options)
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
        r = by.setdefault(h["key"], {"m": m, "qty": 0, "cost": 0.0, "costed": 0, "orders": [], "image": "", "held": []})
        r["held"].append({k: h[k] for k in ("order_id", "store", "day", "qty", "cost", "status")})
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
               "costed": r["costed"], "market": market, "kind": kind_of(m.get("search") or m.get("name") or m["raw"])
               or kind_of(m["raw"]), "held": sorted(r["held"], key=lambda x: x["day"], reverse=True), "image": (m.get("image") if m.get("image_manual") else "")
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
        kind = kind_of((m or {}).get("search") or (m or {}).get("name") or i["name"]) or kind_of(i["name"])
        items.append({"key": k, "raw": i["name"], "qty": qty, "each": each, "m": m, "market": market, "kind": kind})
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
    from . import inbox
    return render_template("market.html", rows=view, tot=tot, show=show if show in SHOWS else "", shows=SHOWS,
                           state=state, sources=SOURCES, typed=TYPED,
                           not_order=[(k, inbox.LABELS[k][0]) for k in inbox.NOT_ORDER], source_name=SOURCE_NAME, horizons=HORIZONS, open=one,
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


def _clear_source(m, src):
    """Forget what a source found (not a price you typed), so it's looked up fresh."""
    if not (m["prices"].get(src) or {}).get("manual"):
        m["prices"].pop(src, None)
    m["matched"] = {k: v for k, v in (m.get("matched") or {}).items() if k != src}
    m["errors"] = {k: v for k, v in (m.get("errors") or {}).items() if k != src}
    m["updated_at"] = None


@bp.post("/market/<k>/link")
def link_route(k):
    """Use this listing for this product: one picked from what was found, or a link you pasted from PriceCharting,
    TCGplayer, an eBay sold search or any shop. Picks teach the AI's listing picker. An empty link unpins."""
    c = _con()
    m = get(c, k)
    url = (request.form.get("url") or request.form.get("pick") or "").strip()
    src = request.form.get("source") or ""
    if not m:
        return _back(f"m-{k}")
    if not url:  # unpin
        if src in m["links"]:
            m["links"].pop(src)
            _clear_source(m, src)
            save(c, m)
            start(current_app.config.get("DB_PATH"), [k])
        return _back(f"m-{k}")
    if not re.match(r"^https?://[^\s\"'<>]+$", url):
        return _back(f"m-{k}")
    src = source_of(url) if not src or src not in SOURCE_NAME or source_of(url) != src else src
    found = m["options"].get(src) or []
    chosen = next((x for x in found if x.get("link") == url), None)
    if chosen and len(found) > 1:
        teach_pick(c, m, m.get("search") or cleanup(m.get("name") or m["raw"]), found, chosen)
        logger.info("market: the AI learned which %s listing is %s", SOURCE_NAME[src], (m.get("name") or "")[:60])
    m["links"][src] = url
    m["rejected"][src] = [x for x in m["rejected"].get(src) or [] if x not in (url, "*")]
    _clear_source(m, src)
    save(c, m)
    start(current_app.config.get("DB_PATH"), [k])
    return _back(f"m-{k}")


@bp.post("/market/<k>/wrong")
def wrong_route(k):
    """That source matched the wrong product: it's never used for this item again, the AI's listing picker learns
    it isn't the product, and the source is looked up again (the next best match, or none)."""
    c = _con()
    m = get(c, k)
    src = request.form.get("source") or ""
    if not m or src not in SOURCE_NAME:
        return _back(f"m-{k}")
    bad = (m["matched"].get(src) or {}).get("link") or (m["prices"].get(src) or {}).get("link") or ""
    if bad and src in ("pricecharting", "tcgplayer", "site"):
        m["rejected"][src] = sorted(set(m["rejected"].get(src) or []) | {bad})
        title = (m["matched"].get(src) or {}).get("title") or ""
        if title:
            teach_pick(c, m, m.get("search") or cleanup(m.get("name") or m["raw"]), [{"title": title, "link": bad}], None)
    if m["links"].get(src) == bad or src in ("ebay", "site"):
        m["links"].pop(src, None)
    m["options"][src] = [x for x in m["options"].get(src) or [] if x.get("link") != bad]
    if (m["prices"].get(src) or {}).get("manual"):
        m["prices"].pop(src)
    _clear_source(m, src)
    if src == "ebay":  # eBay's price is many listings: it stays off until you link a search or type a price
        m["errors"]["ebay"] = "You said eBay's listings were the wrong product. Paste an eBay sold search to use."
        m["rejected"]["ebay"] = ["*"]
    save(c, m)
    start(current_app.config.get("DB_PATH"), [k])
    return _back(f"m-{k}")


@bp.post("/market/<k>/not-order")
def not_order_route(k):
    """Orders holding this item that aren't orders (an ad, a newsletter...): they leave Orders, and the AI re-reads
    their emails as what you said and learns from it. One order, or every order with this item."""
    from . import inbox
    from .orderstore import Store
    from .ordersheet import move_out
    from flask import flash
    c = _con()
    store = Store(c)
    label = request.form.get("label") or "other"
    label = label if label in inbox.NOT_ORDER else "other"
    if request.form.get("all") == "1":
        ids = sorted({h["order_id"] for h in held(c) if h["key"] == k})
    else:
        ids = [int(x) for x in request.form.getlist("order_id") if str(x).isdigit()]
    said = ""
    for oid in ids:
        o = store.order(oid)
        if o is not None:
            said = move_out(store, o, "not_an_order", label)
    if ids:
        flash((f"Removed {len(ids)} orders: not orders. " if len(ids) > 1 else "") + said)
    return _back(f"m-{k}")
