import json

from jigstream import keywords
from jigstream.app import create_app

PAGE = """<!doctype html><html><head><title>Air Jordan 1 Low Panda | Example Kicks</title>
<meta name="description" content="Shop the Air Jordan 1 Low in black and white.">
<meta name="keywords" content="jordan, sneakers, panda">
<script type="application/ld+json">%s</script>
<style>.x{color:red}</style><script>var tracking = "ignore me";</script></head><body>
<nav><a href="/">Home</a><a href="/men">Men</a></nav>
<h1>Air Jordan 1 Low Panda</h1>
<p>The Air Jordan 1 Low Panda comes in black and white leather. Style: 553558-040. Air Jordan 1 Low Panda restocks often.</p>
<h2>You may also like</h2>
<a href="/p/1">Air Jordan 1 Low Panda (GS)</a>
<a href="/p/2">Air Jordan 1 Low Panda (PS)</a>
<a href="/p/3">Air Jordan 1 Mid Panda</a>
<a href="/p/4">Air Jordan 1 Low Panda Socks</a>
<a href="/p/5">Nike Dunk Low Retro</a>
<img src="x.jpg" alt="Air Jordan 1 Low Panda Toddler">
</body></html>""" % json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "Air Jordan 1 Low Panda",
                                 "sku": "553558-040", "brand": {"@type": "Brand", "name": "Jordan"}})


def test_analyze_finds_keywords_and_negatives():
    r = keywords.analyze(PAGE, "https://kicks.example/p/0")
    assert r["title"] == "Air Jordan 1 Low Panda | Example Kicks"
    assert r["name"] == "Air Jordan 1 Low Panda"
    assert r["meta_keywords"] == ["jordan", "sneakers", "panda"]
    assert r["product"]["brand"] == "Jordan" and r["codes"][0] == "553558-040"
    top = {w: c for w, c, *_ in r["words"]}
    assert top["panda"] >= 4 and "ignore" not in top and "the" not in top and "color" not in top
    assert any(p == "air jordan" for p, *_ in r["phrases"])
    pos = {w: reason for w, reason, _ in r["positives"]}
    assert list(pos) == ["air", "jordan", "1", "low", "panda", "553558-040"]
    assert pos["1"] == "model number" and pos["jordan"] == "brand" and "style code" in pos["553558-040"]
    negs = [w for w, _ in r["negatives"]]
    for w in ("gs", "ps", "mid", "socks", "toddler"):
        assert w in negs
    for w in ("air", "jordan", "low", "panda", "dunk", "retro", "nike"):
        assert w not in negs  # target words never; unrelated items don't count
    line = keywords.monitor_line(r["positives"], r["negatives"])
    assert line.startswith("+air,+jordan,+1,+low,+panda,-") and "-gs" in line


def test_catch_alls_skip_target_words():
    negs = keywords.negative_keywords("Air Jordan 1 Low GS", [])
    pos = {w: r for w, r, _ in keywords.positive_keywords("Air Jordan 1 Low GS Shoes", [])}
    assert "gs" in pos and "shoes" not in pos  # the GS version must require GS; generic words add nothing
    assert "gs" not in [w for w, _ in negs] and "ps" in [w for w, _ in negs]


def test_normalize_url():
    assert keywords.normalize_url(" nike.com/t/x ") == "https://nike.com/t/x"
    for bad in ("", "ftp://x.com/a", "javascript:alert(1)"):
        try:
            keywords.normalize_url(bad)
        except keywords.FetchError:
            continue
        raise AssertionError(bad)


def test_page_and_blocked_site(tmp_path, monkeypatch):
    client = create_app(str(tmp_path / "k.db")).test_client()
    assert "Keyword finder" in client.get("/keywords").get_data(as_text=True)

    monkeypatch.setattr(keywords, "fetch", lambda url: (url, PAGE))
    html = client.post("/keywords", data={"url": "kicks.example/p/0"}).get_data(as_text=True)
    assert "553558-040" in html and "+air,+jordan" in html and "-gs" in html

    def blocked(url):
        raise keywords.FetchError("The site refused the request (HTTP 403).")
    monkeypatch.setattr(keywords, "fetch", blocked)
    html = client.post("/keywords", data={"url": "https://shop.example/x"}).get_data(as_text=True)
    assert "HTTP 403" in html


