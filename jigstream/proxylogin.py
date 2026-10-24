"""Fresh proxy lists from providers that only have a dashboard (Discord sign-in, no API).

Connect: BotTrack opens the provider's site in a normal window on this Mac and you sign in there yourself (with
Discord or however the site asks). Then generate a list once the way you always do. While you do, the window
notes what the page asks its own server for and which button you pressed; when an answer (or the page) holds
proxy lines, that's the provider's recipe:
- "request": the call the dashboard made to make the list. Refresh pool repeats it, from BotTrack with the
  sign-in the page sent (a token or the page's cookies) or, when the site wants the sign-in only the window has,
  inside the signed-in window;
- "click": the list is made in the page itself. Refresh pool opens the same page in the window, presses the
  same button and copies the lines it shows.
Staying signed in: when you press Use this, BotTrack keeps the site's sign-in (its cookies, the HttpOnly ones
included, and what the page keeps in its storage) encrypted with the same Keychain key as your cards. Refresh pool
sends them with the request, or puts them back into a window before using it, so you sign in again only when the
site itself ends the sign-in. Cookies the site renews are kept as they change. Only the Mac app has these windows.

Also here: Suggest a provider, which emails the developer a provider's site and docs so a later build can set it up.
"""
import json
import logging
import ssl
import smtplib
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, jsonify, redirect, request, url_for

from . import db, logs, proxies

logger = logging.getLogger("jigstream.proxylogin")
bp = Blueprint("proxylogin", __name__)

# Installed in the provider's page (again after each page change): notes the page's own requests and answers,
# and the buttons pressed. Nothing leaves the window except when BotTrack reads it back.
RECORD_JS = r"""(() => {
if (window.__btRec) return '1';
window.__btRec = true; window.__btReqs = []; window.__btClicks = [];
const keep = r => { window.__btReqs.push(r); if (window.__btReqs.length > 40) window.__btReqs.shift(); };
const hdrs = h => { const o = {}; try { if (!h) return o; if (h instanceof Headers) h.forEach((v, k) => o[k] = v);
  else if (Array.isArray(h)) h.forEach(([k, v]) => o[k] = v); else Object.assign(o, h); } catch (e) {} return o; };
const body = b => typeof b === 'string' ? b : (b instanceof URLSearchParams ? b.toString() : (b == null ? null : '[binary]'));
const of = window.fetch;
if (of) window.fetch = function (input, init) {
  init = init || {};
  const url = typeof input === 'string' ? input : (input && input.url) || String(input);
  const rec = {url: new URL(url, location.href).href, method: (init.method || (input && input.method) || 'GET').toUpperCase(),
               headers: hdrs(init.headers || (input && input.headers)), body: body(init.body), page: location.href, at: Date.now()};
  const p = of.apply(this, arguments);
  p.then(res => { rec.status = res.status; res.clone().text().then(t => { rec.text = t.slice(0, 300000); keep(rec); }).catch(() => {}); }).catch(() => {});
  return p;
};
const XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send, XH = XMLHttpRequest.prototype.setRequestHeader;
XMLHttpRequest.prototype.open = function (m, u) {
  this.__bt = {method: String(m).toUpperCase(), url: new URL(u, location.href).href, headers: {}, page: location.href};
  return XO.apply(this, arguments); };
XMLHttpRequest.prototype.setRequestHeader = function (k, v) { if (this.__bt) this.__bt.headers[k] = v; return XH.apply(this, arguments); };
XMLHttpRequest.prototype.send = function (b) {
  const r = this.__bt;
  if (r) { r.body = body(b); r.at = Date.now();
    this.addEventListener('load', () => { try { r.status = this.status;
      r.text = (typeof this.response === 'string' ? this.response : JSON.stringify(this.response) || '').slice(0, 300000); keep(r); } catch (e) {} }); }
  return XS.apply(this, arguments); };
document.addEventListener('click', e => {
  const c = e.target.closest('button,[role=button],a,input[type=submit],input[type=button]');
  if (!c) return;
  const text = (c.innerText || c.value || c.getAttribute('aria-label') || '').trim().slice(0, 60);
  window.__btClicks.push({tag: c.tagName, text, index: [...document.querySelectorAll(c.tagName)].indexOf(c), page: location.href, at: Date.now()});
}, true);
return '1';
})()"""

