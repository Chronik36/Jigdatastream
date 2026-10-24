"""Proxies: your proxy lists, how fast each one reaches the retail sites, and where it really comes out.

Paste a list one proxy per line (host:port, host:port:user:pass, user:pass@host:port or a URL), with the
provider and a group name. A speed test opens one retail site's home page once through each proxy and times it;
tests run a few at a time and every proxy is tried once per site per test, nothing is retried or rotated.
The detail page asks ip-api.com (free, no key) where the proxy's exit IP is, through the proxy itself, and where
its entry host is. The logins stay in BotTrack's database on this Mac and are never shown in full.
"""
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from . import db

bp = Blueprint("proxies", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY, host TEXT, port INTEGER, user TEXT, password TEXT, scheme TEXT DEFAULT 'http',
    provider TEXT, grp TEXT, added_at TEXT,
    exit_ip TEXT, exit_geo TEXT, entry_ip TEXT, entry_geo TEXT, located_at TEXT,
    UNIQUE (host, port, user)
);
CREATE TABLE IF NOT EXISTS proxy_tests (
    proxy_id INTEGER, site TEXT, ms INTEGER, status INTEGER, result TEXT, error TEXT, tested_at TEXT,
    PRIMARY KEY (proxy_id, site)
);
"""

# The home page each test opens. Pick one in the dropdown, or "All sites".
SITES = {
    "Target": "https://www.target.com/", "Walmart": "https://www.walmart.com/", "Best Buy": "https://www.bestbuy.com/",
    "Amazon": "https://www.amazon.com/", "Pokemon Center": "https://www.pokemoncenter.com/",
    "Sam's Club": "https://www.samsclub.com/", "Costco": "https://www.costco.com/", "GameStop": "https://www.gamestop.com/",
    "Nike": "https://www.nike.com/", "Foot Locker": "https://www.footlocker.com/", "Shopify": "https://www.shopify.com/",
    "Google (baseline)": "https://www.google.com/generate_204",
}
TIMEOUT = 12
WORKERS = 8  # tests at once
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
GEO = "http://ip-api.com/json/{}?fields=status,message,query,country,countryCode,regionName,city,zip,lat,lon,timezone,isp,org,as,asname,mobile,proxy,hosting"

LINE = re.compile(r"^(?:(?P<scheme>https?|socks5h?)://)?(?:(?P<u1>[^:@\s]+):(?P<p1>[^@\s]+)@)?"
                  r"(?P<host>[A-Za-z0-9.-]+):(?P<port>\d{2,5})(?::(?P<u2>[^:\s]+):(?P<p2>\S+))?/?$")
USER_FIRST = re.compile(r"^(?P<u>[^:\s]+):(?P<p>[^:\s]+):(?P<host>[A-Za-z0-9-]+\.[A-Za-z0-9.-]+):(?P<port>\d{2,5})$")


def ensure(con):
    return db.setup_once(con, "proxies", lambda c: c.executescript(SCHEMA))


def now():
    return datetime.now().isoformat(timespec="seconds")


def parse_line(line):
    """One proxy from a pasted line: dict(host, port, user, password, scheme), or None when it can't be read."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    m = LINE.match(s)
    if m and ("." in m["host"] or m["host"] == "localhost"):
        return {"host": m["host"].lower(), "port": int(m["port"]), "scheme": (m["scheme"] or "http").lower(),
                "user": m["u1"] or m["u2"] or "", "password": m["p1"] or m["p2"] or ""}
    m = USER_FIRST.match(s)
    if m:
        return {"host": m["host"].lower(), "port": int(m["port"]), "scheme": "http", "user": m["u"], "password": m["p"]}
    return None


def add_list(con, text, provider="", grp=""):
    """(added, already there, lines that couldn't be read)."""
    ensure(con)
    added = dup = 0
    bad = []
    for line in (text or "").splitlines():
        p = parse_line(line)
        if p is None:
            if line.strip() and not line.strip().startswith("#"):
                bad.append(line.strip()[:40])
            continue
        cur = con.execute("INSERT OR IGNORE INTO proxies (host, port, user, password, scheme, provider, grp, added_at)"
                          " VALUES (?,?,?,?,?,?,?,?)", (p["host"], p["port"], p["user"], p["password"], p["scheme"],
                                                       provider.strip(), grp.strip(), now()))
        if cur.rowcount:
            added += 1
        else:
            dup += 1
    return added, dup, bad


