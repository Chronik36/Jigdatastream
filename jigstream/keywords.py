"""Keyword finder: fetch one web page and list the words and phrases it uses.

The page is fetched once with a normal request. If the site refuses (403, 429, a bot check),
that is reported as-is; nothing here tries to get around it.
"""

import copy
import json
import logging
import re
import secrets
import threading
from collections import OrderedDict
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

from flask import Blueprint, current_app, jsonify, redirect, render_template, request

from . import db, keywordrules, tasks
from .config import Config
from .llm import LLMClient, LLMError

logger = logging.getLogger("jigstream.keywords")
bp = Blueprint("keywords", __name__)

MAX_BYTES = 3_000_000
TIMEOUT = 20
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

STOPWORDS = set("""
a about above after again against all also am an and any are aren't as at be because been before being below between both
but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each few for from further get got
had hadn't has hasn't have haven't having he her here hers herself him himself his how i if in into is isn't it it's its
itself just let's me more most my myself no nor not now of off on once only or other our ours ourselves out over own same
she should shouldn't so some such than that that's the their theirs them themselves then there these they this those
through to too under until up us very was wasn't we were weren't what when where which while who whom why will with won't
would you your yours yourself yourselves one two may might must shall via per etc new use used using within without also
www http https com html amp nbsp quot click here more less see view page site skip content menu close open toggle
sorry stock item items sold cart bag basket add quick shop price prices sale regular unit available unavailable notify
choose option options select quantity qty review reviews rating ratings expand collapse child translation missing icon
icons free shipping order orders checkout account login wishlist share tweet pin pinterest facebook instagram email
""".split())

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "select", "button", "form"}
# Site chrome: menus, footers, drawers. Their text is the same on every page, so it says nothing about the product.
CHROME_TAGS = {"nav", "footer", "aside", "dialog"}
CHROME_NAMES = re.compile(r"(?:^|[\s_-])(?:nav|navbar|navigation|menu|menus|megamenu|footer|drawer|breadcrumbs?|"
                          r"announcement|newsletter|cookie|popup|modal|predictive-search|search-results)(?:$|[\s_-])", re.I)
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
HEADINGS = {"h1", "h2", "h3"}
WORD = re.compile(r"[a-z0-9][a-z0-9'’&+.-]*[a-z0-9]|[a-z0-9]", re.I)
# Product codes: "DZ5485-612", "FQ8232 100", "1203A243-020", or what follows "SKU:" / "Style:".
CODE = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{4,}-[A-Z0-9]{2,4}\b")
LABELLED = re.compile(r"\b(?:SKU|Style(?:\s*(?:code|#|no\.?))?|Model(?:\s*(?:#|no\.?))?|Item\s*(?:#|no\.?)|MPN|UPC)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,24})", re.I)
# Shop wording that ends up inside product links and cards: stock notices, prices, buttons.
SHOP_NOISE = re.compile(r"sorry,?\s+this item is (?:currently )?(?:out of stock|unavailable)\.?|\b(?:sold out|out of stock|in stock|"
                        r"add to (?:cart|bag|basket)|quick (?:view|shop|add)|choose options|notify me|pre-?order|"
                        r"(?:regular|sale|unit) price|translation missing[^ ]*)\b|[$£€]\s?\d[\d,.]*", re.I)


class FetchError(Exception):
    pass