# What the page shows: its text plus what's in its text boxes (generated lists usually land in a textarea).
SHOWN_JS = r"""(() => [...document.querySelectorAll('textarea,input:not([type=password]):not([type=hidden])')].map(e => e.value).join('\n')
  + '\n' + (document.body ? document.body.innerText : ''))()"""

PULL_JS = ("JSON.stringify({reqs: (window.__btReqs || []).splice(0), clicks: (window.__btClicks || []).slice(-8), "
           "page: location.href, cookie: document.cookie, shown: " + SHOWN_JS + ".slice(0, 300000)})")

CLICK_JS = r"""((b) => {
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
const name = el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 60);
const all = [...document.querySelectorAll(b.tag)];
const el = all.filter(visible).find(e => b.text && name(e) === b.text) || all[b.index];
if (!el) return JSON.stringify({error: 'the "' + (b.text || b.tag) + '" button isn\'t on the page'});
el.scrollIntoView({block: 'center'}); el.click();
return JSON.stringify({clicked: name(el)});
})"""

FETCH_JS = r"""((r) => {
window.__btReplay = null;
fetch(r.url, {method: r.method, headers: r.headers, body: r.method === 'GET' || r.method === 'HEAD' ? undefined : r.body, credentials: 'include'})
  .then(res => res.text().then(t => { window.__btReplay = {status: res.status, text: t.slice(0, 2000000)}; }))
  .catch(e => { window.__btReplay = {error: String(e)}; });
return '1';
})"""

# Never sent again from BotTrack: set by the browser itself, or would describe the wrong request.
DROP_HEADERS = {"host", "content-length", "cookie", "connection", "accept-encoding", "origin", "referer", "user-agent"}


STORAGE_JS = "JSON.stringify({origin: location.origin, storage: Object.fromEntries(Object.entries(localStorage))})"
RESTORE_JS = r"""((s) => { let missing = 0;
for (const [k, v] of Object.entries(s || {})) { if (localStorage.getItem(k) !== v) { localStorage.setItem(k, v); missing++; } }
return String(missing); })"""


def seal(saved):
    """A sign-in, encrypted with the card key (Keychain on a Mac)."""
    from . import wallet
    return wallet._fernet().encrypt(json.dumps(saved).encode()).decode()


def unseal(text):
    if not text:
        return {}
    from . import wallet
    try:
        return json.loads(wallet._fernet().decrypt(text.encode()))
    except Exception:  # a different key (restored backup) or a damaged value: sign in again
        logger.warning("couldn't read a saved provider sign-in")
        return {}


def cookie_header(saved, url):
    """Cookie: header for url from a saved sign-in: every cookie whose domain covers the host."""
    host = (urlparse(url).hostname or "").lower()
    out = []
    for c in saved.get("cookies") or []:
        d = (c.get("domain") or "").lstrip(".").lower()
        if d and (host == d or host.endswith("." + d)):
            out.append(f"{c['name']}={c['value']}")
    return "; ".join(out)


def renew(saved, set_cookies, url):
    """Keep cookies the site renewed in its answer (Set-Cookie). Returns True when anything changed."""
    from http.cookies import SimpleCookie
    host = (urlparse(url).hostname or "").lower()
    cookies = saved.setdefault("cookies", [])
    changed = False
    for line in set_cookies or []:
        jar = SimpleCookie()
        try:
            jar.load(line)
        except Exception:
            continue
        for name, m in jar.items():
            domain = (m["domain"] or host).lstrip(".").lower()
            old = next((c for c in cookies if c["name"] == name and c.get("domain", "").lstrip(".").lower() == domain), None)
            if old is None:
                cookies.append({"name": name, "value": m.value, "domain": domain, "path": m["path"] or "/"})
                changed = True
            elif old["value"] != m.value:
                old["value"] = m.value
                changed = True
    return changed


def _cookie_dicts(simple_cookies):
    """pywebview's get_cookies() (SimpleCookie objects, HttpOnly ones too) as plain dicts."""
    out = []
    for jar in simple_cookies or []:
        for name, m in jar.items():
            out.append({"name": name, "value": m.value, "domain": m["domain"], "path": m["path"] or "/",
                        "secure": bool(m["secure"]), "expires": str(m["expires"] or "")})
    return out


