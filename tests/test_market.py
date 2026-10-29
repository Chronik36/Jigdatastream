import json

import pytest

from jigstream import db, market, products, tasks
from jigstream.app import create_app
from jigstream.orderstore import Store

RAW = "Pok&#233;mon Trading Card Game: Scarlet &amp; Violet&#8212;Surging Sp..."
PC_SEARCH = """<table><tr id="product-1" data-product="1"><td class="image"><a href="https://www.pricecharting.com/game/pokemon-surging-sparks/elite-trainer-box"><img class="photo" src="https://storage.googleapis.com/images.pricecharting.com/abc/60.jpg"></a></td>
<td class="title"><a href="https://www.pricecharting.com/game/pokemon-surging-sparks/elite-trainer-box">Elite Trainer Box</a><div class="console-in-title"><a href="/console/pokemon-surging-sparks">Pokemon Surging Sparks</a></div></td>
<td class="price numeric used_price"><span class="js-price">$124.99</span></td></tr>
<tr id="product-2" data-product="2"><td class="title"><a href="/game/pokemon-surging-sparks/elite-trainer-box-pokemon-center">Elite Trainer Box [Pokemon Center]</a><div class="console-in-title">Pokemon Surging Sparks</div></td>
<td class="price numeric used_price"><span class="js-price">$255.75</span></td></tr>
<tr id="product-3" data-product="3"><td class="title"><a href="/game/pokemon-surging-sparks/booster-bundle">Booster Bundle</a><div>Pokemon Surging Sparks</div></td>
<td class="price numeric used_price"><span class="js-price">$40.00</span></td></tr></table>"""
PC_PRODUCT = """<html><head><title>Elite Trainer Box Prices | Pokemon Surging Sparks | Pokemon Cards</title></head><body>
<h1 id="product_name" class="chart_title">Elite Trainer Box <a href="/console/pokemon-surging-sparks">Pokemon Surging Sparks</a></h1>
<img src="https://storage.googleapis.com/images.pricecharting.com/cspose2vqubbmkds/1600.jpg">
<table><tr><td id="used_price"><span class="price js-price">$123.79</span></td></tr></table>
<script>VGPC.chart_data = {"used": [[1746057600000, 10000], [1748736000000, 11000], [1751328000000, 12100], [1754006400000, 0]], "new": []};</script>
</body></html>"""
EBAY = """<ul><li class="s-item"><div class="s-item__title">Pokemon Surging Sparks Elite Trainer Box Sealed</div><span class="s-item__price">$118.00</span></li>
<li class="s-item"><div class="s-item__title">Surging Sparks Elite Trainer Box ETB</div><span class="s-item__price">$122.00</span></li>
<li class="s-item"><div class="s-item__title">Surging Sparks Elite Trainer Box PSA 10 graded</div><span class="s-item__price">$900.00</span></li>
<li class="s-item"><div class="s-item__title">Surging Sparks Elite Trainer Box case of 10</div><span class="s-item__price">$1200.00</span></li></ul>"""
TCG = {"results": [{"results": [
    {"productName": "Surging Sparks Elite Trainer Box", "setName": "SV08: Surging Sparks", "marketPrice": 126.5, "productId": 565606},
    {"productName": "Surging Sparks Booster Bundle", "setName": "SV08: Surging Sparks", "marketPrice": 41.0, "productId": 565607}]}]}


class FakeLLM:
    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def chat_json(self, system, text, examples=()):
        self.seen.append((text, list(examples)))
        return self.answer


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(products, "MIN_GAP", 0)
    products._last.clear()
    products._paused.clear()
    market.state.update(running=False, done=0, total=0, current="", finished=None, last="")


def sites(monkeypatch, tcg=TCG):
    seen = []

    def page(url):
        seen.append(url)
        if "search-products" in url:
            return PC_SEARCH
        if "pricecharting.com/game/" in url:
            return PC_PRODUCT
        if "ebay.com" in url:
            return EBAY
        raise AssertionError(url)
    monkeypatch.setattr(products, "fetch_page", page)
    monkeypatch.setattr(products, "_get_json", lambda url, headers=None, data=None, gap=None: tcg)
    return seen


