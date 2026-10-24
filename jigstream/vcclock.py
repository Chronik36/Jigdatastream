"""Lock and unlock virtual cards (Capital One, Amex) in the bank's own website, from BotTrack.

Neither bank has a public API for this, so BotTrack opens the bank's site in a normal window on this Mac and you
sign in there yourself. BotTrack never sees or keeps the password or the sign-in: the window is private (nothing is
saved when it closes) and only reads the page you leave it on. On the page that lists your virtual cards it:
- scans the cards it can see (nickname, last 4, locked or not, and the full number, expiry and CVV only when the
  page itself shows them) so they can be added to Cards;
- presses each picked card's own lock or unlock control, one card at a time with a pause between, answers the
  bank's "are you sure" box, then reads the page again to check it worked.
If it can't tell which control is the lock, press Teach and lock one card yourself: it watches that click and
repeats it on the others. Only the Mac app has the window (window.py registers it).
"""
import json
import logging
import threading
import time

logger = logging.getLogger("jigstream.vcclock")

BANKS = {"Capital One": "https://myaccounts.capitalone.com/",
         "Amex": "https://www.americanexpress.com/en-us/account/login"}
GAP = 3.0  # seconds between cards, so it moves at a person's pace

# Everything runs inside the bank's page. Card blocks get data-bt-card="<last 4>" so later steps find them again.
COMMON = r"""
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
const FULL = /\b(?:\d{4}[ -]?){3}\d{1,4}(?!\d)|\b\d{4}[ -]?\d{6}[ -]?\d{5}(?!\d)/g;
const MASK = /(?:[•*xX·]{2,}[\s-]?|\.{2,}|ending in\s*|ends in\s*|last 4\D{0,3})(\d{4})(?!\d)/gi;
const LOCK = /\b(lock|freeze|pause|suspend|deactivate|disable|turn off)\b/i;
const UNLOCK = /\b(unlock|unfreeze|resume|unpause|reactivate|activate|enable|turn on)\b/i;
const luhn = d => { let s = 0; for (let i = 0; i < d.length; i++) { let n = +d[d.length - 1 - i];
  if (i % 2) { n *= 2; if (n > 9) n -= 9; } s += n; } return s % 10 === 0; };
const nums = text => { const out = {};
  for (const m of text.matchAll(FULL)) { const d = m[0].replace(/\D/g, ''); if (d.length >= 15 && luhn(d)) out[d.slice(-4)] = d; }
  for (const m of text.matchAll(MASK)) if (!(m[1] in out)) out[m[1]] = '';
  return out; };
const name = el => (el.getAttribute('aria-label') || el.innerText || el.value || el.title || '').trim().slice(0, 60);
const CONTROLS = 'button,[role=button],[role=switch],[role=menuitem],input[type=checkbox],a,label';
// Never pressed, whatever it's called: these delete or cancel the card instead of locking it.
const DANGER = /\b(delete|remove|cancel|close|replace|report)\b/i;
const controls = block => [...block.querySelectorAll(CONTROLS)].filter(visible).filter(el => !DANGER.test(name(el)));
"""