def _set_cookies(window, cookies):
    """Put saved cookies into the window's WebKit cookie store (pywebview has no call for it)."""
    try:
        import Foundation
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView
        store = BrowserView.instances[window.uid].webview.configuration().websiteDataStore().httpCookieStore()
    except Exception:
        logger.debug("no cookie store to restore into", exc_info=True)
        return False
    for c in cookies:
        props = {"Name": c["name"], "Value": c["value"], "Domain": c.get("domain") or "", "Path": c.get("path") or "/"}
        if c.get("secure"):
            props["Secure"] = "TRUE"
        cookie = Foundation.NSHTTPCookie.cookieWithProperties_(props)
        if cookie is not None:
            AppHelper.callAfter(store.setCookie_completionHandler_, cookie, None)
    return True


class SignIn(ValueError):
    """The provider wants you to sign in again (or the Mac window isn't there)."""


def lines_in(text):
    """Proxy lines in an answer or a page: plain lines, or anywhere in JSON (a list of lines or of {host, port})."""
    from .proxyaccounts import pool_lines
    s = (text or "").strip()
    found = []
    if s[:1] in "[{":
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            data = None

        def walk(x):
            if isinstance(x, list):
                got = [ln for ln in pool_lines(json.dumps(x)) if proxies.parse_line(ln)]
                if len(got) > len(found):
                    found[:] = got
                for v in x:
                    if isinstance(v, (dict, list)):
                        walk(v)
            elif isinstance(x, dict):
                for v in x.values():
                    if isinstance(v, (dict, list)):
                        walk(v)
                    elif isinstance(v, str) and "\n" in v:
                        got = [ln.strip() for ln in v.splitlines() if proxies.parse_line(ln)]
                        if len(got) > len(found):
                            found[:] = got
        if data is not None:
            walk(data)
            return found
    return [ln.strip() for ln in s.splitlines() if proxies.parse_line(ln)]


def recipe_from(reqs, clicks, shown, before=(), page=""):
    """(recipe, lines) from what the window saw, or (None, []): the page's own request whose answer had the most
    proxy lines, else the button pressed before new lines showed up on the page."""
    best, lines = None, []
    for r in reqs:
        if not r.get("text") or (r.get("status") or 200) >= 400:
            continue
        got = lines_in(r["text"])
        if len(got) > len(lines):
            best, lines = r, got
    if best:
        headers = {k: v for k, v in (best.get("headers") or {}).items() if k.lower() not in DROP_HEADERS}
        return {"kind": "request", "method": best.get("method") or "GET", "url": best["url"], "headers": headers,
                "body": best.get("body") if best.get("body") != "[binary]" else None, "page": best.get("page") or page}, lines
    new = [ln for ln in lines_in(shown) if ln not in set(before)]
    if new and clicks:
        b = clicks[-1]
        return {"kind": "click", "page": b.get("page") or page, "button": {k: b.get(k) for k in ("tag", "text", "index")}}, new
    return None, []