def clean_item_text(text):
    """A link or card's text reduced to the item name: no stock notices or prices, no repeated name."""
    text = " ".join(SHOP_NOISE.sub(" ", text).split()).strip(" -|/:,")
    half = len(text) // 2
    if half and text[:half].strip() == text[half:].strip():  # name shown twice (visible + screen-reader copy)
        text = text[:half].strip()
    return text


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.headings, self.alts, self.text, self.ld = "", {}, [], [], [], []
        self._stack = []  # (tag, hides its text)
        self._in_title = self._in_ld = self._title_done = False
        self._heading = None
        self.links, self._link = [], None  # link texts: on a product page these are mostly other products

    @property
    def _skip(self):
        return any(hidden for _, hidden in self._stack)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        names = " ".join((a.get("class", ""), a.get("id", ""), a.get("role", "")))
        if tag == "title" and not self._skip and not self._title_done:
            self._in_title = True  # the page title; <title> inside icons doesn't count
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "keywords", "og:title", "og:description", "twitter:title", "og:site_name",
                       "product:brand", "product:retailer_item_id") and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld = True
        elif tag == "img" and a.get("alt", "").strip() and not self._skip:
            self.alts.append(a["alt"].strip())
        if tag in VOID_TAGS:
            return
        hidden = (tag in SKIP_TAGS or tag in CHROME_TAGS or bool(CHROME_NAMES.search(names))
                  or "hidden" in a or a.get("aria-hidden") == "true" or a.get("role") == "navigation"
                  or re.search(r"display\s*:\s*none", a.get("style", "")) is not None
                  or (tag == "header" and not any(t in ("main", "article") for t, _ in self._stack)))
        self._stack.append((tag, hidden))
        if tag in HEADINGS and not self._skip:
            self._heading = [tag, []]
        if tag == "a" and not self._skip:
            self._link = [a.get("title", "")]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title, self._title_done = False, True
        elif tag == "script":
            self._in_ld = False
        if tag in HEADINGS and self._heading and self._heading[0] == tag:
            text = " ".join(" ".join(self._heading[1]).split())
            if text:
                self.headings.append((tag, text))
            self._heading = None
        if tag == "a" and self._link is not None:
            text = clean_item_text(" ".join(" ".join(self._link).split()))
            if 2 <= len(text) <= 160:
                self.links.append(text)
            self._link = None
        if any(t == tag for t, _ in self._stack):  # close it, and anything left open inside it
            while self._stack and self._stack.pop()[0] != tag:
                pass

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_ld:
            self.ld.append(data)
            return
        if self._skip:
            return
        if self._heading:
            self._heading[1].append(data)
        if self._link is not None:
            self._link.append(data)
        if data.strip():
            self.text.append(data.strip())


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        raise FetchError("Paste a link first.")
    if not re.match(r"https?://", url, re.I):
        if "://" in url or re.match(r"[a-z][\w+.-]*:(?!\d)", url, re.I):  # ftp:, javascript:, file:...
            raise FetchError("Only http and https links can be checked.")
        url = "https://" + url
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise FetchError("That doesn't look like a web address. It should start with https://")
    return url


def fetch(url):
    """Returns (final url, html text). Raises FetchError with a message for the page."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            kind = resp.headers.get_content_type()
            if kind not in ("text/html", "application/xhtml+xml", "text/plain"):
                raise FetchError(f"That link is a {kind} file, not a web page.")
            raw = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.geturl(), raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429, 503):
            raise FetchError(f"The site refused the request (HTTP {e.code}). Some stores block automated "
                             "requests; try the page in your browser, or a different page from the same site.") from e
        raise FetchError(f"The site answered HTTP {e.code} ({e.reason}).") from e
    except urllib.error.URLError as e:
        raise FetchError(f"Couldn't reach that site: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise FetchError(f"Couldn't load that page: {e}") from e


def _ld_products(blobs):
    """Name, brand, SKU and similar fields from schema.org JSON-LD blocks."""
    found, others = {}, []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(k in ("Product", "ProductGroup", "IndividualProduct") for k in kinds):
                for key in ("name", "sku", "mpn", "gtin", "gtin12", "gtin13", "productID", "color", "model"):
                    if isinstance(node.get(key), (str, int)) and str(node[key]).strip():
                        found.setdefault(key, str(node[key]).strip())
                brand = node.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                if isinstance(brand, str) and brand.strip():
                    found.setdefault("brand", brand.strip())
                other = node.get("name").strip() if isinstance(node.get("name"), str) else ""
                # Variants of the same product ("Pig - Default Title", "Pig - Red") are options, not look-alikes.
                if other and not other.startswith(found.get("name") or "\0"):
                    others.append(other)
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for blob in blobs:
        try:
            walk(json.loads(blob))
        except ValueError:
            continue
    return found, others


def fold(text):
    """"Pokémon" -> "pokemon": accents off, lowercase."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def words(text):
    return [w for w in (t.strip(".'-") for t in WORD.findall(fold(text).replace("’", "'"))) if w]


def _meaningful(w):
    if re.fullmatch(r"[\d.,-]+", w):
        return w.isdigit() and len(w) >= 4  # years and model numbers, not prices or sizes
    return len(w) >= 3 and w not in STOPWORDS


