from jigstream import bases, drop
from jigstream.app import create_app
from jigstream.orderstore import Store

from test_drop import hook
from test_profile_match import profile
from test_stuck import mail, setup


class FakeLLM:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def chat_json(self, system, user):
        self.asked.append(user)
        return {"base": self.answer}


def test_rule_ai_and_hand_picks(tmp_path):
    c, store = setup(tmp_path)  # Jacob 1: 123 Main Street, Austin 78701; Ana: 9 Oak Ave, Dallas 75201
    profile(c, "Jacob 2", "Jacob", "Smith", "j2@gmail.com", "QZ 123 Mainn St Apt 9")
    profile(c, "Jacob 3", "Jacob", "Smith", "j3@gmail.com", "One Twenty Three Main St")  # same ZIP, no number
    assert bases.save(c, {"line1": "Main St", "zip": "78701"}) .startswith("Enter the street")
    assert bases.save(c, {"name": "Home", "line1": "123 Main St", "city": "Austin", "state": "Texas", "zip": "78701"}) == ""
    home = bases.listing(c)[0]
    assert home["state"] == "TX"
    res = bases.Resolver(c)
    assert res.of_profile("Jacob 1") == res.of_profile("Jacob 2") == home["id"]
    assert res.of_profile("Ana") is None  # a ZIP no base uses: none, without asking
    assert res.of_profile("Jacob 3") is None
    llm = FakeLLM(1)
    assert bases.match_with_ai(c, llm) == 1
    assert len(llm.asked) == 1 and "One Twenty Three" in llm.asked[0]
    assert bases.Resolver(c).of_profile("Jacob 3") == home["id"]
    assert bases.match_with_ai(c, FakeLLM(1)) == 0  # asked once only
    bases.pick(c, bases.Resolver(c).homes["Jacob 2"]["full"], 0)  # by hand: not this base
    assert bases.Resolver(c).of_profile("Jacob 2") is None
    r = bases.report(c)
    assert {a["how"] for a in r["bases"][0]["addresses"]} == {"rule", "AI"}


def test_shipping_and_drops_open_on_bases(tmp_path):
    c, store = setup(tmp_path)
    mail(store, "m1", "Target", "shipped", "2026-10-20T16:00:00+00:00", "1", address="Jacob Smith, XJ 123 Main St Apt 4, Austin, TX 78701")
    mail(store, "m2", "Walmart", "ordered", "2026-10-21T16:00:00+00:00", "2", address="Ana Lopez, 9 Oak Ave, Dallas, TX 75201")
    hook(c, "a", "Target", "Jacob 1", "ISP A", "isp.example.net:8000", number="1")
    hook(c, "b", "Target", "Ana", "ISP A", "isp.example.net:8000", number="9")
    app = create_app(str(tmp_path / "m.db"))
    cl = app.test_client()
    assert "By base address" not in cl.get("/tracking").get_data(as_text=True)  # no bases yet: the cards as before
    page = cl.get("/bases").get_data(as_text=True)
    assert "123 Main St" in page and "From your profiles" in page
    cl.post("/bases", data={"name": "Home", "line1": "123 Main St", "zip": "78701"})
    home = bases.listing(c)[0]["id"]
    page = cl.get("/tracking").get_data(as_text=True)
    assert "By drop date" in page and "By base address" in page and 'class="scard' not in page  # opens on drop days
    page = cl.get("/tracking?view=bases").get_data(as_text=True)
    assert "By base address" in page and "Not matched to a base" in page and 'class="scard' not in page
    ids = {o["retailer"]: f'id="order-{o["id"]}"' for o in store.orders()}
    page = cl.get(f"/tracking?base={home}").get_data(as_text=True)
    assert ids["Target"] in page and ids["Walmart"] not in page and "Jacob 1" in page and "Checkouts here" in page
    assert ids["Walmart"] in cl.get("/tracking?base=none").get_data(as_text=True)
    page = cl.get("/drop").get_data(as_text=True)
    assert "By base address" in page and "Home" in page
    only = drop.report(Store(c).db, base=str(home))
    assert [x["profile"] for s in only["drops"] for d in s["days"] for x in d["checkouts"]] == ["Jacob 1"]
    assert "Jacob 1" in cl.get(f"/drop?base={home}").get_data(as_text=True)