class Session:
    """One signed-in window per provider account, open until BotTrack closes."""

    def __init__(self, webview):
        self.webview = webview
        self.windows, self.seen = {}, {}  # account id -> window / {"lines", "recipe", "before", "cookie"}
        self.lock = threading.Lock()

    def open(self, aid, url, name, saved=None):
        self.seen[aid] = {"lines": [], "recipe": None, "before": None, "cookie": "", "watching": True}
        w = self.windows.get(aid)
        if w is not None:
            try:
                w.show()
                self.restore(aid, saved, url)
                return self._watch(aid)
            except Exception:
                self.windows.pop(aid, None)
        self._window(aid, f"{name}: sign in, then generate a list once", url, saved)
        self._watch(aid)

    def _window(self, aid, title, url, saved, hidden=False):
        try:  # a Discord sign-in that opens in a new window has to stay in BotTrack, not jump to Safari
            self.webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
        except Exception:
            pass
        # Opened blank first: a new window clears the sign-ins WebKit holds (BotTrack's windows are private), so
        # the saved one goes back in after that, before the site loads.
        w = self.webview.create_window(title, "about:blank" if saved else url, width=1200, height=860, hidden=hidden)
        self.windows[aid] = w
        try:
            w.events.closed += lambda *a: self._closed(aid)
        except Exception:
            pass
        if saved:
            time.sleep(2.5)
            self.restore(aid, saved, url)
        return w

    def restore(self, aid, saved, url):
        """Load url in the account's window with the saved sign-in put back (cookies, then the page's storage)."""
        w = self.windows[aid]
        if saved and saved.get("cookies"):
            _set_cookies(w, saved["cookies"])
            time.sleep(0.8)
        w.load_url(url)
        if saved and saved.get("storage"):
            time.sleep(3)
            try:
                if int(self.js(aid, f"{RESTORE_JS}({json.dumps(saved['storage'])})") or 0):
                    w.load_url(url)  # the page reads its storage as it starts, so start it again
                    time.sleep(3)
            except (SignIn, ValueError):
                pass

    def capture(self, aid):
        """The window's sign-in as it is now: the site's cookies (HttpOnly too) and the page's storage."""
        w = self.windows.get(aid)
        if w is None:
            return None
        try:
            cookies = _cookie_dicts(w.get_cookies())
        except Exception:
            logger.debug("couldn't read the provider window's cookies", exc_info=True)
            cookies = []
        try:
            st = json.loads(self.js(aid, STORAGE_JS) or "{}")
        except (SignIn, ValueError):
            st = {}
        return {"cookies": cookies, "storage": st.get("storage") or {}, "origin": st.get("origin") or ""}

    def ensure(self, aid, acct, saved, page):
        """A window for this account, opening a hidden one with the saved sign-in when there's none."""
        if aid in self.windows:
            if saved:  # another BotTrack window may have opened since and cleared WebKit's sign-ins
                _set_cookies(self.windows[aid], saved.get("cookies") or [])
            return
        if not saved:
            raise SignIn("The provider wants you to sign in again. Press Sign in again on this account.")
        self._window(aid, f"{acct['name']}", page, saved, hidden=True)

    def _closed(self, aid):
        self.windows.pop(aid, None)
        if aid in self.seen:
            self.seen[aid]["watching"] = False
        if not self.windows:
            try:
                self.webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            except Exception:
                pass

    def js(self, aid, code):
        w = self.windows.get(aid)
        if w is None:
            raise SignIn("The provider's window isn't open. Press Sign in again on this account.")
        try:
            return w.evaluate_js(code)
        except Exception as e:
            raise SignIn(f"The provider's window isn't answering ({e}). Press Sign in again.")

    def _watch(self, aid):
        def loop():
            state = self.seen[aid]
            reqs = []
            while state["watching"] and aid in self.windows:
                try:
                    self.js(aid, RECORD_JS)
                    got = json.loads(self.js(aid, PULL_JS) or "{}")
                except (SignIn, ValueError, TypeError):
                    time.sleep(1.5)
                    continue
                reqs = (reqs + got.get("reqs", []))[-60:]
                if state["before"] is None:
                    state["before"] = lines_in(got.get("shown", ""))
                recipe, lines = recipe_from(reqs, got.get("clicks", []), got.get("shown", ""), state["before"], got.get("page", ""))
                state["cookie"] = got.get("cookie", "")
                if recipe and len(lines) >= len(state["lines"]):
                    state["recipe"], state["lines"] = recipe, lines
                time.sleep(1.5)
        threading.Thread(target=loop, daemon=True).start()

    def status(self, aid):
        s = self.seen.get(aid) or {}
        r = s.get("recipe") or {}
        return {"open": aid in self.windows, "watching": bool(s.get("watching")), "found": len(s.get("lines") or []),
                "kind": r.get("kind"), "from": urlparse(r.get("url") or r.get("page") or "").path or "",
                "button": (r.get("button") or {}).get("text") or ""}

    def keep(self, aid):
        """Stop watching and tuck the window away; it stays signed in for Refresh pool."""
        s = self.seen.get(aid) or {}
        s["watching"] = False
        w = self.windows.get(aid)
        if w is not None:
            try:
                w.hide()
            except Exception:
                pass
        return s

    def replay_request(self, aid, recipe, timeout=40):
        self.js(aid, f"{FETCH_JS}({json.dumps(recipe)})")
        end = time.time() + timeout
        while time.time() < end:
            out = self.js(aid, "JSON.stringify(window.__btReplay || null)")
            got = json.loads(out) if isinstance(out, str) else None
            if got:
                if got.get("error"):
                    raise SignIn(f"The provider's site didn't answer: {got['error']}")
                if got["status"] in (401, 403):
                    raise SignIn("The provider wants you to sign in again. Press Sign in again on this account.")
                return got["text"]
            time.sleep(1)
        raise SignIn("The provider's site didn't answer in time.")

    def replay_click(self, aid, recipe, timeout=30):
        w = self.windows.get(aid)
        if w is None:
            raise SignIn("Sign in again: this provider's list is made in its page, which needs the signed-in window.")
        if (self.js(aid, "location.href") or "") != recipe["page"]:
            w.load_url(recipe["page"])
            time.sleep(4)
        before = set(lines_in(self.js(aid, SHOWN_JS) or ""))
        r = json.loads(self.js(aid, f"{CLICK_JS}({json.dumps(recipe['button'])})") or "{}")
        if r.get("error"):
            if "sign" in (self.js(aid, "document.body ? document.body.innerText.slice(0, 3000) : ''") or "").lower():
                raise SignIn("The provider wants you to sign in again. Press Sign in again on this account.")
            raise SignIn(f"Couldn't make a list: {r['error']}. Sign in again and generate one so BotTrack can learn it again.")
        end = time.time() + timeout
        lines = []
        while time.time() < end:
            time.sleep(1.5)
            lines = lines_in(self.js(aid, SHOWN_JS) or "")
            new = [ln for ln in lines if ln not in before]
            if new:
                return new
        return lines


