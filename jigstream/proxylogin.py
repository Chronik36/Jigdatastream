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
The window stays signed in until BotTrack closes (it's private, so nothing is kept on disk by it). When a site
needs you again, Refresh pool says so and Sign in again opens it. Only the Mac app has these windows.

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

    def open(self, aid, url, name):
        self.seen[aid] = {"lines": [], "recipe": None, "before": None, "cookie": "", "watching": True}
        w = self.windows.get(aid)
        if w is not None:
            try:
                w.show()
                w.load_url(url)
                return self._watch(aid)
            except Exception:
                self.windows.pop(aid, None)
        try:  # a Discord sign-in that opens in a new window has to stay in BotTrack, not jump to Safari
            self.webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
        except Exception:
            pass
        w = self.webview.create_window(f"{name}: sign in, then generate a list once", url, width=1200, height=860)
        self.windows[aid] = w
        try:
            w.events.closed += lambda *a: self._closed(aid)
        except Exception:
            pass
        self._watch(aid)

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
    """Repeat the dashboard's request from BotTrack itself; the answer's text, or None when it wants the window."""
    from .proxyaccounts import fetch_raw
    headers = dict(recipe.get("headers") or {})
    if cookie:
        headers["Cookie"] = cookie
    try:
        return fetch_raw(recipe["url"], recipe.get("method") or "GET", headers, recipe.get("body"), opener)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 419):
            return None
        raise ValueError(f"The provider answered {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the provider: {e.reason}") from None


def fresh_lines(acct, opener=None):
    """A fresh list for an account set up by signing in. Raises ValueError (SignIn included) with what to do."""
    try:
        recipe = json.loads(acct.get("recipe") or "{}")
    except json.JSONDecodeError:
        recipe = {}
    if not recipe:
        raise ValueError("Sign in to the provider's site and generate a list once first.")
    aid = acct["id"]
    if recipe["kind"] == "request":
        text = _direct(recipe, acct.get("site_cookie") or "", opener)
        lines = lines_in(text) if text is not None else []
        if not lines:
            if not available() or aid not in session().windows:
                raise SignIn("The provider wants you to sign in again. Press Sign in again on this account.")
            lines = lines_in(session().replay_request(aid, recipe))
    else:
        if not available():
            raise SignIn("This provider's list is made in its page, which needs the BotTrack Mac app.")
        lines = session().replay_click(aid, recipe)
    if not lines:
        raise ValueError("The provider's answer had no proxies BotTrack could read.")
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
    try:
        session().open(aid, url, name)
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
    s = session().keep(aid)
    if not s.get("recipe"):
        flash("BotTrack hasn't seen a proxy list in that window yet. Generate one there, then press Use this again.")
        return _back()
    con.execute("UPDATE proxy_accounts SET recipe=?, site_cookie=? WHERE id=?",
                (json.dumps(s["recipe"]), s.get("cookie") or "", aid))
    con.commit()
    from . import proxyaccounts
    acct = proxyaccounts._acct(con, aid)
    n = proxyaccounts.replace_group(con, acct, s["lines"])
    flash(f"Learned {acct['name']}. Its group has the {n} proxies you just made; Refresh pool makes a fresh list any time "
          "while BotTrack is open.")
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