SCAN_JS = "(() => {" + COMMON + r"""
const blocks = new Map();
for (const el of document.querySelectorAll('body *')) {
  if (!visible(el) || /^(SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName)) continue;
  const text = el.innerText || '';
  if (text.length > 400) continue;
  const own = nums(text), keys = Object.keys(own);
  if (keys.length !== 1 || [...el.children].some(c => Object.keys(nums(c.innerText || '')).length)) continue;
  // Climb to the biggest box that still holds just this one card.
  let block = el;
  for (let up = block.parentElement, i = 0; up && up !== document.body && i < 10; up = up.parentElement, i++) {
    if (Object.keys(nums(up.innerText || '')).length > 1) break;
    block = up;
  }
  if (!blocks.has(block)) blocks.set(block, {last4: keys[0], number: own[keys[0]]});
  else if (own[keys[0]]) blocks.get(block).number = own[keys[0]];
}
const out = [];
for (const [block, c] of blocks) {
  const text = block.innerText || '';
  block.dataset.btCard = c.last4;
  const names = controls(block).map(name);
  let locked = null;
  if (names.some(n => UNLOCK.test(n))) locked = true;
  else if (names.some(n => LOCK.test(n))) locked = false;
  else if (/\b(locked|paused|frozen|suspended)\b/i.test(text)) locked = true;
  const sw = controls(block).find(el => el.matches('[role=switch],input[type=checkbox]') && /lock|freeze|pause/i.test(name(el) + ' ' + (el.closest('label') ? el.closest('label').innerText : '')));
  if (sw) { const on = sw.getAttribute('aria-checked') === 'true' || sw.checked === true; locked = on; }
  const exp = text.match(/(?<!\d)(0[1-9]|1[0-2])\s?\/\s?(\d{4}|\d{2})(?!\d)/);
  const cvv = text.match(/(?:cvv|cvc|csc|security code)\D{0,8}(\d{3,4})(?!\d)/i);
  const label = text.split('\n').map(s => s.trim()).find(s => s && !/\d{4}/.test(s) && !LOCK.test(s) && !UNLOCK.test(s) && s.length < 60) || '';
  out.push({last4: c.last4, number: c.number, exp: exp ? exp[1] + '/' + exp[2].slice(-2) : '', cvv: c.number && cvv ? cvv[1] : '',
            label, locked});
}
return JSON.stringify(out);
})()"""

ACT_JS = "((last4, want, learned) => {" + COMMON + r"""
const block = document.querySelector('[data-bt-card="' + last4 + '"]');
if (!block) return JSON.stringify({error: 'not on this page any more'});
const cs = controls(block), word = want === 'unlock' ? UNLOCK : LOCK;
let target = null;
if (learned) target = cs.find(el => el.tagName === learned.tag && (el.getAttribute('role') || '') === learned.role && name(el) === learned.name)
  || [...block.querySelectorAll(learned.tag)].filter(visible)[learned.index];
if (!target) target = cs.find(el => word.test(name(el)) && !el.matches('[role=switch],input[type=checkbox]'));
if (!target) {
  const sw = cs.find(el => el.matches('[role=switch],input[type=checkbox]') && /lock|freeze|pause/i.test(name(el) + ' ' + (el.closest('label') ? el.closest('label').innerText : '')));
  if (sw) {
    const on = sw.getAttribute('aria-checked') === 'true' || sw.checked === true;
    if (on === (want === 'lock')) return JSON.stringify({already: true});
    target = sw;
  }
}
if (!target && cs.some(el => (want === 'unlock' ? LOCK : UNLOCK).test(name(el)))) return JSON.stringify({already: true});  // it offers the opposite
if (!target) return JSON.stringify({error: 'no ' + want + ' control found on this card'});
target.scrollIntoView({block: 'center'});
target.click();
return JSON.stringify({clicked: name(target) || target.tagName.toLowerCase()});
})"""

CONFIRM_JS = "((want) => {" + COMMON + r"""
const word = want === 'unlock' ? UNLOCK : LOCK;
const boxes = [...document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]')].filter(visible);
for (const box of boxes) {
  const bs = [...box.querySelectorAll('button,[role=button]')].filter(visible).filter(b => !/cancel|close|no thanks|back|not now/i.test(name(b)));
  const b = bs.find(b => word.test(name(b))) || bs.find(b => /\b(confirm|yes|continue|ok|submit)\b/i.test(name(b)));
  if (b) { b.click(); return JSON.stringify({confirmed: name(b)}); }
}
return JSON.stringify({});
})"""

LEARN_JS = "(() => {" + COMMON + r"""
window.__btLearned = null;
if (!window.__btLearning) {
  window.__btLearning = true;
  document.addEventListener('click', e => {
    const block = e.target.closest('[data-bt-card]');
    if (!block || window.__btLearned) return;
    const c = e.target.closest(CONTROLS) || e.target;
    window.__btLearned = {last4: block.dataset.btCard, tag: c.tagName, role: c.getAttribute('role') || '', name: name(c),
                          index: [...block.querySelectorAll(c.tagName)].filter(visible).indexOf(c)};
  }, true);
}
return '{}';
})()"""