def url_of(p):
    auth = f"{urllib.request.quote(p['user'], safe='')}:{urllib.request.quote(p['password'] or '', safe='')}@" if p["user"] else ""
    return f"{p['scheme'] if p['scheme'].startswith('http') else 'http'}://{auth}{p['host']}:{p['port']}"


def opener(p):
    if (p["scheme"] or "http").startswith("socks"):
        raise ValueError("SOCKS proxies can't be tested yet, only HTTP ones")
    u = url_of(p)
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": u, "https": u}))


def masked(p):
    """host:port with the login hidden: user•••• when there is one."""
    who = f" · {p['user'][:4]}••••" if p["user"] else ""
    return f"{p['host']}:{p['port']}{who}"


def speed(p, site, fetch=None):
    """Open the site's home page through the proxy once. dict(ms, status, result, error)."""
    url = SITES[site]
    start = time.monotonic()
    try:
        if fetch:
            status = fetch(p, url)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
            with opener(p).open(req, timeout=TIMEOUT) as r:
                r.read(2048)  # the first bytes are enough to time it
                status = r.status
        ms = int((time.monotonic() - start) * 1000)
        return {"ms": ms, "status": status, "result": "ok", "error": ""}
    except urllib.error.HTTPError as e:
        ms = int((time.monotonic() - start) * 1000)
        # The proxy worked and the site answered, but turned it away.
        result = "blocked" if e.code in (403, 429, 503) else ("login" if e.code == 407 else "error")
        return {"ms": ms, "status": e.code, "result": result,
                "error": "The proxy rejected the login" if e.code == 407 else f"The site answered {e.code}"}
    except Exception as e:  # timeouts, refused connections, bad hosts
        reason = getattr(e, "reason", e)
        msg = "Timed out" if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason) else str(reason)
        return {"ms": None, "status": None, "result": "dead", "error": msg[:160]}


def save_test(con, pid, site, r):
    con.execute("INSERT OR REPLACE INTO proxy_tests VALUES (?,?,?,?,?,?,?)",
                (pid, site, r["ms"], r["status"], r["result"], r["error"], now()))