def _order(con, items, total=None, status="in_transit", n=[0]):
    n[0] += 1
    cur = con.execute("INSERT INTO tracked_orders (merge_key, retailer, order_number, order_date, items, total, shipping_status,"
                      " message_ids) VALUES (?,?,?,?,?,?,?,?)",
                      (f"t:{n[0]}", "Target", str(1000 + n[0]), "2026-10-20", json.dumps(items), total, status, "[]"))
    con.commit()
    return cur.lastrowid


def test_cleanup_and_matching():
    assert market.cleanup("Pokémon Trading Card Game: Scarlet & Violet—Surging Sparks Elite Trainer Box...") == \
        "Surging Sparks Elite Trainer Box"
    cands = [{"title": "Elite Trainer Box [Pokemon Center] Pokemon Surging Sparks"},
             {"title": "Elite Trainer Box Pokemon Surging Sparks"},
             {"title": "Surging Sparks Elite Trainer Box PSA 10"}]
    assert market.best_match(cands, "Surging Sparks Elite Trainer Box") is cands[1]
    assert market.best_match(cands, "Prismatic Evolutions Booster Bundle") is None
    assert market.key("Surging  Sparks ETB!") == market.key("surging sparks etb")


def test_pricecharting_tcgplayer_and_ebay_readers(monkeypatch):
    sites(monkeypatch)
    pc = market.pricecharting("Surging Sparks Elite Trainer Box")
    assert pc["price"] == 123.79 and pc["link"].endswith("/elite-trainer-box")
    assert pc["image"].endswith("cspose2vqubbmkds/240.jpg")
    assert [p for _, p in pc["chart"]] == [100.0, 110.0, 121.0]
    tcg = market.tcgplayer("Surging Sparks Elite Trainer Box")
    assert tcg["price"] == 126.5 and tcg["image"].endswith("565606_200w.jpg")
    eb = market.ebay("Surging Sparks Elite Trainer Box")
    assert eb["price"] == 120.0 and eb["count"] == 2  # graded and case listings left out


def test_trend_and_projection():
    m = {"chart": [["2026-05", 100], ["2026-06", 110], ["2026-07", 121]]}
    assert market.monthly_rate(m) == pytest.approx(0.10)
    m = {"chart": [["2026-05", 100], ["2026-06", 200], ["2026-07", 400]]}
    assert market.monthly_rate(m) == market.MAX_RATE  # capped
    assert market.monthly_rate({"history": [["2026-10-01", 100]]}) is None
    assert market.projected(100, 0.1, 3) == 133.1


def test_garbled_name_is_named_priced_and_projected(tmp_path, monkeypatch):
    path = str(tmp_path / "m.db")
    app = create_app(path)
    con = Store(db.connect(path)).db
    oid = _order(con, [{"name": RAW, "quantity": 2, "price": 49.99}], total=107.0)
    _order(con, [{"name": RAW, "quantity": 1}], total=49.99, status="cancelled")  # cancelled: not held
    seen = sites(monkeypatch)
    llm = FakeLLM({"name": "Pokémon TCG: Scarlet & Violet Surging Sparks Elite Trainer Box",
                   "search": "Surging Sparks Elite Trainer Box", "sure": True})
    assert market.refresh(path, llm) == 1
    m = market.get(con, market.key(RAW))
    assert m["name_how"] == "AI" and m["search"] == "Surging Sparks Elite Trainer Box"
    assert any("Surging+Sparks+Elite+Trainer+Box" in u for u in seen)
    assert m["prices"]["pricecharting"]["price"] == 123.79 and m["prices"]["ebay"]["price"] == 120.0
    assert "tradepost" not in m["prices"] and "Tradepost" in m["errors"]["tradepost"]
    assert m["market"] == 123.79  # middle of 123.79, 126.50, 120.00
    assert m["image"].endswith("240.jpg") and m["trend"] == pytest.approx(0.10)
    rows, tot = market.summary(con)
    assert tot["items"] == 2 and tot["value"] == 247.58 and tot["profit"] == round(2 * (123.79 - 49.99), 2)
    assert tot["future"][0]["value"] == pytest.approx(272.34, abs=0.02)
    c = app.test_client()
    page = c.get("/market").get_data(as_text=True)
    for want in ("Surging Sparks Elite Trainer Box", "$247.58", "+$147.60", "In 6 months", "+10.0%/mo", "Email said"):
        assert want in page, want
    assert "+$147.60" in c.get("/market?show=profit").get_data(as_text=True)
    sheet = c.get(f"/orders/{oid}/sheet").get_data(as_text=True)
    assert "Market value" in sheet and "Worth $247.58" in sheet and "Fix name" in sheet
    orders = c.get("/orders?day=all").get_data(as_text=True)
    assert "Market $123.79" in orders and "Worth now" in orders and "240.jpg" in orders


