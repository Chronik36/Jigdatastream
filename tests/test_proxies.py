import json
import urllib.error

import jigstream.app as appmod
from jigstream import db, proxies


def test_parse_lines():
    p = proxies.parse_line
    assert p("1.2.3.4:8080") == {"host": "1.2.3.4", "port": 8080, "scheme": "http", "user": "", "password": ""}
    assert p("isp.leaf.io:7777:jake:s3cr:et")["password"] == "s3cr:et"
    assert p("jake:pw@res.oculus.net:6000")["user"] == "jake"
    assert p("http://a:b@Res.Host.com:80/")["host"] == "res.host.com"
    assert p("jake:pw:res.oculus.net:6000")["host"] == "res.oculus.net"
    assert p("not a proxy") is None and p("# comment") is None


def test_add_test_and_locate(tmp_path):
    con = proxies.ensure(db.connect(str(tmp_path / "p.db")))
    added, dup, bad = proxies.add_list(con, "1.2.3.4:80:u1:p1\n1.2.3.4:80:u1:p1\n5.6.7.8:3128\nnonsense", "Leaf", "Target ISP")
    assert (added, dup, len(bad)) == (2, 1, 1)
    ids = [r[0] for r in con.execute("SELECT id FROM proxies ORDER BY id")]

    def fetch(p, url):
        if p["host"] == "5.6.7.8":
            raise urllib.error.HTTPError(url, 403, "no", {}, None)
        return 200
    proxies.run_tests(con, ids, ["Target", "Walmart"], fetch=fetch)
    g = proxies.groups(con)
    assert g[0]["name"] == "Target ISP" and g[0]["tested"] == 4 and g[0]["ok"] == 2
    assert {t["result"] for t in g[0]["proxies"][1]["tests"].values()} == {"blocked"}
    assert "p1" not in g[0]["proxies"][0]["label"]  # passwords never shown

    answers = {"": {"query": "9.9.9.9", "city": "Ashburn", "regionName": "Virginia", "countryCode": "US", "hosting": True},
               "1.2.3.4": {"query": "1.2.3.4", "city": "Dallas", "countryCode": "US"}}
    assert proxies.locate(con, ids[0], fetch=lambda url, o: answers[url.split("/json/")[1].split("?")[0]]) is None
    row = proxies.get(con, ids[0])
    assert row["exit_ip"] == "9.9.9.9" and json.loads(row["entry_geo"])["city"] == "Dallas"
    assert proxies.kind(json.loads(row["exit_geo"])) == "Datacenter"


def test_proxy_pages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = appmod.create_app(str(tmp_path / "s.db"))
    c = app.test_client()
    assert "No proxies yet" in c.get("/proxies").get_data(as_text=True)
    c.post("/proxies/add", data={"list": "1.2.3.4:80:jacob:hunter2", "provider": "Leaf", "group": "ISP"})
    page = c.get("/proxies").get_data(as_text=True)
    assert "1.2.3.4:80" in page and "hunter2" not in page and "Leaf" in page
    monkeypatch.setattr(proxies, "speed", lambda p, site, fetch=None: {"ms": 120, "status": 200, "result": "ok", "error": ""})
    c.post("/proxies/test", data={"id": "1", "site": "Target", "wait": "1"})
    detail = c.get("/proxies/1").get_data(as_text=True)
    assert "120 ms" in detail and "hunter2" not in detail and "Find where it comes out" in detail
    c.post("/proxies/delete", data={"id": "1"})
    assert "No proxies yet" in c.get("/proxies").get_data(as_text=True)
