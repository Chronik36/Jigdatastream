import io
import json
import urllib.error

import pytest

from jigstream import db, keywords, products
from jigstream.app import create_app

EBAY_PAGE = """<ul>
<li class="s-item s-item__pl-on-bottom"><div class="s-item__title">Shop on eBay</div><span class="s-item__price">$20.00</span></li>
<li class="s-item"><div class="s-item__title"><span>Pokemon Prismatic Evolutions Elite Trainer Box Sealed</span></div>
  <span class="s-item__price">$89.99</span></li>
<li class="s-item"><div class="s-item__title">Prismatic Evolutions Elite Trainer Box ETB New</div><span class="s-item__price">$95.00</span></li>
<li class="s-card"><div class="s-card__title"><span>Pokemon Prismatic Evolutions Elite Trainer Box</span></div>
  <span class="s-card__price">$92.50</span></li>
<li class="s-item"><div class="s-item__title">Prismatic Evolutions Elite Trainer Box EMPTY box only</div><span class="s-item__price">$15.00</span></li>
<li class="s-item"><div class="s-item__title">Pokemon Surging Sparks Booster Bundle</div><span class="s-item__price">$40.00</span></li>
<li class="s-item"><div class="s-item__title">Prismatic Evolutions Elite Trainer Box</div><span class="s-item__price">$80.00 to $99.00</span></li>
</ul>"""

LD_PAGE = """<html><head><script type="application/ld+json">{"@type": "ItemList", "itemListElement": [
 {"@type": "ListItem", "item": {"@type": "Product", "name": "Air Jordan 1 Retro High OG Chicago", "offers": {"price": "310"}}},
 {"@type": "ListItem", "item": {"@type": "Product", "name": "Air Jordan 1 Retro High OG Chicago GS", "offers": {"price": "190"}}},
 {"@type": "ListItem", "item": {"@type": "Product", "name": "Air Jordan 1 High OG Chicago", "offers": [{"lowPrice": 330}]}}
]}</script></head><body>results</body></html>"""

NAME = "Pokemon Prismatic Evolutions Elite Trainer Box"


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCTS_FILE", str(tmp_path / "products.json"))
    monkeypatch.setattr(products, "MIN_GAP", 0)
    monkeypatch.setattr(products, "save_env", lambda updates: None)
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    products._last.clear()
    products._paused.clear()
    products._page_inputs.clear()
    products.last_research.update(query="", searches=[], items=[], errors={}, error=None, **{"for": None})
    monkeypatch.setattr(products.stock, "MIN_GAP", {"target": 0, "bestbuy": 0})
    monkeypatch.setattr(products, "SHOP_GAP", 0)
    products.update_state.update(running=False, done=0, total=0, current="", finished=None)


def pages(monkeypatch, handler):
    calls = []

    def fake(url):
        calls.append(url)
        out = handler(url)
        if isinstance(out, Exception):
            raise out
        return url, out
    monkeypatch.setattr(products.keywords, "fetch", fake)
    return calls


def test_ebay_parser_keeps_matching_sold_prices():
    assert sorted(products.parse_ebay(EBAY_PAGE, NAME)) == ["89.99", "92.50", "95.00"]


def test_ld_listings_skip_other_versions():
    prices, text = products.parse_ld_listings(LD_PAGE, "Air Jordan 1 Retro High OG Chicago")
    assert prices == ["310", 330]  # the GS (kids) listing is a different product
    assert "results" in text


def test_summarize_drops_outliers():
    s = products.summarize([100, 110, 120, 5, 900])
    assert s == {"median": 110, "low": 100, "high": 120, "count": 3}
    assert products.summarize(["x", None]) is None


def test_profit_uses_cheapest_store_and_focus_ranks():
    a = products.new_product("A", retailers=["Target"], msrp=50)
    a["retail"].append({"store": "Walmart", "price": 45.0, "kind": "cost", "link": "", "from": "typed in", "when": ""})
    a["market"] = {"ebay": {"price": 100}, "stockx": {"price": 120}, "goat": {"price": 110}}
    b = products.new_product("B", retailers=["Target"], msrp=20)
    b["market"] = {"ebay": {"price": 90}}
    c = products.new_product("C", retailers=["Target"], msrp=200)
    c["market"] = {"ebay": {"price": 150}}
    d = products.new_product("D", retailers=["Target"], msrp=10, in_production=False)
    d["market"] = {"ebay": {"price": 500}}
    an = products.analysis(a)
    assert an["market"] == 110 and an["best"]["store"] == "Walmart" and an["profit"] == 65
    assert [s["store"] for s in an["stores"]] == ["Walmart", "Target"] and an["stores"][1]["profit"] == 60
    assert an["sell_on"] == "stockx"
    picks = products.focus([a, b, c, d])
    assert [f["product"]["name"] for f in picks] == ["B", "A"]  # C loses money, D is out of production
    assert "Buy at Walmart for $45.00 ($5.00 less than Target)" in picks[1]["reason"]
    assert "Only one selling site" in picks[0]["reason"]