def test_ai_negatives_merged(tmp_path, monkeypatch):
    client = create_app(str(tmp_path / "k.db")).test_client()
    monkeypatch.setattr(keywords, "fetch", lambda url: (url, PAGE))
    monkeypatch.setattr(keywords.LLMClient, "chat_json", lambda self, system, user, **k: {
        "summary": "A sneaker product page.", "main_keywords": ["air jordan 1 low panda"],
        "positive_keywords": [{"word": "panda", "reason": "colourway"}],
        "negative_keywords": [{"word": "reimagined", "reason": "different release"}, {"word": "panda", "reason": "x"}]})
    html = client.post("/keywords", data={"url": "https://kicks.example/p/0", "ai": "1"}).get_data(as_text=True)
    assert "A sneaker product page." in html and "-reimagined" in html and "+panda" in html
    assert "-panda" not in html  # the model may not exclude the product's own words


def test_model_failures_never_crash_the_page(tmp_path, monkeypatch):
    client = create_app(str(tmp_path / "k.db")).test_client()
    monkeypatch.setattr(keywords, "fetch", lambda url: (url, PAGE))

    def slow(self, system, user, **k):
        raise TimeoutError("timed out")

    odd_answers = [slow, lambda self, s, u, **k: {"negative_keywords": 5, "positive_keywords": "panda", "main_keywords": None},
                   lambda self, s, u, **k: ["not", "a", "dict"], lambda self, s, u, **k: {}.get("x")["y"]]
    for answer in odd_answers:
        monkeypatch.setattr(keywords.LLMClient, "chat_json", answer)
        r = client.post("/keywords", data={"url": "https://kicks.example/p/0", "ai": "1"})
        assert r.status_code == 200 and "553558-040" in r.get_data(as_text=True)


def test_plain_string_negatives_from_model(tmp_path, monkeypatch):
    client = create_app(str(tmp_path / "k.db")).test_client()
    monkeypatch.setattr(keywords, "fetch", lambda url: (url, PAGE))
    monkeypatch.setattr(keywords.LLMClient, "chat_json", lambda self, s, u, **k: {"negative_keywords": ["reimagined", 7]})
    assert "-reimagined" in client.post("/keywords", data={"url": "https://x.example/p", "ai": "1"}).get_data(as_text=True)


def test_render_failure_shows_friendly_error(tmp_path, monkeypatch):
    client = create_app(str(tmp_path / "k.db")).test_client()
    monkeypatch.setattr(keywords, "fetch", lambda url: (url, PAGE))
    monkeypatch.setattr(keywords, "monitor_line", lambda *a: (_ for _ in ()).throw(KeyError("boom")))
    r = client.post("/keywords", data={"url": "https://x.example/p"})
    assert r.status_code == 200 and "show the results for that page" in r.get_data(as_text=True)


def test_llm_client_null_reply_is_llm_error(monkeypatch):
    import io
    from jigstream import llm

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout: Resp(b'{"choices":[{"message":{"content":null}}]}'))
    try:
        llm.LLMClient("http://localhost:1/v1", "m").chat_json("s", "u")
    except llm.LLMError:
        pass
    else:
        raise AssertionError("expected LLMError")

    def slow(req, timeout):
        raise TimeoutError("timed out")
    monkeypatch.setattr(llm.urllib.request, "urlopen", slow)
    try:
        llm.LLMClient("http://localhost:1/v1", "m", timeout=5).chat_json("s", "u")
    except llm.LLMError as e:
        assert "longer than 5 seconds" in str(e)


def test_shopify_variants_are_not_lookalikes():
    ld = {"@type": "ProductGroup", "name": "Nee Doh Dig It Pig", "brand": {"name": "Schylling"},
          "hasVariant": [{"@type": "Product", "name": "Nee Doh Dig It Pig - Default Title", "sku": "NDDIP"}]}
    html = ('<html><head><script type="application/ld+json">%s</script></head><body><h1>Nee Doh Dig It Pig</h1>'
            '<a href="/a">Nee Doh Dig It Pig Mini</a></body></html>' % json.dumps(ld))
    r = keywords.analyze(html, "https://gotoytime.com/products/nee-doh-dig-it-pig")
    negs = [w for w, _ in r["negatives"]]
    assert "mini" in negs and "default" not in negs and "title" not in negs