def count_keywords(text, top=40):
    tokens = words(text)
    single = Counter(w for w in tokens if _meaningful(w))
    phrases = Counter()
    for n in (2, 3):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i:i + n]
            # A phrase may hold a stopword in the middle ("air force 1", "made in usa") but not at the ends.
            if _meaningful(gram[0]) and _meaningful(gram[-1]) and len(set(gram)) == n:
                phrases[" ".join(gram)] += 1
    total = sum(single.values()) or 1
    return ([(w, c, round(100 * c / total, 1)) for w, c in single.most_common(top)],
            [(p, c) for p, c in phrases.most_common(top * 2) if c >= 2][:top], len(tokens))


def product_codes(text, ld):
    codes = []
    for key in ("sku", "mpn", "model", "productID", "gtin", "gtin12", "gtin13"):
        if ld.get(key):
            codes.append(ld[key])
    codes += [c for c in LABELLED.findall(text) if any(ch.isdigit() for ch in c)]  # not "Model Kits"
    codes += CODE.findall(text)
    seen, out = set(), []
    for c in codes:
        if c.upper() not in seen:
            seen.add(c.upper())
            out.append(c)
    return out[:15]


# Words that usually mean a different version of the item: other sizing runs, genders, conditions,
# accessories that share the product's name. Suggested as negatives when the target doesn't use them.
VARIANT_TERMS = {
    "gs": "grade school (big kids) sizing", "ps": "preschool sizing", "td": "toddler sizing",
    "kids": "kids' version", "kid": "kids' version", "youth": "kids' version", "toddler": "toddler version",
    "preschool": "preschool version", "infant": "baby version", "baby": "baby version", "crib": "baby version",
    "womens": "women's version", "women's": "women's version", "wmns": "women's version", "mens": "men's version",
    "men's": "men's version", "low": "different cut", "mid": "different cut", "high": "different cut",
    "slide": "a slide, not the shoe", "slides": "a slide, not the shoe", "sandal": "a sandal", "socks": "socks",
    "sock": "socks", "tee": "apparel", "shirt": "apparel", "t-shirt": "apparel", "hoodie": "apparel",
    "shorts": "apparel", "pants": "apparel", "jacket": "apparel", "hat": "apparel", "cap": "apparel",
    "keychain": "accessory", "laces": "accessory", "charm": "accessory", "bag": "accessory", "backpack": "accessory",
    "case": "accessory", "cover": "accessory", "skin": "accessory", "protector": "accessory", "sticker": "accessory",
    "poster": "accessory", "pin": "accessory", "used": "not new", "pre-owned": "not new", "refurbished": "not new",
    "renewed": "not new", "replica": "not genuine", "custom": "customised, not retail", "digital": "digital edition",
    "bundle": "bundle, not the item alone", "gift": "gift card or gift set", "sample": "sample",
}
# Trading cards and toys: the same set or line comes as many different boxes. Kept apart from VARIANT_TERMS
# (Products uses that to reject resale listings, where "pack" or "set" is normal).
PRODUCT_TYPES = {
    "etb": "a different box (Elite Trainer Box)", "elite": "a different box (Elite Trainer Box)",
    "booster": "a different box or pack", "pack": "a different box or pack", "packs": "a different box or pack",
    "display": "a whole display/case, not one box", "tin": "a tin, not this box", "tins": "a tin, not this box",
    "blister": "a blister pack", "collection": "a collection box", "premium": "a premium version",
    "center": "the Pokémon Center exclusive version", "exclusive": "an exclusive version", "sleeved": "a single sleeved pack",
    "binder": "an accessory", "sleeves": "an accessory", "plush": "a plush toy", "figure": "a figure",
    "mini": "a smaller version", "jumbo": "a bigger version", "mega": "a bigger version", "set": "a set of several",
}
VERSION_WORDS = {**VARIANT_TERMS, **PRODUCT_TYPES}
# Always worth excluding unless the product itself is one of these.
CATCH_ALLS = ["gs", "ps", "td", "used", "replica"]


def strip_site(name):
    """"Air Jordan 1 Low | Nike.com" -> "Air Jordan 1 Low"."""
    return re.split(r"\s+[|–—]\s+|\s+[-:]\s+(?=[^-:]*$)", unescape(name or "").strip())[0].strip()


def name_variants(p, ld):
    """Every way the page names its own product: page data, og:title, H1 and title tag."""
    return [n for n in (strip_site(x) for x in (ld.get("name"), p.meta.get("og:title"),
                                                  next((h for t, h in p.headings if t == "h1"), ""), p.title)) if n]