def test_lookup_ebay_and_block_pause(monkeypatch):
    p = products.new_product(NAME)
    calls = pages(monkeypatch, lambda url: EBAY_PAGE)
    entry = products.lookup(p, products.platform("ebay"), use_ai=False)
    assert entry["price"] == 92.5 and entry["count"] == 3 and entry["how"] == "sold listings"
    assert "LH_Sold=1" in calls[0] and "Prismatic+Evolutions" in calls[0]

    calls = pages(monkeypatch, lambda url: keywords.FetchError("The site refused the request (HTTP 403)."))
    with pytest.raises(products.Blocked):
        products.lookup(p, products.platform("stockx"), use_ai=False)
    with pytest.raises(products.Blocked, match="leaving it alone"):
        products.lookup(p, products.platform("stockx"), use_ai=False)
    assert len(calls) == 1  # nothing sent while paused


def test_bot_check_page_is_a_block(monkeypatch):
    pages(monkeypatch, lambda url: "<html><title>Just a moment...</title>Verify you are human</html>")
    with pytest.raises(products.Blocked, match="bot check"):
        products.lookup(products.new_product(NAME), products.platform("goat"), use_ai=False)


def test_app_only_sites_are_typed_in():
    with pytest.raises(products.SourceError, match="phone app"):
        products.lookup(products.new_product(NAME), products.platform("tradepost"))


def test_ai_reads_page_and_learns_from_corrections(monkeypatch, tmp_path):
    path = str(tmp_path / "p.db")
    c = db.connect(path)
    pages(monkeypatch, lambda url: "<html><body>Prismatic ETB $88 sold · $91 sold · Booster $6</body></html>")
    seen = {}

    def fake_answer(llm, con, t, text, trained=False):
        seen["task"], seen["text"] = t["name"], text
        return json.dumps({"prices": [88, 91]})
    monkeypatch.setattr(products.tasks, "answer", fake_answer)
    p = products.new_product(NAME)
    entry = products.lookup(p, products.platform("mercari"), use_ai=True, c=c)
    assert entry["price"] == 89.5 and entry["how"] == "read by the local AI"
    assert seen["task"] == products.READER_TASK and "Booster $6" in seen["text"]
    assert products.teach(c, p["id"], "mercari", [90.0])
    t = products.reader_task(c)
    ex = c.execute("SELECT * FROM ai_task_examples WHERE task_id = ?", (t["id"],)).fetchall()
    assert len(ex) == 1 and json.loads(ex[0]["output"]) == {"prices": [90.0]}


TARGET_SEARCH = {"data": {"search": {"products": [
    {"tcin": "94300069", "item": {"product_description": {"title": "Pokemon Prismatic Evolutions Elite Trainer Box"},
                                  "primary_brand": {"name": "Pokemon"},
                                  "enrichment": {"images": {"primary_image_url": "https://img/t.jpg"}},
                                  "product_classification": {"item_type": {"name": "Trading Cards"}}},
     "price": {"current_retail": 49.99}, "fulfillment": {"shipping_options": {"availability_status": "IN_STOCK"}}},
    {"tcin": "111", "item": {"product_description": {"title": "Pokemon Prismatic Evolutions Sticker Collection"}},
     "price": {"current_retail": 9.99}}]}}}
SHOP_SUGGEST = {"resources": {"results": {"products": [
    {"title": "Pokemon Prismatic Evolutions Elite Trainer Box", "handle": "pkmn-pe-etb", "price": "54.99",
     "available": True, "vendor": "Pokemon", "type": "Collectibles", "image": "//cdn/kith.jpg"},
    {"title": "Air Jordan 4 Retro White Cement", "handle": "aj4-wc", "price": "215.00", "available": False}]}}}
SHOP_PRODUCT = {"pkmn-pe-etb": {"price": 5499, "available": True, "variants": [{"sku": ""}]},
                "aj4-wc": {"price": 21500, "available": False, "variants": [{"sku": "FV5029-100-10"}]}}


class FakeAI:
    def chat_json(self, system, user, examples=()):
        if "search words" in system:
            return {"searches": ["prismatic evolutions elite trainer box"], "skip_words": ["sticker"]}
        return {"keep": [1, 2]}


