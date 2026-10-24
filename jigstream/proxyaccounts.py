"""Proxy provider accounts: data left on each plan and fresh proxy lists, from the providers' own APIs, in one place.

Providers don't share one API, so each account says where to ask:
- a usage address that answers with JSON, and which fields in it are the data used / left / total
  (dotted paths such as "data.traffic_left"), in GB, MB or bytes;
- a list address that answers with a proxy list (one per line, or a JSON list of them). Refresh pool replaces the
  account's group on the Proxies tab with what it returns. Providers that make lists on request take a JSON body,
  which is POSTed instead.
A "*" in a field path adds the numbers up across every plan ("packages.*.usage.gb_remaining").
The API key goes in the address as {key}, or in a header. It is kept in BotTrack's database on this Mac and is
only ever sent to the provider's own addresses. Presets fill in what's known for a provider; anything else can be
typed from the provider's API docs.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import db, proxies

bp = Blueprint("proxyaccounts", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS proxy_accounts (
    id INTEGER PRIMARY KEY, name TEXT, api_key TEXT, auth TEXT DEFAULT 'url',
    usage_url TEXT, used_path TEXT, left_path TEXT, total_path TEXT, unit TEXT DEFAULT 'gb',
    list_url TEXT, list_body TEXT, grp TEXT,
    used_gb REAL, left_gb REAL, total_gb REAL, usage_error TEXT, checked_at TEXT,
    pool_size INTEGER, pool_error TEXT, refreshed_at TEXT
);
"""
AUTHS = {"url": "In the address as {key}", "bearer": "Authorization: Bearer key", "x-api-key": "X-API-Key header",
         "token": "Authorization: Token key"}
UNITS = {"gb": 1, "mb": 1 / 1024, "bytes": 1 / 1024 ** 3}
LOW_GB = 1.0  # warn when a plan has less than this left
# Filled in from each provider's public API docs; picked in the Connect a provider window. Add more as they're checked.
PRESETS = {
    "Webshare": {"auth": "token", "unit": "bytes", "used_path": "bandwidth_total", "left_path": "", "total_path": "",
                 "usage_url": "https://proxy.webshare.io/api/v2/stats/aggregate/?timestamp__gte={month_start}&timestamp__lte={now}",
                 "list_url": "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page_size=100",
                 "help": "API key from Webshare's dashboard (API → Keys). Data used counts this month; Refresh pool loads up to 100 proxies."},
    "ResiFactory": {"auth": "bearer", "unit": "gb", "used_path": "", "left_path": "balances.*.gb", "total_path": "",
                    "usage_url": "https://resifactory.net/api/v1/balance",
                    "list_url": "https://resifactory.net/api/v1/proxies",
                    "list_body": '{"country": "us", "quantity": 100, "proxyType": "rotating", "format": "json"}',
                    "help": "API key (rf_live_...) from ResiFactory's dashboard with usage:read and proxies:generate. Data left adds up every pool; add \"pool\": \"your pool id\" to the list body to pick one."},
    "FlameProxies": {"auth": "bearer", "unit": "gb", "used_path": "packages.*.usage.gb_used",
                     "left_path": "packages.*.usage.gb_remaining", "total_path": "packages.*.usage.gb_total",
                     "usage_url": "https://flameproxies.com/api/customer/non-api-packages/usage",
                     "list_url": "https://flameproxies.com/api/customer/proxies/generate",
                     "list_body": '{"package_id": 0, "quantity": 100, "country": "US", "proxy_type": "sticky", "format": "standard"}',
                     "help": "API key (fp_live_...) from FlameProxies' dashboard (APIs). Put your package ID in place of 0 in the list body; it's on the dashboard next to the plan."},
    "Vantage Proxies": {"auth": "bearer", "unit": "gb", "used_path": "balances.*.used_gb",
                        "left_path": "balances.*.remaining_gb", "total_path": "balances.*.total_gb",
                        "usage_url": "https://vantageproxies.com/api/v1/balances",
                        "list_url": "https://vantageproxies.com/api/v1/endpoints",
                        "list_body": '{"tier": "apex_residential", "quantity": 100, "session": "sticky", "duration": 30, "country": "us"}',
                        "help": "API key from vantageproxies.com/dashboard/api. Data adds up every residential and mobile plan (ISP has no GB meter). Change tier in the list body to core_residential or mobile for those plans; needs a V2 plan."},
    "IPFist Residential": {"auth": "bearer", "unit": "gb", "used_path": "", "left_path": "", "total_path": "",
                           "usage_url": "https://ipfist.com/api/ProxyLogic/BandwidthAnalysis",
                           "list_url": "https://ipfist.com/api/ProxyLogic/Generate?mealType=basic&num=100&country=US&state=&city=&lifeTime=0&server=&format=0",
                           "help": "Residential API key (ak_...) from IPFist's dashboard. IPFist doesn't publish the balance's field names, so BotTrack finds the used / left / total numbers itself; if it can't, Check data shows the answer to fill the fields from. For the Premium plan, change mealType=basic to premium in the list address."},
    "IPRoyal Residential": {"auth": "bearer", "unit": "gb", "used_path": "", "left_path": "available_traffic", "total_path": "",
                            "usage_url": "https://resi-api.iproyal.com/v1/me", "list_url": "",
                            "help": "API token from IPRoyal's dashboard (Residential → API). Residential uses one gateway, so there's no list to refresh."},
}