TOY_LD = {"@type": "Product", "name": "Dig It Pig NeeDoh", "brand": {"name": "SCHYLLING"}, "gtin13": "0019649520008"}
TOY_PAGE = """<html><head><title>Nee doh dig it pig - ToyTime</title><meta property="og:site_name" content="ToyTime">
<meta name="description" content="Buy Nee doh dig it pig for only $6.99 at ToyTime!">
<script type="application/ld+json">%s</script></head><body>
<header class="site-header"><svg><title>ToyTimeTrade_IconsTrade</title></svg>
<nav class="menu"><a href="/c/toys">Toys</a><a href="/c/books">Books</a><a href="/c/bruder">Bruder Toys</a>
<a href="/c/calico">Calico Critters</a><a href="/c/m">Model Kits</a><span>Expand child</span></nav></header>
<main><div class="product"><h1>Nee doh dig it pig</h1><p>Squish the Nee Doh pig. Glow and Green colours sold separately.</p>
<form><button>Add to cart</button><select><option>Default Title</option></select></form></div>
<h2>You may also like</h2>
<a href="/p/1"><span class="visually-hidden">Nice Ice Baby NeeDoh</span> Nice Ice Baby NeeDoh Sorry, this item is out of stock $5.99</a>
<a href="/p/2">Nice Cube NeeDoh Nice Cube NeeDoh Sorry, this item is out of stock</a>
<a href="/p/3" title="Needoh ice cube - ToyTime">Needoh ice cube - ToyTime</a>
<a href="/p/4">Nee Doh Dig It Pig Mini</a>
<a href="/p/5">Nee Doh Snowball High Bounce</a>
</main><footer><svg><title>American Express</title></svg><svg><title>Apple Pay</title></svg>
<a href="/policies">Refund policy</a> <a href="/x">Hat Pin Sticker Backpack Pants</a></footer></body></html>""" % json.dumps(TOY_LD)


def test_shop_page_keeps_to_the_product():
    r = keywords.analyze(TOY_PAGE, "https://gotoytime.com/collections/fidgets/products/nee-doh-dig-it-pig")
    assert r["title"] == "Nee doh dig it pig - ToyTime"  # icon <title>s are not the page title
    top = [w for w, *_ in r["words"]]
    for chrome in ("iconstrade", "books", "bruder", "calico", "expand", "express", "refund", "toys", "sorry", "stock"):
        assert chrome not in top
    pos = {w: reason for w, reason, _ in r["positives"]}
    assert "doh" in pos and "nee doh" in pos["doh"].lower() and "needoh" not in pos
    negs = [w for w, _ in r["negatives"]]
    for bad in ("nee", "doh", "toytime", "gotoytime", "sorry", "stock", "item", "hat", "pin", "pants", "backpack",
                "sticker", "default", "title"):
        assert bad not in negs, bad
    for good in ("mini",):  # "Nice Ice Baby" etc. already miss +dig,+pig, so they need no negative
        assert good in negs, good
    assert r["codes"] == ["0019649520008"]  # not "Green", "Kits" or "Glow"
    assert "Nice Ice Baby NeeDoh" in r["related"] and not any("Sorry" in x for x in r["related"])


def test_page_details_wrap_long_values(tmp_path, monkeypatch):
    client = create_app(str(tmp_path / "k.db")).test_client()
    monkeypatch.setattr(keywords, "fetch", lambda url: (url, TOY_PAGE))
    html = client.post("/keywords", data={"url": "https://gotoytime.com/p"}).get_data(as_text=True)
    assert "overflow-wrap:anywhere" in html


