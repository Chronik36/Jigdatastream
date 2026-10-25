"""Base addresses: the real places every jigged profile address ships to.

You add each base address once (Tracking > Base addresses). Every full address BotTrack sees, on a profile, an order
email or a checkout's profile, is then tied to one base:
- by rule when it has the base's ZIP and house number (the usual jig: letters in front, a made-up unit, a misspelled
  street),
- by the local AI when it has a base's ZIP but the rule can't tell (a changed house number, a written-out street),
- or by hand on the Base addresses page, which always wins.
Addresses in a ZIP no base uses are left unmatched without asking the model. Shipping and Drops open on an overview
by base address, and picking one shows its packages or checkouts.
"""
import logging
import threading
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from . import address as A
from . import db, drop, profilematch

logger = logging.getLogger("jigstream.bases")
bp = Blueprint("bases", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS base_addresses (
    id INTEGER PRIMARY KEY, name TEXT, line1 TEXT, city TEXT, state TEXT, zip TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS base_matches (
    addr TEXT PRIMARY KEY, full TEXT, base_id INTEGER, source TEXT, at TEXT);
"""
AI_LIMIT = 150  # addresses the model looks at per run; the rest wait for the next one
state = {"running": False, "last": ""}


def ensure(con):
    con.executescript(SCHEMA)
    return con


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key(full):
    return " ".join((full or "").upper().replace(",", " ").split())


def where(full):
    """A one-line address as drop uses it (house number + street, ZIP, full), or None."""
    if not (full or "").strip():
        return None
    w = drop._where(full, profilematch.parse_order_address(full)["zip"], full)
    return w if w["base"] or w["zip"] else None  # no house number to read ("One Twenty Three Main"): the AI decides


def full_of(b):
    return ", ".join(x for x in (b["line1"], b["city"], " ".join(y for y in (b["state"], b["zip"]) if y)) if x)


def listing(con):
    ensure(con)
    out = []
    for r in con.execute("SELECT * FROM base_addresses ORDER BY lower(name), id"):
        b = dict(r)
        b["full"] = full_of(b)
        b["where"] = drop._where(b["line1"], b["zip"], b["full"])
        out.append(b)
    return out


def save(con, form, base_id=None):
    """Add a base address, or change one. Returns an error to show, or ""."""
    ensure(con)
    line1 = " ".join((form.get("line1") or "").split())
    zip_ = A.zip5(form.get("zip"))
    if not A.base_line1(line1):
        return "Enter the street address with its house number, like 123 Main St."
    if not zip_:
        return "Enter the 5-digit ZIP code."
    name = (form.get("name") or "").strip() or A.base_line1(line1).title()
    row = (name, line1, (form.get("city") or "").strip(), A.state_code(form.get("state")) or
           (form.get("state") or "").strip().upper(), zip_)
    if base_id:
        con.execute("UPDATE base_addresses SET name=?, line1=?, city=?, state=?, zip=? WHERE id=?", row + (base_id,))
    else:
        con.execute("INSERT INTO base_addresses (name, line1, city, state, zip, created_at) VALUES (?,?,?,?,?,?)",
                    row + (now(),))
    # A new or moved base can change what the model would say, so its answers are asked again; hand picks stay.
    con.execute("DELETE FROM base_matches WHERE source='ai'")
    con.commit()
    return ""


def remove(con, base_id):
    ensure(con)
    con.execute("DELETE FROM base_addresses WHERE id=?", (base_id,))
    con.execute("DELETE FROM base_matches WHERE base_id=? OR source='ai'", (base_id,))
    con.commit()


def pick(con, full, base_id):
    """Tie an address to a base by hand (base_id None or 0: to none). Sticks over rules and the AI."""
    ensure(con)
    con.execute("INSERT OR REPLACE INTO base_matches VALUES (?,?,?,?,?)",
                (key(full), full, base_id or None, "manual", now()))
    con.commit()


def by_rule(w, bases):
    """(base id or None, decided). Same ZIP and house number: that base. A base's ZIP but not its number: not
    decided, for the AI. No base in its ZIP: decided, none."""
    if not w:
        return None, True
    same_zip = [b for b in bases if b["where"]["zip"] and b["where"]["zip"] == w["zip"]]
    if not w["zip"]:
        same_zip = bases
    if not same_zip:
        return None, True
    if not w["base"]:
        return None, False
    hits = [b for b in same_zip if drop.cluster(b["where"]) == drop.cluster(w)]
    if len(hits) == 1:
        return hits[0]["id"], True
    if len(hits) > 1:  # two bases at one house number (rare): the closer street spelling
        street = set(w["base"].split()[1:])
        best = max(hits, key=lambda b: len(street & set(b["where"]["base"].split()[1:])))
        return best["id"], True
    return None, False


def profile_homes(con):
    """profile name -> {"full": its ship address (else billing) on one line}, even one with no house number."""
    out = {}
    for p in con.execute("SELECT name, ship_address1, ship_address2, ship_city, ship_state, ship_zip,"
                         " bill_address1, bill_address2, bill_city, bill_state, bill_zip FROM profiles"):
        f = p[1:6] if (p[1] or "").strip() else p[6:11]
        full = ", ".join(x for x in (" ".join(y for y in f[:2] if y), f[2], " ".join(y for y in f[3:] if y)) if x)
        if full.strip():
            out[p[0]] = {"full": full}
    return out


class Resolver:
    """Which base an address belongs to. Built once per page, so every lookup is a dictionary read."""

    def __init__(self, con):
        ensure(con)
        self.bases = listing(con)
        self.by_id = {b["id"]: b for b in self.bases}
        self.saved = {r["addr"]: r["base_id"] for r in con.execute("SELECT addr, base_id FROM base_matches")}
        self.homes = profile_homes(con)

    def __call__(self, full):
        if not self.bases or not full:
            return None
        k = key(full)
        if k in self.saved:
            return self.saved[k] if self.saved[k] in self.by_id else None
        return by_rule(where(full), self.bases)[0]

    def source(self, full):
        k = key(full)
        return "by hand or AI" if k in self.saved else "rule"

    def of_order(self, o):
        """An order's base: where its email says it shipped, else its matched profile's address."""
        b = self(o.get("shipping_address"))
        if b is None and o.get("profile_name") in self.homes:
            b = self(self.homes[o["profile_name"]]["full"])
        return b

    def of_checkout(self, c):
        return self(c["where"]["full"]) if c.get("where") else None

    def of_profile(self, name):
        return self(self.homes[name]["full"]) if name in self.homes else None


def addresses(con):
    """Every distinct full address in use: profiles' and order emails'. [(full, where it came from)]"""
    seen, out = set(), []
    for name, w in profile_homes(con).items():
        if key(w["full"]) not in seen:
            seen.add(key(w["full"]))
            out.append((w["full"], f"Profile {name}"))
    for r in con.execute("SELECT DISTINCT shipping_address FROM tracked_orders WHERE shipping_address IS NOT NULL"):
        if r[0] and key(r[0]) not in seen:
            seen.add(key(r[0]))
            out.append((r[0], "Order email"))
    return out


def report(con):
    """Each base with the addresses tied to it, plus the ones tied to none, for the Base addresses page."""
    res = Resolver(con)
    groups = {b["id"]: [] for b in res.bases}
    loose, waiting = [], 0
    saved = {r["addr"]: r["source"] for r in con.execute("SELECT addr, source FROM base_matches")}
    for full, where_from in addresses(con):
        b = res(full)
        k = key(full)
        how = {"manual": "by hand", "ai": "AI"}.get(saved.get(k), "rule")
        if b is None and k not in saved and not by_rule(where(full), res.bases)[1]:
            how, waiting = "waiting for the AI", waiting + 1
        (groups[b] if b in groups else loose).append({"full": full, "from": where_from, "how": how})
    return {"bases": [dict(b, addresses=groups[b["id"]]) for b in res.bases], "loose": loose, "waiting": waiting}


def suggestions(con):
    """Base addresses to add, from the places your profiles ship to (jigs folded together), most profiles first."""
    have = {drop.cluster(b["where"]) for b in listing(con)}
    places = {}
    for name, w in drop.profile_addresses(con).items():
        c = drop.cluster(w)
        if c in have:
            continue
        p = places.setdefault(c, {"line1": w["base"].title(), "zip": w["zip"], "profiles": 0, "full": w["full"]})
        p["profiles"] += 1
    return sorted(places.values(), key=lambda p: -p["profiles"])[:12]


PROMPT = """You match jigged shipping addresses to the real base address the package goes to.
Bot users "jig" one real address many ways so a store doesn't see repeats: letters or codes in front of the house
number, made-up apartment, unit or suite numbers, misspelled or abbreviated street names, extra words after the street,
a written-out number. The package still goes to the same house. A different house number on a different street, or a
different city or ZIP, is a different place.
Reply with JSON only: {"base": <the number of the base address it is a jig of, or 0 if none>}"""


def ask(llm, full, bases):
    lines = "\n".join(f"{i}. {b['full']}" for i, b in enumerate(bases, 1))
    reply = llm.chat_json(PROMPT, f"Address: {full}\n\nBase addresses:\n{lines}")
    try:
        n = int((reply or {}).get("base") or 0)
    except (TypeError, ValueError):
        n = 0
    return bases[n - 1]["id"] if 1 <= n <= len(bases) else None


def match_with_ai(con, llm, stop=None, limit=AI_LIMIT):
    """Ask the model about the addresses the rule couldn't place. Returns how many it tied to a base."""
    ensure(con)
    stop = stop or (lambda: False)
    bases = listing(con)
    if not bases:
        return 0
    saved = {r[0] for r in con.execute("SELECT addr FROM base_matches")}
    tied = asked = 0
    for full, _ in addresses(con):
        if stop() or asked >= limit:
            break
        w = where(full)
        if key(full) in saved or by_rule(w, bases)[1]:
            continue
        near = [b for b in bases if not w["zip"] or b["where"]["zip"] == w["zip"]]
        try:
            base_id = ask(llm, full, near)
        except Exception as e:
            logger.warning("the AI couldn't match %s: %s", full[:60], e)
            break
        asked += 1
        tied += base_id is not None
        con.execute("INSERT OR REPLACE INTO base_matches VALUES (?,?,?,?,?)", (key(full), full, base_id, "ai", now()))
        con.commit()
    if asked:
        logger.info("base addresses: the AI looked at %d address(es), tied %d", asked, tied)
    return tied


def run(db_path=None, llm=None, stop=None):
    """match_with_ai in the background, one run at a time."""
    if state["running"]:
        return False
    state.update(running=True, last="The AI is matching addresses to your base addresses...")

    def go():
        try:
            from .config import Config
            from .llm import from_config
            tied = match_with_ai(ensure(db.connect(db_path)), llm or from_config(Config.from_env()), stop)
            state["last"] = f"The AI tied {tied} more address{'es' if tied != 1 else ''} to a base." if tied else \
                "The AI found nothing more to tie."
        except Exception as e:
            logger.exception("matching base addresses failed")
            state["last"] = f"Matching with the AI failed: {e}"
        finally:
            state["running"] = False
    threading.Thread(target=go, daemon=True).start()
    return True


def overview(items, base_of, bases, count):
    """Rows for a by-base table: [{id, name, full, ...count(items at it)}], then the unmatched ones."""
    groups = {b["id"]: [] for b in bases}
    loose = []
    for it in items:
        b = base_of(it)
        (groups[b] if b in groups else loose).append(it)
    rows = [dict(id=b["id"], name=b["name"], full=b["full"], **count(groups[b["id"]])) for b in bases]
    if loose:
        rows.append(dict(id="none", name="Not matched to a base", full="", **count(loose)))
    return rows


def _con():
    return ensure(db.connect(current_app.config.get("DB_PATH")))


@bp.get("/bases")
def page():
    con = _con()
    return render_template("bases.html", r=report(con), suggestions=suggestions(con), matching=state,
                           edit=request.args.get("edit", type=int))


@bp.post("/bases")
def add():
    error = save(_con(), request.form, request.form.get("id", type=int))
    if error:
        flash(error)
    else:
        flash("Saved. The rule matches addresses with this ZIP and house number right away; the AI looks at the rest.")
        run(current_app.config.get("DB_PATH"))
    return redirect(url_for("bases.page"))


@bp.post("/bases/<int:base_id>/delete")
def delete(base_id):
    remove(_con(), base_id)
    flash("Base address removed. Its addresses are matched again.")
    return redirect(url_for("bases.page"))


@bp.post("/bases/pick")
def pick_page():
    pick(_con(), request.form.get("full", ""), request.form.get("base", type=int))
    return redirect(request.form.get("back") or url_for("bases.page"))


@bp.post("/bases/match")
def match_now():
    flash("The AI is matching addresses now. Refresh in a minute." if run(current_app.config.get("DB_PATH"))
          else "The AI is already matching addresses.")
    return redirect(url_for("bases.page"))