def _setup(con):
    con.executescript(SCHEMA)
    if "list_body" not in {r[1] for r in con.execute("PRAGMA table_info(proxy_accounts)")}:
        con.execute("ALTER TABLE proxy_accounts ADD COLUMN list_body TEXT")


def ensure(con):
    proxies.ensure(con)
    return db.setup_once(con, "proxyaccounts", _setup)


def _con():
    return ensure(db.connect(current_app.config.get("DB_PATH")))


def now():
    return datetime.now().isoformat(timespec="seconds")


def fill_dates(url, today=None):
    """{month_start} and {now} in an address become ISO times (for APIs that count usage over a period)."""
    now = today or datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    start = now.replace(day=1, hour=0, minute=0, second=0)
    return url.replace("{month_start}", start.isoformat() + "Z").replace("{now}", now.isoformat() + "Z")


def _request(url, key, auth, body=None):
    url = fill_dates(url)
    headers = {"User-Agent": "BotTrack", "Accept": "application/json, text/plain, */*"}
    if body:
        headers["Content-Type"] = "application/json"
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif auth == "token":
        headers["Authorization"] = f"Token {key}"
    elif auth == "x-api-key":
        headers["X-API-Key"] = key
    return urllib.request.Request(url.replace("{key}", urllib.request.quote(key or "", safe="")), headers=headers,
                                  data=body.encode() if body else None, method="POST" if body else "GET")


def fetch(url, key, auth, opener=None, body=None):
    """The provider's answer as text (POSTing body as JSON when given). Errors read like the provider's, without the key."""
    if not (url or "").startswith("https://"):
        raise ValueError("The address must start with https://")
    try:
        with (opener or urllib.request.urlopen)(_request(url, key, auth, body), timeout=20) as r:
            return r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ValueError(f"The provider answered {e.code} {e.reason}"
                         + (" (check the API key)" if e.code in (401, 403) else "")) from None
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the provider: {e.reason}") from None


def dig(data, path):
    """A value from JSON by a dotted path ("data.0.left"); None when it isn't there. "*" sums the rest of the path
    over every item ("packages.*.usage.gb_used")."""
    parts = [p for p in (path or "").split(".") if p]
    for i, part in enumerate(parts):
        if part == "*":
            items = data.values() if isinstance(data, dict) else data if isinstance(data, list) else []
            found = [_gb(dig(x, ".".join(parts[i + 1:])), "gb") for x in items]
            found = [v for v in found if v is not None]
            return round(sum(found), 6) if found else None
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data


LEFT_WORDS = ("remain", "left", "surplus", "available", "balance", "rest")
USED_WORDS = ("used", "consum", "usage", "spent")
TOTAL_WORDS = ("total", "limit", "quota", "purchas", "capacity", "max")
NOT_DATA = ("price", "money", "amount", "currency", "cost", "day", "time", "count", "id", "expire", "percent", "rate")


def _unit_of(key, value):
    k = key.lower()
    if "byte" in k:
        return "bytes"
    if k.endswith("mb") or "_mb" in k or "mb_" in k:
        return "mb"
    if "kb" in k:
        return "kb"
    if "gb" in k:
        return "gb"
    return "bytes" if value > 1_000_000 else "gb"