def test_corrections_are_remembered_and_taught(tmp_path, monkeypatch):
    client = create_app(str(tmp_path / "k.db")).test_client()
    monkeypatch.setattr(keywords, "fetch", lambda url: (url, TOY_PAGE))
    url = "https://gotoytime.com/products/nee-doh-dig-it-pig"
    html = client.post("/keywords", data={"url": url}).get_data(as_text=True)
    assert 'data-word="mini"' in html and "+dig,+pig,+doh" in html

    page = keywords.page_summary(keywords.analyze(TOY_PAGE, url))
    base = {"url": url, "host": "gotoytime.com", "brand": "SCHYLLING", "product": "Dig It Pig NeeDoh", "page": page,
            "positives": ["dig", "pig", "needoh"], "negatives": ["ice", "glow"]}
    r = client.post("/keywords/feedback", json=dict(base, kind="-", word="mini", action="remove"))
    assert r.get_json()["ok"] and r.get_json()["rule_id"]
    assert client.post("/keywords/feedback", json=dict(base, kind="-", word="-Glow", action="add")).get_json()["word"] == "glow"
    assert client.post("/keywords/feedback", json=dict(base, kind="?", word="x", action="add")).status_code == 400

    # Same store, another page: the rules apply.
    html = client.post("/keywords", data={"url": "https://gotoytime.com/products/other"}).get_data(as_text=True)
    assert 'data-word="mini"' not in html and 'data-word="glow"' in html and "Removed by you" in html
    assert "-glow" in html and "-mini" not in html.split('id="kwline"')[1].split("</div>")[0]

    # Same brand on another store: still applies. Unrelated store and brand: doesn't.
    other = TOY_PAGE.replace("SCHYLLING", "OTHERCO")
    monkeypatch.setattr(keywords, "fetch", lambda url: (url, other))
    assert 'data-word="glow"' not in client.post("/keywords", data={"url": "https://elsewhere.com/p"}).get_data(as_text=True)
    monkeypatch.setattr(keywords, "fetch", lambda url: (url, TOY_PAGE))
    assert 'data-word="glow"' in client.post("/keywords", data={"url": "https://elsewhere.com/p"}).get_data(as_text=True)

    # The corrected page is shown to the model as an example for a new page; AI suggestions you removed stay out.
    seen = {}

    def model(self, system, user, examples=()):
        seen["examples"] = examples
        return {"summary": "x", "negative_keywords": [{"word": "mini"}, {"word": "slime"}]}
    monkeypatch.setattr(keywords.LLMClient, "chat_json", model)
    html = client.post("/keywords", data={"url": "https://gotoytime.com/products/new", "ai": "1"}).get_data(as_text=True)
    assert len(seen["examples"]) == 1 and '"glow"' in seen["examples"][0][1]
    assert 'data-word="slime"' in html and 'data-word="mini"' not in html

    # The corrected page is an example of the Training page's "Keyword finder" task (one per page);
    # rules are listed on the Keywords page and can be deleted.
    from jigstream import db as jdb
    c = jdb.connect(str(tmp_path / "k.db"))
    task_id = c.execute("SELECT id FROM ai_tasks WHERE name = 'Keyword finder'").fetchone()[0]
    rows = c.execute("SELECT output FROM ai_task_examples WHERE task_id = ?", (task_id,)).fetchall()
    assert len(rows) == 1 and '"glow"' in rows[0][0]
    assert "Keyword finder" in client.get("/training").get_data(as_text=True)
    listing = client.get("/keywords").get_data(as_text=True)
    assert "Your keyword rules" in listing and "-mini" in listing
    rule_id = r.get_json()["rule_id"]
    client.post(f"/keywords/rules/{rule_id}/delete")
    html = client.post("/keywords", data={"url": url}).get_data(as_text=True)
    assert 'data-word="mini"' in html


def test_ai_never_excludes_a_plus_keyword_spelled_apart(tmp_path, monkeypatch):
    client = create_app(str(tmp_path / "k.db")).test_client()
    monkeypatch.setattr(keywords, "fetch", lambda url: (url, TOY_PAGE))
    monkeypatch.setattr(keywords.LLMClient, "chat_json", lambda self, s, u, **k: {
        "negative_keywords": [{"word": "nee doh"}, {"word": "Nee"}, {"word": "slime"}]})
    html = client.post("/keywords", data={"url": "https://gotoytime.com/p", "ai": "1"}).get_data(as_text=True)
    assert 'data-word="slime"' in html and 'data-word="nee doh"' not in html and 'data-word="nee"' not in html