def test_fixing_the_name_teaches_and_looks_up_again(tmp_path, monkeypatch):
    path = str(tmp_path / "f.db")
    app = create_app(path)
    con = Store(db.connect(path)).db
    oid = _order(con, [{"name": "PKMN SV8 ETB"}], total=49.99)
    sites(monkeypatch, tcg={"results": []})
    market.refresh(path, None)
    m = market.get(con, market.key("PKMN SV8 ETB"))
    assert m["name_how"] == "as written" and m["market"] is None
    started = []
    monkeypatch.setattr(market, "start", lambda db_path, keys=None, limit=0: started.append(keys))
    c = app.test_client()
    r = c.post(f"/market/{m['key']}/name", data={"name": "Surging Sparks Elite Trainer Box"})
    assert r.status_code == 302 and started == [[m["key"]]]
    m = market.get(con, m["key"])
    assert m["name_how"] == "fixed" and m["updated_at"] is None
    t = market.name_task(con)
    ex = tasks.examples(con, t["id"])
    assert len(ex) == 1 and "PKMN SV8 ETB" in ex[0]["input"] and "Surging Sparks" in ex[0]["output"]
    market.refresh(path, FakeLLM({"name": "wrong", "search": "wrong"}), keys=[m["key"]])  # a fixed name isn't re-asked
    m = market.get(con, m["key"])
    assert m["name"] == "Surging Sparks Elite Trainer Box" and m["market"] == pytest.approx(121.9, abs=0.01)
    # Typed prices stick, Tradepost included; "my market price" overrides.
    c.post(f"/market/{m['key']}/price", data={"source": "tradepost", "price": "80, 90"})
    m = market.get(con, m["key"])
    assert m["prices"]["tradepost"]["price"] == 85 and m["prices"]["tradepost"]["manual"]
    c.post(f"/market/{m['key']}/price", data={"source": "market", "price": "150"})
    assert market.get(con, m["key"])["market"] == 150
    c.post(f"/market/{m['key']}/image", data={"image_url": "https://img.example/x.jpg"})
    assert market.get(con, m["key"])["image"] == "https://img.example/x.jpg"
    assert "https://img.example/x.jpg" in c.get(f"/orders/{oid}/sheet").get_data(as_text=True)
    c.post(f"/market/{m['key']}/ignore", data={})
    _, tot = market.summary(con)
    assert tot["items"] == 0 and tot["ignored"] == 1


def test_tradepost_payouts_count(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    create_app(path)
    con = Store(db.connect(path)).db
    from jigstream import sales
    sales.ensure(con)
    sales.add(con, {"platform": "Tradepost", "item": "Surging Sparks Elite Trainer Box", "payout": 95.0, "date": "2026-10-01"})
    got = market.tradepost(con, "Surging Sparks Elite Trainer Box", "Surging Sparks Elite Trainer Box")
    assert got["price"] == 95.0 and "Tradepost" in got["how"]


def test_blocked_site_tries_the_app_window_once(monkeypatch):
    from jigstream import browsertrack

    def refused(url):
        raise products.Blocked("refused")
    monkeypatch.setattr(products, "fetch_page", refused)
    monkeypatch.setattr(browsertrack, "_reader", None)
    with pytest.raises(products.Blocked):
        market.fetch("https://www.ebay.com/sch/i.html?_nkw=x")
    monkeypatch.setattr(browsertrack, "_reader", lambda url, done, timeout, html=False: EBAY if html else "")
    assert market.fetch("https://www.ebay.com/sch/i.html?_nkw=x") == EBAY
    monkeypatch.setattr(browsertrack, "_reader", lambda url, done, timeout, html=False: "<p>Verify you are human</p>")
    with pytest.raises(products.Blocked):
        market.fetch("https://www.ebay.com/sch/i.html?_nkw=x")