def guess(data):
    """(used, left, total) in GB by field names, for providers whose answer isn't documented: the first number
    under a key like remaining / used / total. Money, counts and dates are skipped."""
    found = {}

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                    continue
                try:
                    n = float(v) if not isinstance(v, bool) else None
                except (TypeError, ValueError):
                    n = None
                kl = k.lower()
                if n is None or any(w in kl for w in NOT_DATA):
                    continue
                for role, words in (("left", LEFT_WORDS), ("used", USED_WORDS), ("total", TOTAL_WORDS)):
                    if role not in found and any(w in kl for w in words):
                        unit = _unit_of(k, n)
                        found[role] = round(n / 1024 ** 3 if unit == "bytes" else n / 1024 ** 2 if unit == "kb"
                                            else n / 1024 if unit == "mb" else n, 3)
                        break
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    return found.get("used"), found.get("left"), found.get("total")


def _gb(v, unit):
    try:
        return round(float(str(v).replace(",", "")) * UNITS.get(unit, 1), 3)
    except (TypeError, ValueError):
        return None


def check_usage(con, acct, opener=None):
    """Ask the provider how much data is used and left; saves and returns (used, left, total) in GB."""
    try:
        data = json.loads(fetch(acct["usage_url"], acct["api_key"], acct["auth"], opener))
        # Some providers answer HTTP 200 with their own {"code", "message", "body"} wrapper, so a failure hides in it.
        if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] not in (0, 200) and "message" in data:
            raise ValueError(f"The provider answered {data['code']}: {data['message']}")
        if any(acct[p] for p in ("used_path", "left_path", "total_path")):
            used, left, total = (_gb(dig(data, acct[p]), acct["unit"]) if acct[p] else None
                                 for p in ("used_path", "left_path", "total_path"))
        else:
            used, left, total = guess(data)
        if left is None and used is not None and total is not None:
            left = round(total - used, 3)
        if used is None and left is None:
            raise ValueError("The answer has no number at those fields: " + json.dumps(data, ensure_ascii=False)[:400])
        con.execute("UPDATE proxy_accounts SET used_gb=?, left_gb=?, total_gb=?, usage_error=NULL, checked_at=? WHERE id=?",
                    (used, left, total, now(), acct["id"]))
        con.commit()
        return used, left, total
    except (ValueError, json.JSONDecodeError) as e:
        msg = str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) else "The answer wasn't JSON"
        con.execute("UPDATE proxy_accounts SET usage_error=?, checked_at=? WHERE id=?", (msg[:300], now(), acct["id"]))
        con.commit()
        raise ValueError(msg) from None


def pool_lines(text):
    """Proxy lines from a list answer: plain lines, or JSON (a list of strings, or of {host, port, ...})."""
    s = text.strip()
    if s[:1] in "[{":
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):  # {"proxies": [...]}, or {"proxies": "line\nline"}
            data = next((v for v in data.values() if isinstance(v, list)), None) or next(
                (v.splitlines() for v in data.values() if isinstance(v, str) and v.count(":") >= 1 and "\n" in v), [])
        if isinstance(data, list):
            out = []
            for x in data:
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict) and (x.get("host") or x.get("ip") or x.get("proxy_address")) and x.get("port"):
                    if x.get("valid") is False:
                        continue
                    login = f":{x.get('username') or x.get('user')}:{x.get('password') or x.get('pass')}" if (x.get("username") or x.get("user")) else ""
                    out.append(f"{x.get('host') or x.get('ip') or x.get('proxy_address')}:{x['port']}{login}")
            return out
    return [line for line in s.splitlines() if line.strip()]


def refresh_pool(con, acct, opener=None):
    """Replace the account's group with the provider's current list. Returns how many proxies it has now."""
    group = acct["grp"] or acct["name"]
    try:
        body = (acct.get("list_body") or "").strip() or None
        text = fetch(acct["list_url"], acct["api_key"], acct["auth"], opener, body)
        try:
            wrapped = json.loads(text)
        except json.JSONDecodeError:
            wrapped = None
        if isinstance(wrapped, dict) and isinstance(wrapped.get("code"), int) and wrapped["code"] not in (0, 200) and "message" in wrapped:
            raise ValueError(f"The provider answered {wrapped['code']}: {wrapped['message']}")
        lines = pool_lines(text)
        if not [line for line in lines if proxies.parse_line(line)]:
            raise ValueError("The provider's list had no proxies BotTrack could read")
    except ValueError as e:
        con.execute("UPDATE proxy_accounts SET pool_error=?, refreshed_at=? WHERE id=?", (str(e)[:300], now(), acct["id"]))
        con.commit()
        raise
    old = [r[0] for r in con.execute("SELECT id FROM proxies WHERE grp = ? AND provider = ?", (group, acct["name"]))]
    con.executemany("DELETE FROM proxy_tests WHERE proxy_id = ?", [(i,) for i in old])
    con.execute("DELETE FROM proxies WHERE grp = ? AND provider = ?", (group, acct["name"]))
    added, dup, _ = proxies.add_list(con, "\n".join(lines), acct["name"], group)
    con.execute("UPDATE proxy_accounts SET pool_size=?, pool_error=NULL, refreshed_at=? WHERE id=?",
                (added + dup, now(), acct["id"]))
    con.commit()
    return added + dup