def target_name(p, ld):
    names = name_variants(p, ld)
    return names[0] if names else ""


def site_words(p, url):
    """The store's own name ("ToyTime", "gotoytime"), which shows up next to every item it sells."""
    out = set(words(p.meta.get("og:site_name", "")))
    for title in (p.title, p.meta.get("og:title", "")):
        parts = re.split(r"\s+[|–—]\s+|\s+[-:]\s+(?=[^-:]*$)", unescape(title or "").strip())
        if len(parts) > 1:
            out |= set(words(parts[-1]))
    host = urlparse(url).netloc.lower().split(":")[0]
    labels = [x for x in host.split(".") if x not in ("www", "shop", "store", "com", "net", "org", "co", "uk", "us", "ca")]
    return out, labels


def with_joins(tokens):
    """Tokens plus each adjacent pair written together, so "nee doh" also counts as "needoh"."""
    return set(tokens) | {a + b for a, b in zip(tokens, tokens[1:])}


# Name words that say what kind of thing it is but not which one; a monitor gains nothing from them.
GENERIC = {"shoe", "shoes", "sneaker", "sneakers", "size", "sizes", "color", "colour", "product", "item", "official"}


def similar_items(name, related):
    """Other items on the page whose names share most of the target's main words."""
    core = {w for w in words(name) if _meaningful(w)}
    out = []
    for text in dict.fromkeys(related):
        toks = with_joins(words(text))
        if text.strip().lower() != name.lower() and len(core & toks) >= max(1, min(2, len(core) // 2)):
            out.append((text, toks))
    return out


def spaced_form(word, text):
    """How the page writes `word` with a space in it ("Nee Doh" for needoh), or ""."""
    for i in range(2, len(word) - 1):
        m = re.search(r"\b" + re.escape(word[:i]) + r"[\s-]+" + re.escape(word[i:]) + r"\b", text, re.I)
        if m:
            return m.group(0)
    return ""


# Short forms stores use for a phrase: a negative is never the target's own phrase shortened.
ABBREVIATIONS = {"tcg": {"trading", "card", "game"}, "etb": {"elite", "trainer", "box"}, "wmns": {"womens"},
                 "sv": {"scarlet", "violet"}, "og": {"original"}, "ltd": {"limited"}}


def name_segments(name):
    """"Pokémon TCG: Scarlet & Violet—Prismatic Evolutions ETB" -> the series parts, then the item itself."""
    parts = [x for x in re.split(r"\s*[:—–|]\s*|\s+-\s+", name) if words(x)]
    return parts or [name]


def positive_keywords(name, similar, brand="", codes=(), top=6, page_text="", skip=()):
    """[(word, reason, in_line)]: words a monitor should require. in_line=False ones are shown but not put
    in the +/- line (a style code only matches where the site lists it).

    Every + word has to be in a listing's title, and stores word the same item differently, so fewer,
    sharper words beat the whole name: the item's own words come first, the series/brand prefix
    ("Pokémon Trading Card Game: Scarlet & Violet") only fills spare room."""
    brand_words = set(words(brand))
    segments = name_segments(name)
    specific = set(words(segments[-1]))
    raw = name.lower()
    picked = {}
    for i, w in enumerate(dict.fromkeys(words(name))):
        if w in GENERIC or w in skip or not (_meaningful(w) or w.isdigit() or w in VERSION_WORDS):
            continue
        shared = sum(w in toks for _, toks in similar)
        spaced = spaced_form(w, page_text) if len(w) >= 5 and not w.isdigit() else ""
        if spaced:
            # Monitors match text inside the title, so pick the part both spellings contain:
            # "doh" is in "NeeDoh" and in "Nee Doh"; "needoh" would miss every "Nee Doh" item.
            part = max(reversed([t for t in words(spaced) if t not in STOPWORDS]), key=len, default="")
            if len(part) >= 3:
                picked.setdefault(part, (0, i, f"product line written both “{w}” and “{spaced}”; "
                                               f"+{part} matches both spellings", True))
            continue
        if w.isdigit():
            rank, reason = 0, "model number"
        elif w in VERSION_WORDS:
            rank, reason = 0, "says which version this is, so other versions don't match"
        elif similar and not shared:
            rank, reason = 0, "only this item has it, the best word for telling it apart"
        elif w in brand_words:
            rank, reason = 1, "brand"
        elif similar and shared == len(similar):
            rank, reason = 1, "shared with every similar item, so it needs the other words too"
        else:
            rank, reason = 1, "in the product name"
        if w not in specific and not w.isdigit():
            rank, reason = 2, reason + " (series name, so it's only used when the item's own name is too short)"
        in_line = w in raw or not w.isascii()
        if not in_line:  # the site writes it with an accent ("Pokémon")
            reason = "the site writes it with an accent, which many monitors won't match to the plain spelling"
        picked.setdefault(w, (rank, i, reason, in_line))
    best = sorted(picked.items(), key=lambda kv: (not kv[1][3], kv[1][0], kv[1][1]))[:top]
    if sum(rank < 2 for _, (rank, *_) in best) >= 3:  # the item's own words are enough on their own
        best = [kv for kv in best if kv[1][0] < 2]
    out = [(w, reason, in_line) for w, (_, _, reason, in_line) in sorted(best, key=lambda kv: kv[1][1])]
    for code in list(codes)[:1]:
        out.append((code.lower(), "style code / SKU, an exact match where the site shows it", False))
    return out


def negative_keywords(name, related, top=20, similar=None, also=(), site=(), host_labels=()):
    """Words that show up on similar items but not in the target's name, most telling first.

    related: names of other items on the page (links, image alts, other products in the page data).
    also: other ways the page names the target (H1, title), whose words are never negatives.
    site / host_labels: the store's name and domain, which appear next to every item.
    Returns [(negative word, reason)].
    """
    target = set(words(name))
    for other in also:
        target |= set(words(other))
    joined_target = target | with_joins(words(name))

    def is_store(w):
        return w in site or any(len(w) >= 4 and w in label for label in host_labels)

    seen, similar_name = Counter(), {}
    for text, _ in similar if similar is not None else similar_items(name, related):
        toks, counted = words(text), set()
        for i, w in enumerate(toks):
            if w in target or is_store(w) or not (_meaningful(w) or w in VERSION_WORDS):
                continue
            if ABBREVIATIONS.get(w, {None}) <= target:  # "TCG" when the item says "Trading Card Game"
                continue
            # Half of a target word written with a space ("Nee Doh" when the item is "NeeDoh").
            if (i + 1 < len(toks) and w + toks[i + 1] in joined_target) or (i and toks[i - 1] + w in joined_target):
                continue
            if w not in counted:
                counted.add(w)
                seen[w] += 1
                similar_name.setdefault(w, text)
    out = []
    for w, n in sorted(seen.items(), key=lambda kv: (kv[0] not in VERSION_WORDS, -kv[1], kv[0])):
        reason = VERSION_WORDS.get(w, "on a similar item")
        out.append((w, f"{reason}: “{similar_name[w][:70]}”" + (f" and {n - 1} more" if n > 1 else "")))
    for w in CATCH_ALLS:
        if w not in target and all(w != o for o, _ in out):
            out.append((w, VERSION_WORDS[w] + " (common wrong match)"))
    return out[:top]


def monitor_line(positives, negatives):
    """"+jordan,+low,-gs,-ps": the keyword format most bots and monitors take."""
    pos = [w for w, _, in_line in positives if in_line]
    negs = list(dict.fromkeys(w.lower() for w, _ in negatives))
    return ",".join([f"+{w}" for w in pos] + [f"-{w}" for w in negs if w not in pos])


def line_verdict(pos, neg, text):
    """How a monitor treats an item name: ("match", ""), ("blocked", "-word") or ("missed", "+word").
    Monitors look for each keyword anywhere in the lowercased name, so "+doh" matches "NeeDoh"."""
    low = text.lower()
    for w in pos:
        if w not in low:
            return "missed", "+" + w
    for w in neg:
        if w in low:
            return "blocked", "-" + w
    return "match", ""


def tidy_negatives(negatives, positives, targets):
    """Drop negatives that would also block the product itself (a monitor sees "-ice" inside "Nice")."""
    pos = {w for w, *_ in positives}
    names = [t.lower() for t in targets if t]
    out = [(w, r) for w, r in negatives if w not in pos and not any(w in n for n in names)]
    # "-ice" already blocks everything "-nice" would.
    return [(w, r) for w, r in out if not any(o != w and o in w for o, _ in out)]


def drop_unneeded(negatives, positives, similar):
    """Keep a negative only if it blocks something the + words would otherwise let through. ("-surging"
    adds nothing when "+prismatic" already rules out "Surging Sparks".) Version words and catch-alls stay:
    they guard against items this page doesn't show."""
    pos = [w for w, _, in_line in positives if in_line]
    caught = [t.lower() for t, _ in similar if line_verdict(pos, [], t)[0] == "match"]
    return [(w, r) for w, r in negatives if w in VERSION_WORDS or any(w in t for t in caught)]


def cover_lookalikes(positives, negatives, similar, targets, top=20):
    """Every similar item the line would still match gets a negative of its own, so none slips through."""
    pos = [w for w, _, in_line in positives if in_line]
    negatives = negatives[:top]
    names = " ".join(targets).lower()
    for text, _ in similar:
        if line_verdict(pos, [w for w, _ in negatives], text)[0] != "match":
            continue
        extra = [w for w in words(text) if (_meaningful(w) or w in VERSION_WORDS) and w not in names]
        if extra:
            best = max(extra, key=lambda w: (w in VERSION_WORDS, len(w)))
            negatives.insert(0, (best, f"keeps out “{text[:70]}”, which your + words would also match"))
    return negatives


def analyze(html, url=""):
    p = PageParser()
    p.feed(html)
    p.close()
    ld, ld_others = _ld_products(p.ld)
    title = " ".join(unescape(p.title).split())
    body = " ".join(p.text)
    meta_kw = [k.strip() for k in re.split(r"[,;\n]", p.meta.get("keywords", "")) if k.strip()]
    single, phrases, total = count_keywords(" ".join([title, *(h for _, h in p.headings), body]))
    important = " ".join([title, p.meta.get("description", ""), " ".join(h for _, h in p.headings)]).lower()
    single = [(w, c, pct, w in important) for w, c, pct in single]
    phrases = [(w, c, w in important) for w, c in phrases]
    name = target_name(p, ld)
    related = p.links + p.alts + ld_others + [h for t, h in p.headings if t != "h1"]
    similar = similar_items(name, related)
    codes = product_codes(body + " " + title, ld)
    site, host_labels = site_words(p, url)
    page_text = " ".join([title, body, *related, *name_variants(p, ld)])
    positives = positive_keywords(name, similar, ld.get("brand", ""), codes, page_text=page_text, skip=site)
    targets = name_variants(p, ld)
    negatives = negative_keywords(name, related, top=40, similar=similar, also=targets[1:], site=site,
                                  host_labels=host_labels)
    negatives = cover_lookalikes(positives, drop_unneeded(tidy_negatives(negatives, positives, targets), positives, similar),
                                 similar, targets)
    return {
        "url": url, "host": urlparse(url).netloc, "title": title,
        "description": p.meta.get("description") or p.meta.get("og:description", ""),
        "meta_keywords": meta_kw, "og_title": p.meta.get("og:title", ""), "site": p.meta.get("og:site_name", ""),
        "headings": p.headings[:40], "product": ld, "codes": codes,
        "alts": list(dict.fromkeys(p.alts))[:20], "words": single, "phrases": phrases, "word_count": total,
        "thin": total < 60, "name": name, "positives": positives, "negatives": negatives,
        # Words that are part of the product's own name, however the page spells it: never negatives.
        "protected": sorted(set().union(*(words(n) for n in name_variants(p, ld))) | {
            t for w in words(name) if len(w) >= 5 for t in words(spaced_form(w, page_text))}),
        "related": [text for text, _ in similar][:30],
        # What the line is checked against on the page: the product itself, then its look-alikes.
        "check": [{"text": t, "target": True} for t in dict.fromkeys(targets)][:3]
                 + [{"text": text, "target": False} for text, _ in similar][:30],
    }


def page_summary(result):
    """What the model is shown about a page (also what a saved example keeps)."""
    return {
        "title": result["title"], "description": result["description"], "meta_keywords": result["meta_keywords"],
        "headings": [h for _, h in result["headings"][:15]], "product": result["product"], "codes": result["codes"],
        "top_words": [w for w, *_ in result["words"][:25]], "top_phrases": [p for p, *_ in result["phrases"][:20]],
        "target_item": result["name"], "similar_items_on_page": result["related"][:20],
    }


AI_SYSTEM = ("You read keyword data pulled from one web page. Reply with JSON only: "
             '{"summary": "one or two sentences on what the page is about", '
             '"main_keywords": ["the 5-12 keywords or phrases the page is built around, most important first"], '
             '"product": "product name if it is a product page, else empty", '
             '"positive_keywords": [{"word": "a word from the target item name a monitor should require", '
             '"reason": "short reason"}], '
             '"negative_keywords": [{"word": "a word that appears in the names of similar but wrong items '
             '(other sizing runs like GS/PS/TD, other genders, other cuts or colourways, apparel, accessories, used '
             'or replica) and NOT in the target item name", "reason": "short reason"}]}. '
             "Negative keywords are for a restock monitor that must match the target item and skip look-alikes. "
             "Earlier pages may follow with the keywords the user settled on; learn from them.")


def ai_summary(result, config, con):
    """Ask the local model which keywords the page is targeting. Returns dict or raises LLMError.

    Runs as the "Keyword finder" task on the Training page, so pages you corrected are shown to the
    model first, and its instructions can be edited there."""
    client = LLMClient(config.llm_base_url, config.llm_model, config.llm_api_key, timeout=config.llm_timeout)
    t = keywordrules.keyword_task(con, AI_SYSTEM)
    answer = json.loads(tasks.answer(client, con, t, json.dumps(page_summary(result), ensure_ascii=False)))
    if not isinstance(answer, dict):
        raise LLMError("The model's answer wasn't in the expected format.")

    def listed(key):
        value = answer.get(key)
        return value if isinstance(value, list) else []

    def pairs(key):
        # Models sometimes give plain strings instead of {"word", "reason"} objects; take either.
        out = []
        for n in listed(key):
            word, reason = (n.get("word"), n.get("reason")) if isinstance(n, dict) else (n, "")
            if isinstance(word, (str, int, float)) and str(word).strip():
                out.append((str(word).strip(), str(reason or "").strip()))
        return out

    target = set(words(result["name"]))
    negs = [(w, r) for w, r in pairs("negative_keywords") if w.lower() not in target][:15]  # never exclude the item's own words
    return {"summary": str(answer.get("summary") or ""), "product": str(answer.get("product") or ""), "negatives": negs,
            "positives": pairs("positive_keywords")[:10],
            "main_keywords": [str(k) for k in listed("main_keywords") if str(k).strip()][:15]}


@bp.route("/keywords", methods=["GET", "POST"])
def keywords_page():
    if request.method == "GET":
        try:
            saved = keywordrules.all_rules(rules_db())
        except Exception:
            logger.exception("keyword finder: couldn't load saved keyword rules")
            saved = []
        return render_template("keywords.html", result=None, error=None, url="", use_ai=False, saved=saved)
    url, use_ai = request.form.get("url", ""), bool(request.form.get("ai"))
    result = error = None
    try:
        url = normalize_url(url)
        logger.info("keyword finder: fetching %s", url)
        final, html = fetch(url)
        result = analyze(html, final)
        logger.info("keyword finder: %s -> %d words, %d keywords, %d phrases%s", final, result["word_count"],
                    len(result["words"]), len(result["phrases"]), " (little text, page may load by script)" if result["thin"] else "")
    except FetchError as e:
        error = str(e)
        logger.warning("keyword finder: %s failed: %s", url, e)
    except Exception as e:  # odd markup should never take the page down
        error = f"Couldn't read that page: {e}"
        logger.exception("keyword finder: unexpected error for %s", url)
    rules = []
    if result:
        try:
            rules = keywordrules.rules_for(rules_db(), result["host"], result["product"].get("brand", ""))
            keywordrules.apply(result, rules)
        except Exception:  # the suggestions still show without your saved rules
            logger.exception("keyword finder: couldn't load saved keyword rules")
    ai_token = None
    if result and use_ai:
        # The model can take a minute or more (longer while a sync is using it), so the page shows the
        # keywords right away and asks the model from the browser; a long-held request left the window blank.
        ai_token = remember_page(result, rules)
    try:
        line = monitor_line(result["positives"], result["negatives"]) if result else ""
        return render_template("keywords.html", result=result, error=error, url=url, use_ai=use_ai, ai_token=ai_token,
                               line=line, summary=page_summary(result) if result else None)
    except Exception as e:
        logger.exception("keyword finder: couldn't show results for %s", url)
        return render_template("keywords.html", result=None, url=url, use_ai=use_ai, ai_token=None, line="",
                               error=f"Couldn't show the results for that page ({type(e).__name__}: {e}). "
                                     "Details are in Logs; please send them over.")


def merge_ai(result, ai):
    """Put the model's extra + and - keywords into the same lists, so they can be kept or removed like the rest."""
    have = {w.lower() for w, *_ in result["positives"]} | {w.lower() for w, _ in result["negatives"]}
    for w, r in ai["positives"]:
        if w.lower() not in have:
            result["positives"].append((w.lower(), "local AI" + (f": {r}" if r else ""), True))
            have.add(w.lower())
    keep = {w.lower() for w, *_ in result["positives"]} | set(result.get("protected", ()))
    for w, r in ai["negatives"]:
        toks = words(w)
        # "nee doh" is needoh written with a space: never exclude a + keyword, however it's spelled.
        if not toks or with_joins(toks) & keep or "".join(toks) in keep:
            continue
        if w.lower() not in have:
            result["negatives"].append((w.lower(), "local AI" + (f": {r}" if r else "")))
            have.add(w.lower())


# Pages waiting on the local AI, by token: the page is fetched once, and the model reads the same analysis.
_PAGES, _PAGES_LOCK = OrderedDict(), threading.Lock()


def remember_page(result, rules):
    token = secrets.token_urlsafe(12)
    with _PAGES_LOCK:
        _PAGES[token] = (copy.deepcopy(result), rules)
        while len(_PAGES) > 20:
            _PAGES.popitem(last=False)
    return token


@bp.route("/keywords/ai", methods=["POST"])
def keywords_ai():
    """The local AI's suggestions for a page already shown: {ok, summary, main_keywords, positives, negatives}
    with only the + and - keywords that aren't on the page yet."""
    token = (request.get_json(silent=True) or {}).get("token", "")
    with _PAGES_LOCK:
        held = _PAGES.get(token)
    if not held:
        return jsonify(ok=False, error="That page has expired. Press Find keywords again.")
    result, rules = copy.deepcopy(held[0]), held[1]
    before = {("+", w) for w, *_ in result["positives"]} | {("-", w) for w, _ in result["negatives"]}
    try:
        ai = keywordrules.filter_ai(ai_summary(result, Config.from_env(), rules_db()), rules)
        merge_ai(result, ai)
    except LLMError as e:
        logger.warning("keyword finder: local AI summary failed: %s", e)
        return jsonify(ok=False, error=str(e))
    except Exception as e:  # the keyword results already show without the AI part
        logger.exception("keyword finder: local AI summary crashed for %s", result["url"])
        return jsonify(ok=False, error=f"Something went wrong asking the model ({type(e).__name__}: {e}).")
    logger.info("keyword finder: local AI picked %d main keywords for %s", len(ai["main_keywords"]), result["url"])
    targets = [c["text"] for c in result.get("check", []) if c["target"]] or [result["name"]]
    negs = tidy_negatives(result["negatives"], result["positives"], targets)
    return jsonify(ok=True, summary=ai["summary"], main_keywords=ai["main_keywords"],
                   positives=[[w, r] for w, r, _ in result["positives"] if ("+", w) not in before],
                   negatives=[[w, r] for w, r in negs if ("-", w) not in before])


def rules_db():
    return keywordrules.ensure(db.connect(current_app.config.get("DB_PATH")))


@bp.post("/keywords/feedback")
def keywords_feedback():
    """Keep or drop one suggestion. Saves a rule for the store and brand, and the page as a training example."""
    data = request.get_json(silent=True) or {}
    try:
        con = rules_db()
        word, rule_id = keywordrules.save_rule(con, data.get("host", ""), data.get("brand", ""), data.get("kind", ""),
                                      str(data.get("word", "")), data.get("action", ""), data.get("url", ""),
                                      data.get("product", ""))
        if isinstance(data.get("page"), dict):
            keywordrules.save_example(con, AI_SYSTEM, data["page"], [str(w) for w in data.get("positives") or []],
                                      [str(w) for w in data.get("negatives") or []])
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        logger.exception("keyword finder: couldn't save feedback")
        return jsonify(ok=False, error=f"Couldn't save that ({e})."), 500
    return jsonify(ok=True, word=word, rule_id=rule_id)


@bp.post("/keywords/rules/<int:rule_id>/delete")
def keywords_rule_delete(rule_id):
    keywordrules.delete_rule(rules_db(), rule_id)
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify(ok=True)
    return redirect("/keywords#rules")
