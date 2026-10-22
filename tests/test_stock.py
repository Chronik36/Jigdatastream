import io
import json
import urllib.error

import pytest

from jigstream import stock
from jigstream.app import create_app

TARGET_NEARBY = {"data": {"fulfillment_fiats": {"product_id": "91619929", "locations": [
    {"location_available_to_promise_quantity": 0.0, "in_store_only": {"availability_status": "OUT_OF_STOCK"},
     "order_pickup": {"availability_status": "UNAVAILABLE"},
     "store": {"store_id": "1375", "location_name": "Minneapolis Nicollet Mall", "distance": 0.6,
               "mailing_address": {"address_line1": "900 Nicollet Mall", "city": "Minneapolis", "state": "MN"}}},
    {"location_available_to_promise_quantity": 7.0, "in_store_only": {"availability_status": "IN_STOCK"},
     "order_pickup": {"availability_status": "IN_STOCK"},
     "store": {"store_id": "2048", "location_name": "St Paul Midway", "distance": 9.2,
               "mailing_address": {"address_line1": "1300 University Ave W", "city": "St Paul", "state": "MN"}}},
    {"location_available_to_promise_quantity": 2.0, "in_store_only": {"availability_status": "LIMITED_STOCK"},
     "store": {"store_id": "9999", "location_name": "Far Away", "distance": 80.0}},
]}}}

BB_STORES = {"ispuEligible": True, "stores": [
    {"storeID": 281, "name": "Richfield", "address": "1000 W 78th St", "city": "Richfield", "state": "MN",
     "distance": 7.4, "lowStock": True, "minPickupHours": 1},
    {"storeID": 10, "name": "Maplewood", "address": "1795 County Rd D E", "city": "Maplewood", "state": "MN",
     "distance": 3.1, "lowStock": False},
]}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    stock._last_request.clear()
    stock._paused_until.clear()
    stock._results.clear()
    stock.recent.clear()
    monkeypatch.setattr(stock, "MIN_GAP", {"target": 0, "bestbuy": 0})
    monkeypatch.setattr(stock, "save_env", lambda updates: None)
    monkeypatch.delenv("TARGET_REDSKY_KEY", raising=False)


def serve(monkeypatch, handler):
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        out = handler(req.full_url)
        if isinstance(out, Exception):
            raise out
        return FakeResp(out.encode() if isinstance(out, str) else json.dumps(out).encode())

    monkeypatch.setattr(stock.urllib.request, "urlopen", fake)
    return calls


def test_parse_ids():
    assert stock.parse_target_id("https://www.target.com/p/pokemon-etb/-/A-91619929#lnk=sametab") == "91619929"
    assert stock.parse_target_id("https://www.target.com/p/x/-/A-123456?preselect=7654321") == "7654321"
    assert stock.parse_target_id("A-91619929") == "91619929"
    assert stock.parse_bestbuy_id("https://www.bestbuy.com/site/ps5/6566040.p?skuId=6566040") == ("sku", "6566040")
    assert stock.parse_bestbuy_id("6566040") == ("sku", "6566040")
    assert stock.parse_bestbuy_id("711719577294") == ("upc", "711719577294")
    assert stock.detect_retailer("https://www.walmart.com/ip/123") == "walmart"
    with pytest.raises(stock.StockError):
        stock.parse_target_id("pokemon cards")
    with pytest.raises(stock.StockError):
        stock.clean_zip("5540")


def test_target_nearby_stores(monkeypatch):
    calls = serve(monkeypatch, lambda url: TARGET_NEARBY)
    r = stock.check("target", "https://www.target.com/p/x/-/A-91619929", "55401", 25)
    assert "fiats_v1" in calls[0] and "nearby=55401" in calls[0] and "tcin=91619929" in calls[0]
    assert [s["store_id"] for s in r["stores"]] == ["2048", "1375"]  # in stock first; 80 mi store dropped
    assert r["stores"][0]["quantity"] == 7 and r["stores"][0]["in_stock"]
    assert not r["stores"][1]["in_stock"] and r["in_stock"] == 1
    assert r["stores"][0]["address"] == "1300 University Ave W, St Paul, MN"
    assert stock.recent[0]["in_stock"] == 1


def test_target_retired_key_tries_next(monkeypatch):
    def handler(url):
        if stock.TARGET_KEYS[0] in url:
            return urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b'{"message":"Invalid Key"}'))
        return TARGET_NEARBY
    serve(monkeypatch, handler)
    r = stock.check("target", "91619929", "55401", 25)
    assert r["in_stock"] == 1


def test_block_is_reported_and_paused(monkeypatch):
    calls = serve(monkeypatch, lambda url: urllib.error.HTTPError(url, 429, "Too Many", {}, io.BytesIO(b"")))
    with pytest.raises(stock.Blocked):
        stock.check("target", "91619929", "55401", 25)
    n = len(calls)
    with pytest.raises(stock.Blocked, match="leaving it alone"):
        stock.check("target", "91619929", "55401", 25)
    assert len(calls) == n  # no request sent while paused


def test_bot_check_page_counts_as_block(monkeypatch):
    serve(monkeypatch, lambda url: "<html>Please press and hold to confirm you are a human</html>")
    with pytest.raises(stock.Blocked, match="bot check"):
        stock.check("target", "91619929", "55401", 25)


def test_bestbuy_needs_key(monkeypatch):
    monkeypatch.delenv("BESTBUY_API_KEY", raising=False)
    with pytest.raises(stock.StockError, match="API key"):
        stock.check("bestbuy", "6566040", "55401", 25)


def test_bestbuy_stores(monkeypatch):
    monkeypatch.setenv("BESTBUY_API_KEY", "abcdef123456")
    calls = serve(monkeypatch, lambda url: BB_STORES if "/stores.json" in url else
                  {"products": [{"sku": 6566040, "name": "PlayStation 5 Pro", "url": "https://bestbuy.com/x"}]})
    r = stock.check("bestbuy", "https://www.bestbuy.com/site/x/6566040.p?skuId=6566040", "55401", 25)
    assert any("products/6566040/stores.json" in c and "postalCode=55401" in c for c in calls)
    assert r["title"] == "PlayStation 5 Pro" and [s["name"] for s in r["stores"]] == ["Maplewood", "Richfield"]
    assert r["stores"][1]["note"] == "Low stock" and r["in_stock"] == 2


def test_auto_recheck_reuses_recent_result(monkeypatch):
    calls = serve(monkeypatch, lambda url: TARGET_NEARBY)
    stock.check("target", "91619929", "55401", 25)
    n = len(calls)
    stock.check("target", "91619929", "55401", 25, auto=True)
    assert len(calls) == n


def test_unsupported_retailer_message():
    with pytest.raises(stock.StockError, match="bot protection"):
        stock.check("walmart", "https://www.walmart.com/ip/123", "55401", 25)


def test_page(monkeypatch, tmp_path):
    serve(monkeypatch, lambda url: TARGET_NEARBY)
    client = create_app(str(tmp_path / "t.db")).test_client()
    assert b"In-store stock" in client.get("/stock").data
    page = client.post("/stock", data={"product": "https://www.target.com/p/x/-/A-91619929", "retailer": "auto",
                                       "zip": "55401", "radius": "25"}).data.decode()
    assert "St Paul Midway" in page and "1 store in stock" in page and ">7<" in page
    page = client.post("/stock", data={"product": "https://www.walmart.com/ip/123", "zip": "55401"}).data.decode()
    assert "bot protection" in page and "Open Walmart" in page