def stores(monkeypatch, blocked=()):
    def fake(req, timeout=None):
        url = req.full_url
        host = url.split("/")[2]
        if host in blocked:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))
        if "redsky" in url:
            body = TARGET_SEARCH
        elif "suggest.json" in url:
            body = SHOP_SUGGEST if host == "kith.com" else {"resources": {"results": {"products": []}}}
        else:
            body = SHOP_PRODUCT[url.rsplit("/", 1)[1][:-3]]
        return FakeResp(json.dumps(body).encode())
    monkeypatch.setattr(products.urllib.request, "urlopen", fake)
    monkeypatch.setattr(products.stock.urllib.request, "urlopen", fake)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_research_uses_real_listings(monkeypatch):
    stores(monkeypatch, blocked={"undefeated.com"})
    monkeypatch.setattr(products, "_client", lambda config=None: FakeAI())
    searches, items, errors = products.research("pokemon prismatic etb")
    assert searches == ["prismatic evolutions elite trainer box"]
    etb = items[0]
    assert etb["name"] == "Pokemon Prismatic Evolutions Elite Trainer Box"
    assert [(o["store"], o["price"]) for o in etb["offers"]] == [("Target", 49.99), ("Kith", 54.99)]
    assert etb["sku"] == "94300069" and etb["sku_label"] == "Target TCIN" and etb["low"] == 49.99
    assert "Target $49.99" in etb["why"] and "Kith $54.99" in etb["why"]
    assert not any("Sticker" in it["name"] for it in items)  # the AI's skip word
    aj4 = next(it for it in items if "Jordan" in it["name"])
    assert aj4["sku"] == "FV5029-100" and aj4["selling"] == 0  # style code from a size SKU; sold out everywhere
    assert "Undefeated" in errors


def test_research_without_ai_searches_the_words_typed(monkeypatch):
    stores(monkeypatch)

    class Off:
        def chat_json(self, *a, **k):
            raise products.LLMError("Could not reach the local model")
    monkeypatch.setattr(products, "_client", lambda config=None: Off())
    searches, items, _ = products.research("prismatic evolutions elite trainer box")
    assert searches == ["prismatic evolutions elite trainer box"] and items


def test_page_flow(monkeypatch, tmp_path):
    client = create_app(str(tmp_path / "a.db")).test_client()
    assert "Products" in client.get("/products").get_data(as_text=True)

    stores(monkeypatch)
    monkeypatch.setattr(products, "_client", lambda config=None: FakeAI())
    client.post("/products/research", data={"query": "pokemon prismatic evolutions elite trainer box"})
    page = client.get("/products").get_data(as_text=True)
    assert "Track selected" in page and "Target TCIN" in page and "Kith $" in page

    i = next(i for i, it in enumerate(products.last_research["items"]) if it["name"] == NAME)
    client.post("/products/add", data={"pick": [str(i)]})
    data = products.load()
    assert len(data["products"]) == 1
    p = data["products"][0]
    assert [(r["store"], r["price"]) for r in p["retail"]] == [("Target", 49.99), ("Kith", 54.99)]
    assert p["sku"] == "94300069" and p["sku_label"] == "Target TCIN"

    client.post(f"/products/{p['id']}/retail", data={"store": "Walmart", "price": "44.97", "kind": "cost"})
    pages(monkeypatch, lambda url: EBAY_PAGE if "ebay" in url else
          keywords.FetchError("The site refused the request (HTTP 403)."))
    r = client.post(f"/products/{p['id']}/update", data={})
    assert "StockX" in r.headers["Location"] or "error" in r.headers["Location"]
    client.post(f"/products/{p['id']}/market", data={"platform": "tradepost", "price": "100"})
    p = products.load()["products"][0]
    assert p["market"]["ebay"]["price"] == 92.5 and p["market"]["tradepost"]["manual"]
    a = products.analysis(p)
    assert a["best"]["store"] == "Walmart" and a["profit"] == round((92.5 + 100) / 2 - 44.97, 2)
    page = client.get(f"/products?open={p['id']}").get_data(as_text=True)
    assert "Focus on these" in page and "Buy at Walmart" in page

    client.post("/products/sources/add", data={"name": "Whatnot", "url": "https://www.whatnot.com/search?query={q}"})
    assert products.platform("x-whatnot")
    r = client.post("/products/sources/add", data={"name": "Bad", "url": "https://example.com/search"})
    assert "error" in r.headers["Location"]
    client.post("/products/sources/test")
    status = products.load()["status"]
    assert status["ebay"]["state"] == "ok" and status["tradepost"]["state"] == "app"
    assert status["x-whatnot"]["state"] == "blocked"
    client.post("/products/sources/goat/toggle")
    assert "goat" in products.load()["off"]