_webview = None
_session = None


def register(webview):
    global _webview, _session
    _webview, _session = webview, None


def available():
    return _webview is not None


def session():
    global _session
    if _webview is None:
        raise SignIn("Signing in to a provider's site works in the BotTrack Mac app, which opens it in its own window.")
    if _session is None:
        _session = Session(_webview)
    return _session


def _direct(recipe, cookie, opener=None):
    """Repeat the dashboard's request from BotTrack itself: (answer text, Set-Cookie lines), or (None, []) when the
    site wants a fresh sign-in."""
    headers = {"User-Agent": "BotTrack", "Accept": "application/json, text/plain, */*", **(recipe.get("headers") or {})}
    if cookie:
        headers["Cookie"] = cookie
    url, method, body = recipe["url"], recipe.get("method") or "GET", recipe.get("body")
    if not url.startswith("https://"):
        raise ValueError("The address must start with https://")
    req = urllib.request.Request(url, headers=headers, method=method,
                                 data=body.encode() if body and method not in ("GET", "HEAD") else None)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=30) as r:
            got = getattr(r, "headers", None)
            renewed = got.get_all("Set-Cookie") or [] if got is not None and hasattr(got, "get_all") else []
            return r.read(2_000_000).decode("utf-8", "replace"), renewed
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 419):
            return None, []
        raise ValueError(f"The provider answered {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the provider: {e.reason}") from None


def keep_signin(con, aid, saved):
    con.execute("UPDATE proxy_accounts SET site_session=? WHERE id=?", (seal(saved), aid))
    con.commit()


def fresh_lines(acct, opener=None, con=None):
    """A fresh list for an account set up by signing in. Raises ValueError (SignIn included) with what to do.
    With con, a sign-in the site renewed along the way is saved."""
    try:
        recipe = json.loads(acct.get("recipe") or "{}")
    except json.JSONDecodeError:
        recipe = {}
    if not recipe:
        raise ValueError("Sign in to the provider's site and generate a list once first.")
    aid = acct["id"]
    saved = unseal(acct.get("site_session"))
    used_window = False
    if recipe["kind"] == "request":
        cookie = cookie_header(saved, recipe["url"]) if saved else acct.get("site_cookie") or ""
        text, renewed = _direct(recipe, cookie, opener)
        if saved and renew(saved, renewed, recipe["url"]) and con is not None:
            keep_signin(con, aid, saved)
        lines = lines_in(text) if text is not None else []
        if not lines:
            if not available():
                raise SignIn("The provider wants you to sign in again. Press Sign in again on this account.")
            session().ensure(aid, acct, saved, recipe.get("page") or acct.get("site_url"))
            lines = lines_in(session().replay_request(aid, recipe))
            used_window = True
    else:
        if not available():
            raise SignIn("This provider's list is made in its page, which needs the BotTrack Mac app.")
        session().ensure(aid, acct, saved, recipe["page"])
        lines = session().replay_click(aid, recipe)
        used_window = True
    if not lines:
        raise ValueError("The provider's answer had no proxies BotTrack could read.")
    if used_window and con is not None:
        now = session().capture(aid)
        if now and now["cookies"]:
            keep_signin(con, aid, now)
    return lines


def _con():
    from . import proxyaccounts
    return proxyaccounts.ensure(db.connect(current_app.config.get("DB_PATH")))


def _back():
    return redirect(url_for("proxies.page") + "#accounts")