def test_saving_waits_for_a_busy_database(tmp_path, monkeypatch):
    import sqlite3
    import threading
    import time as _time
    path = str(tmp_path / "k.db")
    client = create_app(path).test_client()
    client.get("/keywords")  # makes the tables
    blocker = sqlite3.connect(path, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")  # a sync mid-write
    threading.Thread(target=lambda: (_time.sleep(6), blocker.commit()), daemon=True).start()
    r = client.post("/keywords/feedback", json={"url": "u", "host": "shop.example", "kind": "+", "word": "nee",
                                                "action": "add"})
    assert r.get_json()["ok"]  # waited past sqlite's default 5 seconds instead of "database is locked"
    r = client.post("/keywords/feedback", json={"url": "u", "host": "shop.example", "kind": "+", "word": "nee",
                                                "action": "remove"})
    assert r.get_json()["ok"]


# Modeled on a Target trading-card page: one set, sold as many different boxes.
TCG_PAGE = """<!doctype html><html><head>
<title>Pokémon Trading Card Game: Scarlet &amp; Violet—Prismatic Evolutions Elite Trainer Box : Target</title>
<meta property="og:site_name" content="Target">
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product",
 "name": "Pokémon Trading Card Game: Scarlet &amp; Violet—Prismatic Evolutions Elite Trainer Box",
 "brand": {"@type": "Brand", "name": "Pokémon"}, "sku": "94300072"}</script></head><body>
<header><a href="/c/toys">Toys</a><a href="/c/video-games">Video Games</a></header>
<main><h1>Pokémon Trading Card Game: Scarlet &amp; Violet—Prismatic Evolutions Elite Trainer Box</h1>
<p>Item Number (DPCI): 087-35-0498. Includes 9 booster packs and a full-art promo card. TCIN: 94300072</p>
<h2>Related items</h2>
<a href="/p/1">Pokémon TCG: Scarlet &amp; Violet—Prismatic Evolutions Booster Bundle</a>
<a href="/p/2">Pokémon Trading Card Game: Scarlet &amp; Violet—Prismatic Evolutions Surprise Box</a>
<a href="/p/3">Pokémon TCG: Scarlet &amp; Violet—Prismatic Evolutions Super-Premium Collection</a>
<a href="/p/4">Pokémon TCG: Scarlet &amp; Violet—Prismatic Evolutions Binder Collection</a>
<a href="/p/5">Pokémon TCG: Scarlet &amp; Violet—Prismatic Evolutions Pokémon Center Elite Trainer Box</a>
<a href="/p/6">Pokémon TCG: Scarlet &amp; Violet—Surging Sparks Elite Trainer Box</a>
<a href="/p/7">Pokémon TCG: Prismatic Evolutions Elite Trainer Box Sleeves</a>
</main><footer>Target Circle · Help · Returns</footer></body></html>"""


def _line_verdicts(r):
    line = keywords.monitor_line(r["positives"], r["negatives"]).split(",")
    pos = [t[1:] for t in line if t.startswith("+")]
    neg = [t[1:] for t in line if t.startswith("-")]
    return line, {c["text"]: keywords.line_verdict(pos, neg, c["text"])[0] for c in r["check"]}


def test_the_line_catches_the_product_and_nothing_else_on_the_page():
    for page, url in ((PAGE, "https://kicks.example/p"), (TOY_PAGE, "https://gotoytime.com/products/nee-doh-dig-it-pig"),
                      (TCG_PAGE, "https://www.target.com/p/prismatic-etb/-/A-94300072")):
        r = keywords.analyze(page, url)
        line, verdicts = _line_verdicts(r)
        for c in r["check"]:
            want = "match" if c["target"] else ("missed", "blocked")
            assert verdicts[c["text"]] in want, (line, c["text"], verdicts[c["text"]])


def test_product_line_spelled_two_ways_uses_the_part_both_contain():
    r = keywords.analyze(TOY_PAGE, "https://gotoytime.com/products/nee-doh-dig-it-pig")
    line = keywords.monitor_line(r["positives"], r["negatives"])
    assert "+doh" in line and "+needoh" not in line and "+nee," not in line
    pos, neg = [t[1:] for t in line.split(",") if t[0] == "+"], [t[1:] for t in line.split(",") if t[0] == "-"]
    assert keywords.line_verdict(pos, neg, "Nee Doh Dig It Pig")[0] == "match"
    assert keywords.line_verdict(pos, neg, "Dig It Pig NeeDoh")[0] == "match"


def test_negatives_never_block_the_product_itself():
    negs = keywords.tidy_negatives([("ice", "x"), ("nice", "x"), ("pig", "x"), ("cube", "x")], [("dig", "", True)],
                                   ["Dig It Piglet NeeDoh"])
    assert [w for w, _ in negs] == ["ice", "cube"]  # "pig" is inside "Piglet"; "-ice" already covers "nice"


def test_trading_card_boxes():
    r = keywords.analyze(TCG_PAGE, "https://www.target.com/p/prismatic-etb/-/A-94300072")
    line = keywords.monitor_line(r["positives"], r["negatives"])
    pos = [w for w, _, in_line in r["positives"] if in_line]
    for w in ("prismatic", "evolutions", "elite", "trainer"):
        assert w in pos, line
    negs = [w for w, _ in r["negatives"]]
    for w in ("bundle", "center", "sleeves"):
        assert w in negs, line
    for w in ("target", "box", "pok", "mon", "pokémon", "elite"):
        assert w not in negs, line
