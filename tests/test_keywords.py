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
    assert r["positives"] == ["air", "jordan", "1", "low", "panda"]
    negs = [w for w, _ in r["negatives"]]
    for w in ("gs", "ps", "mid", "socks", "toddler"):
        assert w in negs
    for w in ("air", "jordan", "low", "panda", "dunk", "retro", "nike"):
        assert w not in negs  # target words never; unrelated items don't count
    line = keywords.monitor_line(r["positives"], r["negatives"])
    assert line.startswith("+air,+jordan,+1,+low,+panda,-") and "-gs" in line


def test_catch_alls_skip_target_words():
    _, negs = keywords.negative_keywords("Air Jordan 1 Low GS", [], [])
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
    monkeypatch.setattr(keywords.LLMClient, "chat_json", lambda self, system, user: {
        "summary": "A sneaker product page.", "main_keywords": ["air jordan 1 low panda"],
        "negative_keywords": [{"word": "reimagined", "reason": "different release"}, {"word": "panda", "reason": "x"}]})
    html = client.post("/keywords", data={"url": "https://kicks.example/p/0", "ai": "1"}).get_data(as_text=True)
    assert "A sneaker product page." in html and "-reimagined" in html
    assert "-panda" not in html  # the model may not exclude the product's own words