@bp.post("/proxies/login/open")
def open_site():
    con = _con()
    aid = request.form.get("id", type=int)
    if aid:
        row = con.execute("SELECT name, site_url FROM proxy_accounts WHERE id=?", (aid,)).fetchone()
        if not row:
            return _back()
        name, url = row["name"], row["site_url"]
        saved = unseal(con.execute("SELECT site_session FROM proxy_accounts WHERE id=?", (aid,)).fetchone()[0])
    else:
        name, url = (request.form.get("name") or "").strip(), (request.form.get("site_url") or "").strip()
        if url and "://" not in url:
            url = "https://" + url
        if not name or not url.startswith("https://"):
            flash("Give the provider a name and its dashboard address (https://...).")
            return _back()
        aid = con.execute("INSERT INTO proxy_accounts (name, auth, unit, site_url) VALUES (?, 'site', 'gb', ?)",
                          (name, url)).lastrowid
        con.commit()
        saved = None
    try:
        session().open(aid, url, name, saved)
        flash(f"Sign in to {name} in the new window, then generate a proxy list there once. BotTrack learns how, "
              "and Refresh pool does it for you after that.")
    except SignIn as e:
        flash(str(e))
    return redirect(url_for("proxies.page", learning=aid) + "#accounts")


@bp.get("/proxies/login/status")
def status():
    aid = request.args.get("id", type=int)
    if not available() or not aid:
        return jsonify({"open": False})
    return jsonify(session().status(aid))


@bp.post("/proxies/login/save")
def save():
    """Keep what the window learned, and fill the account's group with the list it just saw."""
    con = _con()
    aid = request.form.get("id", type=int)
    if not available() or not aid:
        return _back()
    signin = session().capture(aid)
    s = session().keep(aid)
    if not s.get("recipe"):
        flash("BotTrack hasn't seen a proxy list in that window yet. Generate one there, then press Use this again.")
        return _back()
    con.execute("UPDATE proxy_accounts SET recipe=?, site_cookie=?, site_session=? WHERE id=?",
                (json.dumps(s["recipe"]), s.get("cookie") or "", seal(signin) if signin and (signin["cookies"] or signin["storage"]) else None, aid))
    con.commit()
    from . import proxyaccounts
    acct = proxyaccounts._acct(con, aid)
    n = proxyaccounts.replace_group(con, acct, s["lines"])
    flash(f"Learned {acct['name']}. Its group has the {n} proxies you just made. Refresh pool makes a fresh list any time; "
          "the sign-in is kept (encrypted) until the site ends it.")
    return _back()


# Suggest a provider ------------------------------------------------------------------------------------------------

def send_suggestion(fields, sender, password, version="", to=None, smtp=None):
    """Email a provider suggestion from the user's own Gmail to the developer address."""
    from .packs import developer
    to = to or developer()
    if not to:
        raise RuntimeError("This copy of BotTrack has no developer address to send suggestions to.")
    msg = EmailMessage()
    msg["Subject"] = f"BotTrack provider suggestion: {fields['name']}"
    msg["From"], msg["To"] = sender, to
    msg.set_content("A proxy provider to set up in a coming build.\n\n"
                    + "\n".join(f"{k}: {v}" for k, v in fields.items() if v)
                    + f"\n\nBotTrack {version or 'unknown build'}")
    if smtp is None:
        smtp = smtplib.SMTP_SSL(logs.SMTP_HOST, 465, context=ssl.create_default_context(), timeout=30)
    with smtp as s:
        s.login(sender, password)
        s.send_message(msg)
    return to


@bp.post("/proxies/suggest")
def suggest():
    from .config import Config
    fields = {k: (request.form.get(k) or "").strip()[:500] for k in ("name", "site", "docs", "login", "notes")}
    accounts = dict(Config.from_env().accounts)
    sender = next(iter(accounts), "")
    if not fields["name"] and not fields["site"]:
        flash("Name the provider or paste its site.")
    elif not sender:
        flash("Connect a Gmail inbox on Setup first; the suggestion is sent from it.")
    else:
        fields["name"] = fields["name"] or urlparse(fields["site"] if "://" in fields["site"] else "https://" + fields["site"]).netloc
        try:
            from .updater import installed_version
            send_suggestion(fields, sender, accounts[sender], installed_version())
            flash(f"Sent. Thanks! {fields['name']} can get its own preset in a coming update.")
        except Exception as e:
            flash(f"Couldn't send the suggestion: {logs.redact(str(e))}")
    return _back()