def accounts(con):
    out = []
    for r in con.execute("SELECT * FROM proxy_accounts ORDER BY name"):
        a = dict(r)
        a["key_hint"] = ("••••" + a["api_key"][-4:]) if a["api_key"] and len(a["api_key"]) > 6 else ("set" if a["api_key"] else "")
        a.pop("api_key")
        a["pct_used"] = round(100 * a["used_gb"] / a["total_gb"]) if a["used_gb"] is not None and a["total_gb"] else None
        a["low"] = a["left_gb"] is not None and a["left_gb"] < LOW_GB
        out.append(a)
    return out


FIELDS = ("name", "auth", "usage_url", "used_path", "left_path", "total_path", "unit", "list_url", "list_body", "grp")


@bp.post("/proxies/accounts/save")
def save():
    con = _con()
    f = {k: (request.form.get(k) or "").strip() for k in FIELDS}
    if not f["name"]:
        flash("Give the provider account a name.")
        return redirect(url_for("proxies.page") + "#accounts")
    f["auth"] = f["auth"] if f["auth"] in AUTHS else "url"
    f["unit"] = f["unit"] if f["unit"] in UNITS else "gb"
    for u in ("usage_url", "list_url"):
        if f[u] and not f[u].startswith("https://"):
            flash("Provider addresses must start with https://")
            return redirect(url_for("proxies.page") + "#accounts")
    if f["list_body"]:
        try:
            json.loads(f["list_body"])
        except json.JSONDecodeError:
            flash("The list body has to be JSON, like {\"quantity\": 100}.")
            return redirect(url_for("proxies.page") + "#accounts")
    key = (request.form.get("api_key") or "").strip()
    aid = request.form.get("id", type=int)
    if aid:
        con.execute(f"UPDATE proxy_accounts SET {', '.join(k + '=?' for k in FIELDS)} WHERE id=?", (*f.values(), aid))
        if key:  # left blank keeps the saved key
            con.execute("UPDATE proxy_accounts SET api_key=? WHERE id=?", (key, aid))
    else:
        con.execute(f"INSERT INTO proxy_accounts ({', '.join(FIELDS)}, api_key) VALUES ({', '.join('?' * (len(FIELDS) + 1))})",
                    (*f.values(), key))
    con.commit()
    flash(f"Saved {f['name']}.")
    return redirect(url_for("proxies.page") + "#accounts")


def _acct(con, aid):
    row = con.execute("SELECT * FROM proxy_accounts WHERE id=?", (aid,)).fetchone()
    return dict(row) if row else None


@bp.post("/proxies/accounts/<int:aid>/<action>")
def act(aid, action):
    con = _con()
    a = _acct(con, aid)
    if not a:
        return redirect(url_for("proxies.page"))
    try:
        if action == "usage":
            used, left, total = check_usage(con, a)
            flash(f"{a['name']}: " + ", ".join(f"{label} {v:g} GB" for label, v in (("used", used), ("left", left), ("of", total))
                                                if v is not None) + ".")
        elif action == "pool":
            n = refresh_pool(con, a)
            flash(f"{a['name']}: the {a['grp'] or a['name']} group now has {n} prox{'y' if n == 1 else 'ies'} from the provider.")
        elif action == "delete":
            con.execute("DELETE FROM proxy_accounts WHERE id=?", (aid,))
            con.commit()
            flash(f"Removed {a['name']}. Its proxies stay on the list.")
    except ValueError as e:
        flash(f"{a['name']}: {e}")
    return redirect(url_for("proxies.page") + "#accounts")


@bp.post("/proxies/accounts/usage-all")
def usage_all():
    con = _con()
    bad = []
    for a in con.execute("SELECT * FROM proxy_accounts WHERE COALESCE(usage_url, '') != ''").fetchall():
        try:
            check_usage(con, dict(a))
        except ValueError:
            bad.append(a["name"])
    flash("Checked data left on every account." + (f" Couldn't check {', '.join(bad)}." if bad else ""))
    return redirect(url_for("proxies.page") + "#accounts")
