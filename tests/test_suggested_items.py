import json

from jigstream import db
from jigstream.extract import compact, drop_suggested, split_recommendations
from jigstream.orderstore import drop_suggested_items

BODY = """Thanks for your order!
Order #102000000000505
Pokémon Trading Card Game: 30th Celebration Knock Out Collection
Qty: 2  $29.98
Order total $32.37
Ship to Alex R, 1 Main St
You may also like
Funko Bitty POP! Pokemon 24-...
$9.99
LEGO Pokémon Trainer Su...
$19.99
Ultra Pro Pokémon Tradi...
Questions? Contact us"""


def test_suggestions_are_not_the_order():
    main, recs = split_recommendations(BODY)
    assert "Funko" not in main and "LEGO" in recs and "Order total" in main
    assert "Funko" not in compact(BODY)
    items = [{"name": "Pokémon Trading Card Game: 30th Celebration Knock Out Collection", "quantity": 2},
             {"name": "Funko Bitty POP! Pokemon 24-..."}, {"name": "LEGO Pokémon Trainer Su..."},
             {"name": "Pokémon Destined Rivals Elit…"}]
    assert [i["name"][:7] for i in drop_suggested(items, BODY)] == ["Pokémon"]
    # every name cut off (some shipping emails do that): all stay
    cut = [{"name": "Pokémon Trading Ca..."}, {"name": "Pokémon Destined Ri..."}]
    assert drop_suggested(cut) == cut


def test_saved_orders_lose_their_suggested_items(tmp_path):
    con = db.connect(str(tmp_path / "o.db"))
    items = [{"name": "Poster Collection x2"}, {"name": "Care Bears Toddler It's My B..."}]
    con.execute("INSERT INTO tracked_orders (merge_key, retailer, items) VALUES ('t:1', 'Target', ?)", (json.dumps(items),))
    con.execute("INSERT INTO tracked_orders (merge_key, retailer, items) VALUES ('t:2', 'Target', ?)",
                (json.dumps([{"name": "Only Cut Off..."}]),))
    assert drop_suggested_items(con) == 1
    rows = [json.loads(r[0]) for r in con.execute("SELECT items FROM tracked_orders ORDER BY id")]
    assert rows == [[{"name": "Poster Collection x2"}], [{"name": "Only Cut Off..."}]]