class Closed(RuntimeError):
    """The bank window isn't open."""


class Session:
    """The one bank window and what was found in it. Nothing from the sign-in is kept."""

    def __init__(self, webview, gap=GAP):
        self.webview, self.gap = webview, gap
        self.window, self.bank, self.cards = None, None, []
        self.learned = {}  # want -> how you pressed it on one card, for this window only
        self.learning = None
        self.job = {"running": False, "done": 0, "total": 0, "want": "", "results": []}
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def open(self, bank):
        url = BANKS[bank]
        self.cards, self.learned, self.learning = [], {}, None
        if self.window is not None:
            try:
                self.window.load_url(url)
                self.bank = bank
                return
            except Exception:
                self.window = None
        self.window = self.webview.create_window(f"{bank}: sign in, then open your virtual cards", url,
                                                 width=1200, height=860)
        try:
            self.window.events.closed += self._closed
        except Exception:
            pass
        self.bank = bank

    def _closed(self, *a):
        self.window, self.cards, self.learned = None, [], {}
        self.stop.set()

    def js(self, code):
        if self.window is None:
            raise Closed("Open the bank window first.")
        try:
            out = self.window.evaluate_js(code)
        except Exception as e:
            raise Closed(f"The bank window isn't answering ({e}). Open it again.")
        return json.loads(out) if isinstance(out, str) else (out or {})

    def scan(self):
        self.cards = self.js(SCAN_JS) or []
        logger.info("found %d card(s) in the %s window", len(self.cards), self.bank)  # never the numbers
        return self.cards

    def teach(self, want):
        self.js(LEARN_JS)
        self.learning = want

    def check_learned(self):
        if not self.learning:
            return None
        got = self.js("JSON.stringify(window.__btLearned || null)")
        if got:
            self.learned[self.learning] = got
            self.learning = None
        return got

    def act(self, last4, want):
        """Press one card's control and answer the confirm box. Returns a short result."""
        r = self.js(f"{ACT_JS}({json.dumps(last4)}, {json.dumps(want)}, {json.dumps(self.learned.get(want))})")
        if r.get("error") or r.get("already"):
            return r
        time.sleep(min(1.5, self.gap))
        r.update(self.js(f"{CONFIRM_JS}({json.dumps(want)})"))
        return r

    def run(self, last4s, want, on_done=None):
        """Lock or unlock each card in turn, then read the page again to check."""
        self.stop.clear()
        self.job = {"running": True, "done": 0, "total": len(last4s), "want": want, "results": []}
        try:
            for i, last4 in enumerate(last4s):
                if self.stop.is_set():
                    break
                try:
                    r = self.act(last4, want)
                except Closed as e:
                    self.job["results"].append({"last4": last4, "ok": False, "note": str(e)})
                    break
                time.sleep(self.gap)
                if r.get("already"):
                    ok, note = True, f"was already {want}ed"
                elif r.get("error"):
                    ok, note = False, r["error"]
                else:
                    now = {c["last4"]: c for c in self.scan()}
                    state = now.get(last4, {}).get("locked")
                    ok = state is None or state == (want == "lock")
                    note = (f"pressed \"{r.get('clicked')}\"" + (f", then \"{r['confirmed']}\"" if r.get("confirmed") else "")
                            + ("" if ok else "; the page still shows the old state"))
                self.job["results"].append({"last4": last4, "ok": ok, "note": note})
                self.job["done"] = i + 1
            if on_done:
                on_done(self.job)
        finally:
            self.job["running"] = False


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
        raise Closed("Card locks work in the BotTrack Mac app, which can open the bank's site in its own window.")
    if _session is None:
        _session = Session(_webview)
    return _session
