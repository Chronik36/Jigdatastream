"""A lump payout email is matched to the sale emails it pays out, and the emails around it can be relabelled."""
from jigstream import db, expensepages, inbox, sales
from jigstream.app import create_app

from test_flex1028 import Now


def sale(con, mid, item, payout, day, platform="eBay"):
    return con.execute("INSERT INTO sales (message_id, platform, item, payout, date, account, source, counted)"
                       " VALUES (?,?,?,?,?, 'me@gmail.com', 'email', ?)",
                       (mid, platform, item, payout, day, 0 if item is None else 1)).lastrowid


def test_a_payout_is_matched_to_the_sales_it_pays_out(tmp_path):
    con = sales.ensure(db.connect(str(tmp_path / "p.db")))
    a = sale(con, "<a>", "ETB", 120.50, "2026-10-12")
    b = sale(con, "<b>", "Needoh", 21.79, "2026-10-14")
    c = sale(con, "<c>", "Booster box", 131.94, "2026-10-15")
    sale(con, "<old>", "Old thing", 21.79, "2026-09-01")  # too long before
    sale(con, "<v>", "Vinted shirt", 21.79, "2026-10-14", "Vinted")  # another platform
    lump = sale(con, "<p>", None, 274.23, "2026-10-23")
    odd = sale(con, "<q>", None, 2.00, "2026-10-21")
    con.commit()
    assert sales.match_payouts(con) == 1
    covered = {r[0] for r in con.execute("SELECT id FROM sales WHERE covered_by = ?", (lump,))}
    assert covered == {a, b, c}  # 120.50 + 21.79 + 131.94 = 274.23
    assert con.execute("SELECT counted FROM sales WHERE id = ?", (lump,)).fetchone()[0] == 0
    rows = {r["id"]: r for r in sales.rows(con)}
    assert len(rows[lump]["covers"]) == len(covered) and rows[next(iter(covered))]["paid_in"]["id"] == lump
    assert rows[odd]["covers"] == []
    sales.update(con, odd, {"counted": 1})  # by hand: kept as you said
    assert con.execute("SELECT counted, count_manual FROM sales WHERE id = ?", (odd,)).fetchone()[:] == (1, 1)
    sales.delete(con, lump)
    assert con.execute("SELECT COUNT(*) FROM sales WHERE covered_by IS NOT NULL").fetchone()[0] == 0


def test_emails_around_a_payout_can_be_relabelled(tmp_path, monkeypatch):
    path = str(tmp_path / "e.db")
    app = create_app(path)
    con = sales.ensure(db.connect(path))
    lump = sale(con, "<p>", None, 274.23, "2026-10-23")
    for mid, sender, subject, day in (("<s1>", "ebay@ebay.com", "You made the sale for Pokemon ETB", "2026-10-15"),
                                      ("<s2>", "news@target.com", "Deals", "2026-10-16"),
                                      ("<s3>", "ebay@ebay.com", "Old", "2026-08-01")):
        con.execute("INSERT INTO emails (message_id, account, sender, subject, date, kind) VALUES (?, 'me@gmail.com', ?, ?, ?, 'other')",
                    (mid, sender, subject, day))
    con.commit()
    c = app.test_client()
    got = c.get(f"/expenses/sales/{lump}/emails").get_json()["emails"]
    assert [e["message_id"] for e in got] == ["<s1>"] and got[0]["kind"] == "other" and not got[0]["sale"]
    page = c.get("/expenses/profit?month=2026-10").get_data(as_text=True)
    assert "See the emails around it" in page and "Emails around it" in page
    monkeypatch.setenv("GMAIL_ACCOUNTS", "me@gmail.com:pw")
    monkeypatch.setattr(expensepages, "llm_for", lambda config: None)
    monkeypatch.setattr(expensepages.threading, "Thread", Now)
    done = []
    monkeypatch.setattr(inbox, "label_emails", lambda config, st, llm, picks, label, sale=None, what=None:
                        done.append((picks[0]["message_id"], label)))
    c.post(f"/expenses/sales/{lump}/email", data={"message_id": "<s1>", "label": "sale"})
    c.post(f"/expenses/sales/{lump}/email", data={"message_id": "<s2>", "label": "sale"})  # not one of its emails
    assert done == [("<s1>", "sale")]