def run_tests(con, ids, sites, state=None, fetch=None):
    """Test each proxy on each site, WORKERS at a time. state, when given, gets live counts for the page."""
    ensure(con)
    rows = [dict(r) for r in con.execute(f"SELECT * FROM proxies WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
    jobs = [(p, s) for p in rows for s in sites]
    if state is not None:
        state.update(total=len(jobs), done=0, running=True, started=now())
    lock = threading.Lock()

    def one(job):
        p, site = job
        r = speed(p, site, fetch)
        with lock:
            save_test(con, p["id"], site, r)
            if state is not None:
                state["done"] += 1
        return r

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(one, jobs))
    finally:
        if state is not None:
            state["running"] = False
    return results


def geo(ip_or_host, opener_=None, fetch=None):
    """ip-api.com's answer for an IP ("" asks about whoever is asking: the proxy's exit when sent through it)."""
    url = GEO.format(ip_or_host)
    if fetch:
        return fetch(url, opener_)
    o = opener_ or urllib.request.build_opener()
    with o.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def locate(con, pid, fetch=None):
    """Find the exit IP (through the proxy) and the entry host's location, and save them. Returns an error or None."""
    p = get(con, pid)
    try:
        exit_ = geo("", opener(p), fetch)
    except Exception as e:
        return f"Couldn't reach ip-api.com through this proxy: {getattr(e, 'reason', e)}"
    try:
        entry_ip = socket.gethostbyname(p["host"]) if not fetch else p["host"]
        entry = geo(entry_ip, None, fetch)
    except Exception:
        entry_ip, entry = "", {}
    con.execute("UPDATE proxies SET exit_ip=?, exit_geo=?, entry_ip=?, entry_geo=?, located_at=? WHERE id=?",
                (exit_.get("query", ""), json.dumps(exit_), entry_ip, json.dumps(entry), now(), pid))
    return None


def get(con, pid):
    ensure(con)
    r = con.execute("SELECT * FROM proxies WHERE id=?", (pid,)).fetchone()
    if not r:
        abort(404)
    return dict(r)


def place(g):
    """'Ashburn, Virginia, US' from an ip-api answer."""
    return ", ".join(x for x in (g.get("city"), g.get("regionName"), g.get("countryCode")) if x)


def kind(g):
    """What kind of IP it looks like to a site: residential, datacenter or mobile."""
    if not g:
        return ""
    if g.get("mobile"):
        return "Mobile"
    return "Datacenter" if g.get("hosting") else "Residential / ISP"


def groups(con):
    """[(group, provider set, proxies with their tests and averages)] sorted by group name."""
    ensure(con)
    tests = {}
    for t in con.execute("SELECT * FROM proxy_tests"):
        tests.setdefault(t["proxy_id"], {})[t["site"]] = dict(t)
    out = {}
    for r in con.execute("SELECT * FROM proxies ORDER BY grp, provider, id"):
        p = dict(r)
        p["tests"] = tests.get(p["id"], {})
        p["label"] = masked(p)
        good = [t["ms"] for t in p["tests"].values() if t["result"] == "ok" and t["ms"] is not None]
        p["avg"] = round(sum(good) / len(good)) if good else None
        p["exit"] = json.loads(p["exit_geo"]) if p["exit_geo"] else {}
        g = out.setdefault(p["grp"] or "No group", {"name": p["grp"] or "No group", "providers": set(), "proxies": []})
        g["providers"].add(p["provider"] or "?")
        g["proxies"].append(p)
    for g in out.values():
        res = [t for p in g["proxies"] for t in p["tests"].values()]
        oks = [t["ms"] for t in res if t["result"] == "ok" and t["ms"] is not None]
        g["avg"] = round(sum(oks) / len(oks)) if oks else None
        g["ok"] = sum(t["result"] == "ok" for t in res)
        g["tested"] = len(res)
    return sorted(out.values(), key=lambda g: (g["name"] == "No group", g["name"].lower()))


def checkout_record(con, p):
    """How this proxy did in your bots' checkout webhooks: {success, decline, failure}."""
    from .drop import proxy_of
    want = f"{p['host']}:{p['port']}"
    out = {"success": 0, "decline": 0, "failure": 0}
    for r in con.execute("SELECT status, raw FROM checkouts WHERE status IN ('success','decline','failure')"
                         " AND raw LIKE ?", (f"%{p['host']}%",)):
        if proxy_of(r["raw"])[1] == want:
            out[r["status"]] += 1
    return out


# ---- pages ----

_state = {"running": False, "total": 0, "done": 0}


def _con():
    return ensure(db.connect(current_app.config.get("DB_PATH")))


@bp.get("/proxies")
def page():
    con = _con()
    from . import proxyaccounts, proxylogin, proxysessions
    proxyaccounts.ensure(con)
    return render_template("proxies.html", groups=groups(con), sites=list(SITES), state=_state,
                           site=request.args.get("site", ""), accounts=proxyaccounts.accounts(con), auths=proxyaccounts.AUTHS,
                           presets=proxyaccounts.PRESETS, login=proxylogin.available(),
                           learning=request.args.get("learning", type=int), ttl_choices=proxysessions.TTLS,
                           ttl_words=proxysessions.ttl_words, shared_steps=len(proxysessions.shared(con)))


@bp.post("/proxies/add")
def add():
    added, dup, bad = add_list(_con(), request.form.get("list"), request.form.get("provider", ""), request.form.get("group", ""))
    msg = f"Added {added} prox{'y' if added == 1 else 'ies'}."
    if dup:
        msg += f" {dup} were already here."
    if bad:
        msg += f" Couldn't read {len(bad)} line{'s' if len(bad) > 1 else ''} (like \"{bad[0]}\")."
    flash(msg)
    return redirect(url_for("proxies.page"))


@bp.post("/proxies/test")
def test():
    """Test one proxy, a group or everything, on one site or all of them, in the background."""
    if _state.get("running"):
        flash("A speed test is already running.")
        return redirect(url_for("proxies.page"))
    con = _con()
    site = request.form.get("site", "")
    sites = list(SITES) if site in ("", "all") else [site] if site in SITES else []
    if not sites:
        abort(400)
    if request.form.get("id"):
        ids = [int(request.form["id"])]
    elif request.form.get("group") is not None:
        g = request.form["group"]
        ids = [r[0] for r in con.execute("SELECT id FROM proxies WHERE COALESCE(NULLIF(grp,''),'No group')=?", (g,))]
    else:
        ids = [r[0] for r in con.execute("SELECT id FROM proxies")]
    path = current_app.config.get("DB_PATH")
    if request.form.get("wait"):  # one proxy on one site: answer right away
        run_tests(con, ids, sites)
    else:
        _state.update(running=True, total=len(ids) * len(sites), done=0)
        threading.Thread(target=lambda: run_tests(ensure(db.connect(path)), ids, sites, _state), daemon=True).start()
    back = request.form.get("back")
    return redirect(back if back and back.startswith("/proxies") else url_for("proxies.page", site=site))


@bp.get("/proxies/status")
def status():
    return jsonify({k: _state.get(k) for k in ("running", "total", "done")})


@bp.get("/proxies/<int:pid>")
def detail(pid):
    con = _con()
    p = get(con, pid)
    p["label"] = masked(p)
    p["exit"] = json.loads(p["exit_geo"]) if p["exit_geo"] else {}
    p["entry"] = json.loads(p["entry_geo"]) if p["entry_geo"] else {}
    tests = {t["site"]: dict(t) for t in con.execute("SELECT * FROM proxy_tests WHERE proxy_id=?", (pid,))}
    from . import drop
    from .orderstore import Store
    Store(con)  # order columns the checkout list reads exist on any database
    want = f"{p['host']}:{p['port']}"
    used = sorted((c for c in drop.rows(con) if c["proxy"] == want), key=lambda c: c["ts"] or "", reverse=True)
    return render_template("proxy_detail.html", p=p, tests=tests, sites=list(SITES), place=place, kind=kind,
                           record=checkout_record(con, p), used=used, hostport=want)


@bp.get("/proxies/find")
def find():
    """Open a proxy's page from a checkout's host:port, when it's one of your saved proxies."""
    host, _, port = request.args.get("hp", "").rpartition(":")
    row = _con().execute("SELECT id FROM proxies WHERE host = ? AND port = ?", (host, port)).fetchone() if port.isdigit() else None
    if row:
        return redirect(url_for("proxies.detail", pid=row["id"]))
    flash(f"{request.args.get('hp', 'That proxy')} isn't in your saved proxy lists. Paste its list on Proxies to test it.")
    return redirect(url_for("drop_page", by="proxy", name=request.args.get("hp", "")))


@bp.post("/proxies/<int:pid>/locate")
def relocate(pid):
    err = locate(_con(), pid)
    if err:
        flash(err)
    return redirect(url_for("proxies.detail", pid=pid))


@bp.post("/proxies/<int:pid>/edit")
def edit(pid):
    con = _con()
    get(con, pid)
    con.execute("UPDATE proxies SET provider=?, grp=? WHERE id=?",
                (request.form.get("provider", "").strip(), request.form.get("group", "").strip(), pid))
    return redirect(url_for("proxies.detail", pid=pid))


@bp.post("/proxies/delete")
def delete():
    con = _con()
    if request.form.get("id"):
        ids = [int(request.form["id"])]
    else:
        g = request.form.get("group", "")
        ids = [r[0] for r in con.execute("SELECT id FROM proxies WHERE COALESCE(NULLIF(grp,''),'No group')=?", (g,))]
    for i in ids:
        con.execute("DELETE FROM proxy_tests WHERE proxy_id=?", (i,))
        con.execute("DELETE FROM proxies WHERE id=?", (i,))
    flash(f"Removed {len(ids)} prox{'y' if len(ids) == 1 else 'ies'}.")
    return redirect(url_for("proxies.page"))
