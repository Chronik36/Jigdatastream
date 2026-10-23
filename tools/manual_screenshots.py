"""Retake the screenshots shown on the Manual page.

    pip install playwright        # Chromium must be installed for Playwright
    python tools/manual_screenshots.py
    python tools/manual_screenshots.py expenses expense-ledger   # only these shots

Runs BotTrack against a throwaway database filled with made-up sample data (no real inboxes, cards
or addresses), stubs out Gmail, Discord and the AI server, and saves annotated screenshots to
jigstream/static/manual/. Each shot's numbered markers are listed in SHOTS; the Manual's text refers
to the same numbers, so keep the two in step when a page changes.
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "jigstream" / "static" / "manual"
WORK = Path(tempfile.mkdtemp(prefix="bottrack-manual-"))
os.chdir(WORK)
os.environ.update({
    "GMAIL_ACCOUNTS": "sneakers.demo@gmail.com:abcdabcdabcdabcd,restock.demo@gmail.com:efghefghefghefgh",
    "DISCORD_BOT_TOKEN": "demo-token-not-real",
    "DISCORD_CHANNEL_IDS": "111,112",
    "BOTTRACK_CARD_KEY_FILE": str(WORK / "card.key"),
    "JIGSTREAM_LOGS": str(WORK / "logs"),
    "BOTTRACK_CODE_DIR": str(WORK / "app"),
    "TRAINING_MODE": "1",
})
sys.path.insert(0, str(ROOT))

from jigstream import app as appmod, db, discord, emailbook, engines, finetune, inbox, profilebook, tasks, updater, wallet  # noqa: E402
from jigstream.mail import Email  # noqa: E402
from jigstream.orderstore import Store  # noqa: E402

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
A1, A2 = "sneakers.demo@gmail.com", "restock.demo@gmail.com"

ORDERS = [
    ("Nike", "C01234567", "2026-10-18", [("Air Jordan 1 Retro High OG", 2)], 360.00, "1Z999AA10123456784", "UPS", "in_transit", "2026-10-24", A1),
    ("Foot Locker", "P8812004411", "2026-10-15", [("New Balance 9060", 1)], 149.99, "9400111899223100001234", "USPS", "delivered", None, A1),
    ("Target", "912004578833", "2026-10-20", [("Pokemon TCG Elite Trainer Box", 4)], 219.96, None, None, "ordered", None, A2),
    ("Best Buy", "BBY01-806633210", "2026-10-12", [("PlayStation 5 Pro", 1)], 699.99, "772899172345", "FedEx", "out_for_delivery", "2026-10-22", A2),
    ("Walmart", "200011947712345", "2026-10-09", [("Funko Pop! Exclusive", 3)], 47.97, None, None, "cancelled", None, A1),
    ("Shopify: Kith", "KITH-55120", "2026-10-05", [("Kith Box Logo Hoodie", 1)], 165.00, "9205590164917312751089", "USPS", "delivered", None, A2),
]
EXPENSES = [
    ("Oculus Proxies", "proxies", "10 GB residential data", 75.00, "one_time", "2026-10-19", A1),
    ("Valor AIO", "bot", "Monthly license renewal", 60.00, "monthly", "2026-10-01", A1),
    ("Notify (whop.com)", "cook_group", "Membership", 40.00, "monthly", "2026-10-03", A2),
    ("CapSolver", "captcha", "Balance top-up", 20.00, "one_time", "2026-10-11", A2),
    ("Vultr", "server", "Chicago server", 24.00, "monthly", "2026-10-02", A1),
    ("Oculus Proxies", "proxies", "5 GB residential data", 40.00, "one_time", "2026-09-21", A1),
]
INBOX = [
    (A1, "Nike <nike@official.nike.com>", "Your order is on its way", "2026-10-19T14:02:00", "in_transit"),
    (A2, "Best Buy <BestBuyInfo@emailinfo.bestbuy.com>", "Your package is out for delivery", "2026-10-22T08:11:00", "out_for_delivery"),
    (A2, "Target <orders@oe.target.com>", "Thanks for your order #912004578833", "2026-10-20T19:40:00", "ordered"),
    (A1, "Oculus Proxies <billing@oculusproxies.com>", "Your receipt from Oculus Proxies", "2026-10-19T10:15:00", "botting_expense"),
    (A1, "Walmart <help@walmart.com>", "Your order has been canceled", "2026-10-10T07:30:00", "cancelled"),
    (A2, "Notify <no-reply@whop.com>", "Payment received: Notify membership", "2026-10-03T12:00:00", "botting_expense"),
    (A1, "Foot Locker <footlocker@em.footlocker.com>", "Delivered: your New Balance 9060", "2026-10-17T16:20:00", "delivered"),
    (A2, "Kith <hello@kith.com>", "Order KITH-55120 shipped", "2026-10-06T09:00:00", "in_transit"),
]
PROFILES = [
    ("Target 1", "Target", "Alex", "Rivera", "Valor"), ("Target 2", "Target", "Alex", "Rivera", "Valor"),
    ("Nike Main", "Nike", "Sam", "Taylor", "Stellar"), ("Nike Alt", "Nike", "Sam", "Taylor", "Stellar"),
    ("Walmart 1", "", "Jordan", "Lee", "Hayha"),
]


def seed(path):
    c = db.connect(path)
    store = Store(c)
    for i, (retailer, number, day, items, total, tracking, carrier, status, eta, acct) in enumerate(ORDERS):
        email = Email(f"<order{i}@demo>", f"{retailer} <orders@demo>", "Order", day + "T12:00:00", "", acct)
        store.save(email, {"kind": "order", "retailer": retailer, "order_number": number, "order_date": day,
                           "items": [{"name": n, "quantity": q, "price": None} for n, q in items], "total": total,
                           "currency": "USD", "shipping_address": "123 Demo St, Austin, TX 78701",
                           "tracking_number": tracking, "carrier": carrier, "shipping_status": status,
                           "estimated_delivery": eta})
    for i, (vendor, cat, desc, amount, period, day, acct) in enumerate(EXPENSES):
        email = Email(f"<exp{i}@demo>", f"{vendor} <billing@demo>", "Receipt", day + "T12:00:00", "", acct)
        store.save(email, {"kind": "botting_expense", "vendor": vendor, "category": cat, "description": desc,
                           "amount": amount, "currency": "USD", "date": day, "order_number": None,
                           "billing_period": period})
    for acct in (A1, A2):
        store.mark_synced(acct, "2026-10-22T08:30:00")
    store.set_inbox_label({"account": A1, "message_id": "<inbox6@demo>", "sender": INBOX[6][1],
                           "subject": INBOX[6][2], "date": INBOX[6][3]}, "delivered", "learned")
    for i, (acct, sender, subject, day, kind) in enumerate(INBOX):
        email = Email(f"<inbox{i}@demo>", sender, subject, day, "", acct)
        store.record_email(email, "botting_expense" if kind == "botting_expense" else "order")
        if i in (0, 1, 2, 4, 6):
            right = {"kind": "order", "shipping_status": kind}
            model = dict(right, shipping_status="ordered") if i == 6 else right
            store.add_inbox_example(email, "", model, right, kind)
    c.execute("INSERT INTO ai_tasks (name, instructions, answer, created_at) VALUES (?,?,?,?)",
              ("Read Discord checkouts", "Read a bot's checkout message and give the site, product, size and price.",
               "json", "2026-10-20"))
    drops = [("Nike", "Air Jordan 1 Retro High OG", "10", 180), ("Target", "Pokemon TCG Elite Trainer Box", "", 49.99),
             ("Best Buy", "PlayStation 5 Pro", "", 699.99), ("Walmart", "Funko Pop! Exclusive", "", 14.88)] * 3
    tasks.add_examples(c, 1, [(f"Successful checkout! Site: {site} | Product: {prod} | Size: {size or '-'} | Price: ${price}",
                               json.dumps({"site": site, "product": prod, "size": size or None, "price": price}))
                              for site, prod, size, price in drops])
    tasks.save_adapter(c, "task-1", {"base": "mlx-community/Qwen3-4B-abliterated-4bit", "path": "models/adapters/task-1-demo",
                                     "examples": 12, "iters": 60, "loss": 0.214})
    checkouts = [("Valor", "success", "", "Nike", "Air Jordan 1 Retro High OG", "10", "Nike Main"),
                 ("Valor", "decline", "Card declined", "Target", "Pokemon TCG Elite Trainer Box", "", "Target 2"),
                 ("Stellar", "success", "", "Best Buy", "PlayStation 5 Pro", "", "Nike Alt"),
                 ("Hayha", "failure", "Address rejected", "Walmart", "Funko Pop! Exclusive", "", "Walmart 1"),
                 ("Valor", "success", "", "Target", "Pokemon TCG Elite Trainer Box", "", "Target 1")]
    for i, (bot, status, reason, site, product, size, profile) in enumerate(checkouts):
        c.execute("INSERT INTO checkouts (fingerprint, bot, status, reason, site, product, size, price, profile,"
                  " order_number, email, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"demo{i}", bot, status, reason, site, product, size, "", profile, "", "",
                   f"2026-10-{18 - i:02d}T15:0{i}:00"))
    c.commit()
    wallet.add_cards(c, wallet.parse_lines("Main:4111111111111111:04/2029:123\nBackup:5555555555554444:11/2028:456\n"
                                           "Travel:378282246310005:07/2030:1234")[0], "Chase", "Virtual", 500, "Main")
    cards = [r[0] for r in c.execute("SELECT id FROM cards ORDER BY id")]
    for i, (name, group, first, last, bot) in enumerate(PROFILES):
        form = {"name": name, "group_name": group, "first_name": first, "last_name": last,
                "email": f"alex.rivera+{i + 1}@example.com" if i < 2 else f"{first.lower()}.{i}@example.com", "phone": "5125550100",
                "bill_address1": f"{100 + i} Demo St", "bill_city": "Austin", "bill_state": "TX", "bill_zip": "78701",
                "bill_country": "US", "same": "on", "card_holder": f"{first} {last}",
                "card_id": str(cards[i]) if i < len(cards) else ""}
        profilebook.save_profile(c, form)
        c.execute("UPDATE profiles SET bot = ? WHERE name = ?", (bot, name))
    c.execute("UPDATE profiles SET bill_address1 = '9 Demo Street Apt', bill_zip = '7870' WHERE name = 'Walmart 1'")
    c.commit()
    emailbook.add(c, "Gmail dots", [f"alex.rivera+{n}@example.com" for n in range(1, 7)])
    emailbook.add(c, "Catchall", [f"drop{n}@demo-catchall.com" for n in range(1, 5)])
    c.commit()
    if has_module("bank"):  # the Bank page, once it's in the app
        from jigstream import bank
        rows = ["Transaction Date,Description,Amount", "10/18/2026,NIKE.COM ORDER,-360.00",
                "10/19/2026,OCULUS PROXIES,-75.00", "10/15/2026,FOOT LOCKER 0412,-149.99",
                "10/12/2026,BEST BUY 00012345,-699.99", "10/20/2026,TARGET.COM,-219.96",
                "10/02/2026,VULTR HOLDINGS,-24.00", "10/14/2026,HEB GROCERY,-86.41", "10/16/2026,DIGITALOCEAN,-12.00",
                "10/04/2026,WHOP*NOTIFY,-41.20"]
        bank.import_file(c, "activity.csv", "\n".join(rows).encode(), "Chase Sapphire 1234")
        bank.auto_match(c)
    logs = WORK / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "app.log").write_text(
        "2026-10-22 08:30:01 INFO    jigstream.sync: sneakers.demo@gmail.com: 14 new emails\n"
        "2026-10-22 08:30:09 INFO    jigstream.sync: Nike C01234567 -> in_transit (UPS 1Z999AA10123456784)\n"
        "2026-10-22 08:31:40 INFO    jigstream.sync: restock.demo@gmail.com: 9 new emails\n"
        "2026-10-22 08:32:02 INFO    jigstream.sync: Oculus Proxies receipt -> expense (proxies, $75.00)\n"
        "2026-10-22 08:33:15 INFO    jigstream.sync: Sync finished: 6 orders, 2 expenses, 15 skipped\n")


SAMPLE_PAGE = """<html><head><title>Air Jordan 1 Retro High OG 'Chicago' Men's Shoes | Demo Sneaker Store</title>
<meta name="description" content="The Air Jordan 1 Retro High OG Chicago brings back the classic red, white and black colorway.">
<script type="application/ld+json">{"@type": "Product", "name": "Air Jordan 1 Retro High OG 'Chicago'", "brand": "Jordan",
"color": "Varsity Red/White/Black", "sku": "DZ5485-612"}</script></head><body>
<h1>Air Jordan 1 Retro High OG 'Chicago'</h1><h2>Men's Shoes</h2>
<p>The Air Jordan 1 Retro High OG Chicago returns in its original red, white and black colorway. Premium leather, an
Air sole unit and the classic Wings logo. Style: DZ5485-612. The Air Jordan 1 Retro High OG is a Jordan classic.</p>
<p>Air Jordan 1 Retro High OG Chicago, men's sizing. Leather upper. Jordan Wings logo on the collar.</p>
<h2>You might also like</h2><ul>
<li><a href="/p/1">Air Jordan 1 Low 'Chicago'</a></li><li><a href="/p/2">Air Jordan 1 Mid 'Chicago' Big Kids' Shoes</a></li>
<li><a href="/p/3">Air Jordan 1 Retro High OG 'Chicago' Toddler</a></li><li><a href="/p/4">Air Jordan 1 Retro High OG GS</a></li>
</ul></body></html>"""


class FakeDiscord:
    def __init__(self, token):
        pass

    def me(self):
        return {"username": "BotTrack Reader", "id": "999"}

    def channels(self):
        return [("111", "My cook group", "valor-success"), ("112", "My cook group", "stellar-checkouts"),
                ("113", "My cook group", "general")]


def patch():
    rows = [{"account": a, "message_id": f"<inbox{i}@demo>", "sender": s, "subject": subj, "date": d,
             "spam": i == 5} for i, (a, s, subj, d, _) in enumerate(INBOX)]
    inbox.search = lambda config, query, limit=50: (rows, [])
    body = ("Hi Alex,\n\nGood news: your order C01234567 has shipped.\n\nAir Jordan 1 Retro High OG  x2   $360.00\n\n"
            "Carrier: UPS\nTracking number: 1Z999AA10123456784\nEstimated delivery: Fri, Oct 24\n\n"
            "Shipping to:\n123 Demo St\nAustin, TX 78701\n\nThanks for shopping with us.")
    inbox.open_email = lambda config, account, mid: Email(mid, INBOX[0][1], INBOX[0][2], INBOX[0][3], body, account)
    appmod.installed_models = lambda url, timeout=5: list(engines.OLLAMA_CANDIDATES)
    engines.mlx_available = lambda: True
    engines.mlx_choices = lambda: ["mlx-community/Qwen3-8B-abliterated-4bit"]
    finetune.base_choices = lambda: ["mlx-community/Qwen3-4B-abliterated-4bit", "mlx-community/Qwen3-8B-abliterated-4bit"]
    engines.compare_state.update({"finished_at": "2026-10-21 21:40", "results": [
        {"model": "huihui_ai/qwen3-abliterated:14b", "engine": "ollama", "accuracy": 93, "right": 28, "total": 30,
         "unreadable": 0, "seconds": 6.1},
        {"model": "huihui_ai/qwen3-abliterated:8b", "engine": "ollama", "accuracy": 87, "right": 26, "total": 30,
         "unreadable": 0, "seconds": 3.4},
        {"model": "mlx-community/Qwen3-8B-abliterated-4bit", "engine": "mlx", "accuracy": 87, "right": 26,
         "total": 30, "unreadable": 1, "seconds": 2.2}]})
    discord.DiscordClient = FakeDiscord
    if has_module("stock"):
        from jigstream import stock
        stores = [("Austin Downtown", "1124", "415 Congress Ave, Austin, TX", 1.2, 6), ("Austin North", "2211", "9900 Lamar Blvd, Austin, TX", 8.4, 2),
                  ("Round Rock", "1797", "301 Louis Henna Blvd, Round Rock, TX", 17.9, 0), ("Pflugerville", "2350", "1601 Pecan St, Pflugerville, TX", 15.3, 1),
                  ("Cedar Park", "1423", "1335 E Whitestone Blvd, Cedar Park, TX", 19.6, 0)]
        stock.CHECKERS["target"] = lambda product, zip_code, radius: {
            "retailer": "target", "product_id": "89238122", "title": "Pokemon TCG Elite Trainer Box",
            "link": "https://www.target.com/p/-/A-89238122", "quantities": True, "only_in_stock": False,
            "stores": sorted([{"store_id": i, "name": n, "address": a, "distance": d, "quantity": q, "in_stock": q > 0,
                               "note": "Limited stock" if q == 1 else "", "status": ""} for n, i, a, d, q in stores],
                             key=lambda r: (not r["in_stock"], r["distance"]))}
    if has_module("keywords"):
        from jigstream import keywords
        keywords.fetch = lambda url: (url, SAMPLE_PAGE)
    updater.installed_version = lambda app_dir=None: "build-42-ed618bf"
    updater.can_self_update = lambda app_dir=None: True
    updater.backups = lambda app_dir=None: ["previous"]
    updater.state.update({"checked_at": "2026-10-22T09:00:00", "available": False})


# Each shot: (name, page path, [(marker target, number)], options). A target is a CSS selector, or
# "TAG@text" for the first TAG whose text contains text (handy for section headings).
SHOTS = []


def shot(name, path, marks=(), **opts):
    SHOTS.append((name, path, marks, opts))


MARK_JS = """([marks, scrollTo, tight]) => {
  const find = sel => {
    const m = sel.match(/^(\\w+)@(.+)$/);
    if (m) return [...document.querySelectorAll(m[1])].find(e => e.textContent.includes(m[2]));
    return document.querySelector(sel);
  };
  if (scrollTo) { const t = find(scrollTo); if (t) scrollTo_(t); }
  function scrollTo_(t) { window.scrollTo(0, t.getBoundingClientRect().top + scrollY - 76); }
  document.querySelectorAll('.mk-ring,.mk-num').forEach(e => e.remove());
  marks.forEach(([sel, n]) => {
    const el = find(sel);
    if (!el) { console.log('missing marker target ' + sel); return; }
    const r = el.getBoundingClientRect(), host = el.closest('dialog[open]') || document.body;
    const fixed = host !== document.body, sx = fixed ? 0 : scrollX, sy = fixed ? 0 : scrollY, pos = fixed ? 'fixed' : 'absolute';
    const pad = tight ? 1 : 5, ring = document.createElement('div'); ring.className = 'mk-ring';
    ring.style.cssText = `position:${pos};left:${r.left + sx - pad}px;top:${r.top + sy - pad}px;width:${r.width + 2 * pad}px;`
      + `height:${r.height + 2 * pad}px;border:3px solid #ff8a3d;border-radius:12px;box-shadow:0 0 0 4px rgba(255,138,61,.22);`
      + 'pointer-events:none;z-index:9998;margin:0';
    const num = document.createElement('div'); num.className = 'mk-num'; num.textContent = n;
    const nx = tight ? r.right + sx + 8 : Math.max(2, r.left + sx - 17), ny = tight ? r.top + sy + r.height / 2 - 13 : Math.max(2, r.top + sy - 17);
    num.style.cssText = `position:${pos};left:${nx}px;top:${ny}px;`
      + 'width:26px;height:26px;border-radius:50%;background:#ff8a3d;color:#0b1436;font:700 14px/26px Inter,sans-serif;'
      + 'text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.5);z-index:9999;pointer-events:none;margin:0';
    host.append(ring, num);
  });
}"""

DESKTOP = {"width": 1360, "height": 860}
PHONE = {"width": 390, "height": 844}


def capture(app):
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        desktop = browser.new_page(viewport=DESKTOP)
        phone = browser.new_page(viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True)
        for page in (desktop, phone):
            page.on("console", lambda m: print("  ", m.text) if "missing" in m.text else None)
            page.on("dialog", lambda d: d.dismiss())
        only = set(sys.argv[1:])
        for name, path, marks, o in SHOTS:
            if only and name not in only:
                continue
            if o.get("needs") and not o["needs"]():
                print("skipped", name)
                continue
            page = phone if o.get("phone") else desktop
            page.set_viewport_size(o.get("viewport") or (PHONE if o.get("phone") else DESKTOP))
            if o.get("page_setup"):
                o["page_setup"](page)
            if o.get("server"):
                o["server"](app)
            try:
                page.goto(BASE + path)
                page.wait_for_load_state("networkidle")
                if o.get("js"):
                    page.evaluate(o["js"])
                    page.wait_for_timeout(600)
                    page.wait_for_load_state("networkidle")
                page.evaluate(MARK_JS, [[list(m) for m in marks], o.get("scroll"), o.get("tight", False)])
                page.wait_for_timeout(100)
                clip = o.get("clip")
                if o.get("clip_to"):  # crop to one element (a dialog), with room for the markers
                    box = page.locator(o["clip_to"]).bounding_box()
                    vp = page.viewport_size
                    x, y = max(0, box["x"] - 24), max(0, box["y"] - 24)
                    clip = {"x": x, "y": y, "width": min(vp["width"] - x, box["width"] + 48),
                            "height": min(vp["height"] - y, box["height"] + 48)}
                page.screenshot(path=str(OUT / f"{name}.png"), full_page=o.get("full", False), clip=clip)
                print("saved", name)
            finally:
                if o.get("reset"):
                    o["reset"](app)
        browser.close()


def ai_loading(app):
    app.config["AI_STATE"] = {"ready": False, "error": None, "message": "Downloading the AI model", "percent": 42}


def update_ready(app):
    updater.state.update({"available": True, "message": "Faster Inbox search and a new Manual tab"})


def update_reset(app):
    updater.state.update({"available": False, "message": ""})


def has_module(name):
    import importlib.util
    return importlib.util.find_spec(f"jigstream.{name}") is not None


def has_phone():
    return has_module("mobile")


def has_bank():
    return has_module("bank")


def phone_login(page):
    page.goto(BASE + "/m/login")
    page.fill("input[name=email]", PHONE_EMAIL)
    page.fill("input[name=password]", PHONE_PASSWORD)
    page.click("button")
    page.wait_for_load_state("networkidle")


TOP = {"x": 0, "y": 0, "width": 1360, "height": 150}
shot("nav", "/orders", [(".side a.it[href='/']", 1), (".side a[href='/orders']", 2), (".side a[href='/inbox']", 3),
                        (".side a[href='/expenses']", 4), (".side a[href='/bank']", 5), (".side a[href='/training']", 6),
                        (".side a[href='/checkouts']", 7), (".side a[href='/profiles']", 8), (".side a[href='/cards']", 9),
                        (".side a[href='/emails']", 10), (".aicard", 11), (".side a[href='/manual']", 12),
                        (".side a[href='/logs']", 13), (".side a[href='/setup']", 14), (".collapse", 15), (".crumb", 16)],
     tight=True, clip={"x": 0, "y": 0, "width": 560, "height": 860})
shot("ai-banner", "/orders", [("#aibar", 1)], server=ai_loading, reset=lambda app: app.config.pop("AI_STATE", None), clip=TOP)
shot("update-banner", "/orders", [(".top .chip.upd", 1), (".banner button", 2)], server=update_ready, reset=update_reset, clip=TOP)
shot("dashboard", "/", [(".stats", 1), ("form[action='/upload/webhooks']", 2), ("form[action='/upload/profiles']", 3),
                        ("main > table", 4)], full=True)
shot("orders", "/orders", [(".stats", 1), ("form[action='/sync/gmail'] button", 2),
                           ("form.ghost[action='/sync/gmail'] button", 3), ("#q", 4), ("#f", 5),
                           ("tbody tr:nth-child(2) td:nth-child(5)", 6), ("tbody tr:nth-child(2) a.track", 7)])
shot("inbox", "/inbox", [(".chips a", 1), (".chips input[name=q]", 2), (".mrow input", 3), (".labelbar", 4),
                         ("#reader .qlabels", 5), (".lbl-mine", 6)],
     js="document.querySelector('.mrow[data-id]').click()")
shot("expenses", "/expenses", [(".xtabs", 1), (".stats .card:first-child", 2), (".chart", 3), (".look", 4),
                               ("button[onclick='openExpense()']", 5)], viewport={"width": 1360, "height": 1000})
shot("expense-ledger", "/expenses/ledger", [(".ftabs", 1), ("#mf", 2), ("main > p.muted.m", 3), (".st-close", 4),
                                            ("select[name=method]", 5)], viewport={"width": 1360, "height": 1420})
shot("checkouts", "/checkouts", [("main > p", 1), ("tbody tr:nth-child(3) td:nth-child(3), tr:nth-child(3) td:nth-child(3)", 2),
                                 ("tr:nth-child(3) td:last-child", 3)])
shot("profiles", "/profiles", [(".gside", 1), ("button[onclick='openProfile()']", 2), ("button[onclick*=exportdlg]", 3),
                               (".toolbar", 4), ("button[onclick='openDistribute()']", 5), (".pcard a.edit", 6),
                               (".pcard.warn .issues", 7)], viewport={"width": 1360, "height": 1150})
shot("profile-edit", "/profiles", [("#grpsel", 1), ("#savedsel", 2), ("label:has(> #same)", 3), ("#profdlg select[name=card_id]", 4)],
     js="openProfile(2)", viewport={"width": 1360, "height": 1700}, clip_to="#profdlg")
shot("mass-distribute", "/profiles", [("#distdlg .fields", 1), ("#emailfill", 2), ("#distform textarea", 3),
                                      ("label:has(> input[name=billing])", 4), ("#distdlg .df button:last-child", 5)],
     js="""document.querySelectorAll('input.pick').forEach((c, i) => { if (i < 3) { c.checked = true; } }); counted();
           openDistribute(); document.getElementById('emailgroup').value = 'Gmail dots'; fillEmails();""",
     clip_to="#distdlg")
shot("export", "/profiles", [("#exportdlg select[name=bot]", 1), ("#exportdlg input[type=file]", 2),
                             ("#exportdlg .df button:last-child", 3)],
     js="document.getElementById('exportdlg').showModal()", clip_to="#exportdlg")
shot("cards", "/cards", [(".gside", 1), ("button[onclick*=adddlg]", 2), ("tbody select", 3),
                         ("button[title^='Show the full']", 4), ("tbody tr:first-child td:nth-last-child(2)", 5)],
     viewport={"width": 1560, "height": 760})
shot("cards-add", "/cards", [("#adddlg select", 1), ("#adddlg textarea", 2), ("#addbtn", 3)],
     js="""document.getElementById('adddlg').showModal(); const t = document.querySelector('#adddlg textarea');
           t.value = 'Main:4111111111111111:04/2029:123\\nBackup:5555555555554444:11/2028:456';
           t.dispatchEvent(new Event('input'));""", clip_to="#adddlg")
shot("emails", "/emails?group=Gmail%20dots", [(".gside", 1), ("button[onclick*=adddlg]", 2), ("a[href^='/emails/export']", 3),
                           ("tr:nth-child(5) td:nth-last-child(2)", 4)])
shot("setup-updates", "/setup", [("form[action='/update/check'] button", 1), ("form[action='/update/install'] button", 2),
                                 ("form[action='/update/rollback'] button", 3)],
     scroll="h3@Updates")
shot("setup-inboxes", "/setup", [("table:has(form[action='/setup/inbox/remove'])", 1), ("form[action='/setup/inbox'] input[name=address], form[action='/setup/inbox']", 2),
                                 ("form[action='/setup/inbox/remove'] button", 3)], scroll="h3@Gmail inboxes")
shot("setup-tracking", "/setup", [("summary@How to get a free USPS", 1), ("form[action='/setup/carriers'] button", 2)],
     scroll="h3@Live package tracking")
shot("setup-discord", "/setup", [("a[href*='discord.com/oauth2'], a[href*='discord.com/api/oauth2']", 1),
                                 ("form[action='/setup/discord/channels'] table", 2),
                                 ("form[action='/setup/discord/channels'] button", 3)], scroll="h3@Discord")
shot("setup-ai", "/setup", [("form[action='/setup/training']", 1), ("form[action='/setup/model'] select", 2),
                            ("form[action='/setup/model'] button", 3)], scroll="h3@Training mode")
shot("setup-compare", "/setup", [("form[action='/setup/compare']", 1), ("td@Best", 2),
                                 ("form[action='/setup/engine'] button", 3)], scroll="h3@Compare models")
shot("training", "/training", [(".tk-grid a.tk", 1), (".tk-grid a.tk + a.tk", 2), (".tk-new", 3)])
shot("training-task", "/training/tasks/1", [("form[action$='/try']", 1), ("#examples form[action$='/examples']", 2),
                                            ("#examples form[action$='/import']", 3), ("#run", 4), ("#train select", 5),
                                            ("#train table", 6)], full=True)
shot("training-email", "/training/email", [("a[href='/training/export'] button", 1), ("main > table tbody tr td:nth-child(2)", 2),
                                           ("main > table tbody tr td:nth-child(3)", 3), ("main > table tbody tr button.danger", 4),
                                           ("#train", 5)])
shot("logs", "/logs", [("main > p:nth-of-type(2) a", 1), ("a[href='/logs/download'] button", 2), ("main pre", 3)])
shot("bank", "/bank", [(".stats", 1), ("form[action='/bank/import']", 2), ("a[href='/bank/plaid']", 3),
                       (".tabs", 4), ("tbody tr td:nth-child(5)", 5), ("tbody tr .acts", 6)],
     viewport={"width": 1360, "height": 1200}, needs=has_bank)
shot("bank-plaid", "/bank/plaid", [(".panel:has(select[name=env])", 1), ("select[name=env]", 2)], needs=has_bank)
shot("keywords", "/keywords", [("input[name=url]", 1), ("label:has(> input[name=ai])", 2), ("#kwline", 3),
                               ("table.kw-table", 4), (".kw-grid .panel", 5), (".kw-grid .panel + .panel", 6),
                               (".kw-grid .panel + .panel + .panel", 7)],
     js="document.querySelector('input[name=url]').value = 'https://demo-sneakers.example/air-jordan-1-chicago';"
        "document.querySelector('form.kw-form').submit()", viewport={"width": 1360, "height": 1250}, needs=lambda: has_module("keywords"))
shot("stock", "/stock?run=1&retailer=target&product=https%3A%2F%2Fwww.target.com%2Fp%2F-%2FA-89238122&zip=78701&radius=25",
     [("input[name=product]", 1), ("select[name=retailer]", 2), ("input[name=zip]", 3), ("select[name=radius]", 4),
      ("table.st-table", 5), ("#recheck", 6), ("form[action='/stock/bestbuy-key']", 7)],
     viewport={"width": 1360, "height": 1100}, needs=lambda: has_module("stock"))
shot("setup-phone", "/setup", [("input[name=on]", 1), ("input[name=email][type=email]", 2),
                               ("input[name=allow_lan]", 3)], scroll="h3@Phone access", needs=has_phone)
shot("phone-login", "/m/login", phone=True, needs=has_phone)
shot("phone-orders", "/m/orders", phone=True, needs=has_phone, page_setup=phone_login)
shot("phone-expenses", "/m/expenses", phone=True, needs=has_phone)
shot("phone-profiles", "/m/profiles", phone=True, needs=has_phone)


PHONE_EMAIL, PHONE_PASSWORD = "demo@example.com", "demo-password-123"


def main():
    if has_phone():
        from werkzeug.security import generate_password_hash
        os.environ.update({"PHONE_ACCESS": "1", "PHONE_EMAIL": PHONE_EMAIL,
                           "PHONE_PASSWORD_HASH": generate_password_hash(PHONE_PASSWORD)})
    patch()
    path = str(WORK / "demo.db")
    seed(path)
    app = appmod.create_app(path)
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    threading.Thread(target=lambda: app.run(port=PORT, use_reloader=False), daemon=True).start()
    import time
    time.sleep(1.5)
    capture(app)


if __name__ == "__main__":
    main()
