"""Keyword finder: fetch one web page and list the words and phrases it uses.

The page is fetched once with a normal request. If the site refuses (403, 429, a bot check),
that is reported as-is; nothing here tries to get around it.
"""

import json
import logging
import re
import urllib.error
import urllib.request
from collections import Counter
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

from flask import Blueprint, render_template, request

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
""".split())

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "canvas"}
HEADINGS = {"h1", "h2", "h3"}
WORD = re.compile(r"[a-z0-9][a-z0-9'’&+.-]*[a-z0-9]|[a-z0-9]", re.I)
# Product codes: "DZ5485-612", "FQ8232 100", "1203A243-020", or what follows "SKU:" / "Style:".
CODE = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{4,}-[A-Z0-9]{2,4}\b")
LABELLED = re.compile(r"\b(?:SKU|Style(?:\s*(?:code|#|no\.?))?|Model(?:\s*(?:#|no\.?))?|Item\s*(?:#|no\.?)|MPN|UPC)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,24})", re.I)


class FetchError(Exception):
    pass


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.headings, self.alts, self.text, self.ld = "", {}, [], [], [], []
        self._skip = 0  # depth inside tags whose text isn't shown
        self._in_title = self._in_ld = False
        self._heading = None
        self.links, self._link = [], None  # link texts: on a product page these are mostly other products

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "keywords", "og:title", "og:description", "twitter:title", "og:site_name",
                       "product:brand", "product:retailer_item_id") and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld = True
        elif tag == "img" and a.get("alt", "").strip():
            self.alts.append(a["alt"].strip())
        if tag in HEADINGS and not self._skip:
            self._heading = [tag, []]
        if tag == "a" and not self._skip:
            self._link = [a.get("title", "")]
        if tag in SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ld = False
        if tag in HEADINGS and self._heading and self._heading[0] == tag:
            text = " ".join(" ".join(self._heading[1]).split())
            if text:
                self.headings.append((tag, text))
            self._heading = None
        if tag == "a" and self._link is not None:
            text = " ".join(" ".join(self._link).split())
            if 2 <= len(text) <= 160:
                self.links.append(text)
            self._link = None
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1

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
                if isinstance(node.get("name"), str) and node["name"].strip() != found.get("name"):
                    others.append(node["name"].strip())
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for blob in blobs:
        try:
            walk(json.loads(blob))
        except ValueError:
            continue
    return found, others


def words(text):
    return [w for w in (t.strip(".'-") for t in WORD.findall(text.lower().replace("’", "'"))) if w]


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
    codes += LABELLED.findall(text)
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
# Always worth excluding unless the product itself is one of these.
CATCH_ALLS = ["gs", "ps", "td", "used", "replica"]


def target_name(p, ld):
    for name in (ld.get("name"), p.meta.get("og:title"), next((h for t, h in p.headings if t == "h1"), ""), p.title):
        if name and name.strip():
            # "Air Jordan 1 Low | Nike.com" -> "Air Jordan 1 Low"
            return re.split(r"\s+[|–—]\s+|\s+-\s+(?=[^-]*$)", unescape(name).strip())[0].strip()
    return ""


def negative_keywords(name, related, page_words, top=20):
    """Words that show up on similar items but not in the target's name, most telling first.

    related: names of other items on the page (links, image alts, other products in the page data).
    Returns (positive keywords, [(negative word, reason)]).
    """
    target = set(words(name))
    positives = [w for w in dict.fromkeys(words(name)) if _meaningful(w) or (w.isdigit() and len(w) <= 3)][:8]
    core = {w for w in target if _meaningful(w)}
    seen, similar = Counter(), {}
    for text in dict.fromkeys(related):
        toks = set(words(text))
        if text.strip().lower() == name.lower() or len(core & toks) < max(1, min(2, len(core) // 2)):
            continue  # not a near-match for the target, so its words don't help
        for w in toks - target:
            if _meaningful(w) or w in VARIANT_TERMS:
                seen[w] += 1
                similar.setdefault(w, text)
    out = []
    for w, n in sorted(seen.items(), key=lambda kv: (kv[0] not in VARIANT_TERMS, -kv[1], kv[0])):
        reason = VARIANT_TERMS.get(w, "on a similar item")
        out.append((w, f"{reason}: “{similar[w][:70]}”" + (f" and {n - 1} more" if n > 1 else "")))
    present = set(page_words)
    for w, reason in VARIANT_TERMS.items():
        if w in present and w not in target and w not in seen:
            out.append((w, f"{reason} (mentioned on the page)"))
    for w in CATCH_ALLS:
        if w not in target and all(w != o for o, _ in out):
            out.append((w, VARIANT_TERMS[w] + " (common wrong match)"))
    return positives, out[:top]


def monitor_line(positives, negatives):
    """"+jordan,+low,-gs,-ps": the keyword format most bots and monitors take."""
    negs = list(dict.fromkeys(w.lower() for w, _ in negatives))
    return ",".join([f"+{w}" for w in positives] + [f"-{w}" for w in negs if w not in positives])


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
    positives, negatives = negative_keywords(name, related, words(body))
    return {
        "url": url, "host": urlparse(url).netloc, "title": title,
        "description": p.meta.get("description") or p.meta.get("og:description", ""),
        "meta_keywords": meta_kw, "og_title": p.meta.get("og:title", ""), "site": p.meta.get("og:site_name", ""),
        "headings": p.headings[:40], "product": ld, "codes": product_codes(body + " " + title, ld),
        "alts": list(dict.fromkeys(p.alts))[:20], "words": single, "phrases": phrases, "word_count": total,
        "thin": total < 60, "name": name, "positives": positives, "negatives": negatives,
        "related": [r for r in dict.fromkeys(related) if set(words(r)) & set(positives)][:30],
    }


def ai_summary(result, config):
    """Ask the local model which keywords the page is targeting. Returns dict or raises LLMError."""
    client = LLMClient(config.llm_base_url, config.llm_model, config.llm_api_key, timeout=config.llm_timeout)
    page = {
        "title": result["title"], "description": result["description"], "meta_keywords": result["meta_keywords"],
        "headings": [h for _, h in result["headings"][:15]], "product": result["product"], "codes": result["codes"],
        "top_words": [w for w, *_ in result["words"][:25]], "top_phrases": [p for p, *_ in result["phrases"][:20]],
        "target_item": result["name"], "similar_items_on_page": result["related"][:20],
    }
    system = ("You read keyword data pulled from one web page. Reply with JSON only: "
              '{"summary": "one or two sentences on what the page is about", '
              '"main_keywords": ["the 5-12 keywords or phrases the page is built around, most important first"], '
              '"product": "product name if it is a product page, else empty", '
              '"negative_keywords": [{"word": "a word that appears in the names of similar but wrong items '
              '(other sizing runs like GS/PS/TD, other genders, other cuts or colourways, apparel, accessories, used '
              'or replica) and NOT in the target item name", "reason": "short reason"}]}. '
              "Negative keywords are for a restock monitor that must match the target item and skip look-alikes.")
    answer = client.chat_json(system, json.dumps(page, ensure_ascii=False))
    kws = answer.get("main_keywords") or []
    target = set(words(result["name"]))
    negs = [(str(n.get("word", "")).strip(), str(n.get("reason", "")).strip()) for n in answer.get("negative_keywords") or []
            if isinstance(n, dict)]
    negs = [(w, r) for w, r in negs if w and w.lower() not in target][:15]  # never exclude the item's own words
    return {"summary": str(answer.get("summary") or ""), "product": str(answer.get("product") or ""), "negatives": negs,
            "main_keywords": [str(k) for k in kws if str(k).strip()][:15] if isinstance(kws, list) else []}


@bp.route("/keywords", methods=["GET", "POST"])
def keywords_page():
    if request.method == "GET":
        return render_template("keywords.html", result=None, error=None, url="", use_ai=False)
    url, use_ai = request.form.get("url", ""), bool(request.form.get("ai"))
    result = error = ai = ai_error = None
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
    if result and use_ai:
        try:
            ai = ai_summary(result, Config.from_env())
            logger.info("keyword finder: local AI picked %d main keywords for %s", len(ai["main_keywords"]), url)
        except (LLMError, ValueError, AttributeError) as e:
            ai_error = str(e)
            logger.warning("keyword finder: local AI summary failed: %s", e)
    line = monitor_line(result["positives"], result["negatives"] + (ai["negatives"] if ai else [])) if result else ""
    return render_template("keywords.html", result=result, error=error, url=url, use_ai=use_ai, ai=ai, ai_error=ai_error,
                           line=line)
