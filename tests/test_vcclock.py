"""The card-lock scripts against a stand-in bank page, in a real browser engine."""
import json
import pathlib

import pytest

from jigstream import vcclock

PAGE = pathlib.Path(__file__).parent / "fixtures" / "fake_bank.html"


@pytest.fixture
def bank():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:
            try:
                browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            except Exception:
                pytest.skip("no browser here")
        page = browser.new_page()

        class Window:
            class events:
                class closed:
                    def __iadd__(self, f):
                        return self
                closed = closed()

            def evaluate_js(self, code):
                return page.evaluate(code)

            def load_url(self, url):
                page.goto(PAGE.as_uri())

        class FakeWebview:
            def create_window(self, title, url, **kw):
                page.goto(PAGE.as_uri())
                return Window()
        s = vcclock.Session(FakeWebview(), gap=0.05)
        s.open("Capital One")
        yield s, page
        browser.close()


def test_scan_lock_unlock_and_teach(bank):
    s, page = bank
    cards = {c["last4"]: c for c in s.scan()}
    assert set(cards) == {"9999", "1234", "5678", "1111", "4321"}  # the account in the top bar shows too; you don't pick it
    assert cards["1234"]["label"] == "Target drops" and cards["1234"]["locked"] is False
    assert cards["5678"]["locked"] is False
    assert cards["1111"]["locked"] is True and cards["1111"]["number"] == "4111111111111111"
    assert cards["1111"]["exp"] == "09/28" and cards["1111"]["cvv"] == "123"
    assert cards["1234"]["number"] == "" and cards["1234"]["cvv"] == ""

    s.run(["1234", "5678", "1111", "4321"], "lock")
    res = {r["last4"]: r for r in s.job["results"]}
    assert res["1234"]["ok"] and "Lock card" in res["1234"]["note"]  # answered the confirm box
    assert page.is_checked("input.sw")
    assert res["1111"]["ok"] and "already" in res["1111"]["note"]
    assert not res["4321"]["ok"]  # an icon with no words: needs teaching
    assert {c["last4"]: c["locked"] for c in s.scan()}["1234"] is True
    assert page.locator("text=Delete").count() == 1  # never pressed

    s.run(["1234", "1111"], "unlock")
    assert all(r["ok"] for r in s.job["results"])
    assert {c["last4"]: c["locked"] for c in s.scan()}["1111"] is False

    s.teach("lock")
    page.click("text=🔒")  # you press it once yourself
    assert s.check_learned()["tag"] == "DIV"
    page.evaluate("document.querySelectorAll('.st2')[0].textContent = 'Active'")
    r = s.act("4321", "lock")
    assert r.get("clicked") and page.text_content(".st2") == "Locked"


def test_page_without_the_mac_app(tmp_path, monkeypatch):
    import jigstream.app as appmod
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(vcclock, "_webview", None)
    c = appmod.create_app(str(tmp_path / "s.db")).test_client()
    assert "works in the BotTrack Mac app" in c.get("/cards/locks").get_data(as_text=True)
    assert 'href="/cards/locks"' in c.get("/cards").get_data(as_text=True)


def test_last4_cards_join_the_wallet(tmp_path):
    from jigstream import db, wallet
    con = db.connect(str(tmp_path / "w.db"))
    assert wallet.add_last4(con, "Amex", "5678", "Walmart") and not wallet.add_last4(con, "Amex", "5678")
    card = wallet.cards(con)[0]
    assert card["last4"] == "5678" and card["card_type"] == "Virtual" and wallet.reveal(con, card["id"]) is None
