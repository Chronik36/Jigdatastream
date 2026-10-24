"""Undo and Redo for anything you change in the app, as many steps back and forward as you like.

Every page action that saves something (a POST from the window) is one step. While it runs, each database
connection it opens gets temporary triggers that write down, for every row it adds, changes or deletes, the SQL that
puts the row back. Undo runs a step's SQL newest first; the triggers record the opposite while it does, and that
becomes the Redo step. A new change after an undo clears Redo, like any editor.

Left out: the sync and other background work (no page action behind them), settings kept in .env, and anything
already sent outside the Mac (an email, a provider's API). Only the last KEEP steps are kept.
"""
import re
import time

from flask import Blueprint, current_app, flash, g, has_request_context, jsonify, redirect, request, session

bp = Blueprint("undo", __name__)

KEEP = 100
SCHEMA = """
CREATE TABLE IF NOT EXISTS undo_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, grp INTEGER, sql TEXT);
CREATE INDEX IF NOT EXISTS undo_log_grp ON undo_log (grp);
CREATE TABLE IF NOT EXISTS undo_steps (grp INTEGER PRIMARY KEY, kind TEXT, label TEXT, page TEXT, at TEXT);
"""
# Bookkeeping, not your data: the undo tables themselves and the phone app's change counter.
SKIP = {"undo_log", "undo_steps", "change_count", "sqlite_sequence"}
# Page actions that aren't edits: syncing, checks, settings, sending logs, the phone app, and Undo/Redo themselves.
NOT_STEPS = re.compile(r"^/(undo|redo|sync|logs|m/|prefs|update|whats-new|setup|api/|tracking/check|proxies/test|"
                       r"proxies/\d+/locate|cards/locks/(run|scan|open|stop)|bank/plaid/(sync|link-token)|"
                       r"training/train|training/tasks/\d+/(try|run)|products/sources/test|keywords$|keywords/ai|stock$|proxies/suggest)")
_triggers = {}  # (database file, schema version) -> trigger SQL


def _tables(con):
    rows = con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    virtual = [r[0] for r in rows if (r[1] or "").upper().startswith("CREATE VIRTUAL")]
    for name, _ in rows:
        if name in SKIP or name.startswith(("sqlite_", "sync_")) or name in virtual or any(name.startswith(v + "_") for v in virtual):
            continue
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{name}")')]
        if cols:
            yield name, cols


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def trigger_sql(con):
    """TEMP triggers for every table that log the SQL undoing each change into undo_log, under temp.undo_cur's group."""
    key = (con.execute("PRAGMA database_list").fetchone()[2], con.execute("PRAGMA schema_version").fetchone()[0])
    if key in _triggers:
        return _triggers[key]
    out = []
    for i, (t, cols) in enumerate(_tables(con)):
        qt = _q(t).replace("'", "''")
        names = ", ".join(_q(c) for c in cols).replace("'", "''")
        olds = "||','||".join(f"quote(OLD.{_q(c)})" for c in cols)
        sets = "||".join(f"CASE WHEN OLD.{_q(c)} IS NOT NEW.{_q(c)} THEN '{_q(c).replace(chr(39), chr(39) * 2)}='||quote(OLD.{_q(c)})||',' ELSE '' END"
                         for c in cols)
        changed = " OR ".join(f"OLD.{_q(c)} IS NOT NEW.{_q(c)}" for c in cols)
        log = "INSERT INTO undo_log (grp, sql) SELECT grp, {} FROM undo_cur"
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS undo_{i}_i AFTER INSERT ON main.{_q(t)} BEGIN "
                   + log.format(f"'DELETE FROM {qt} WHERE rowid='||NEW.rowid") + "; END")
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS undo_{i}_d BEFORE DELETE ON main.{_q(t)} BEGIN "
                   + log.format(f"'INSERT OR REPLACE INTO {qt} (rowid, {names}) VALUES ('||OLD.rowid||','||{olds}||')'") + "; END")
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS undo_{i}_u BEFORE UPDATE ON main.{_q(t)} WHEN {changed} BEGIN "
                   + log.format(f"'UPDATE {qt} SET '||rtrim({sets}, ',')||' WHERE rowid='||OLD.rowid") + "; END")
    _triggers[key] = out
    return out


def record(con, grp):
    """From now on, every change on this connection is written down under step grp."""
    con.executescript(SCHEMA)
    con.execute("PRAGMA recursive_triggers = ON")  # so INSERT OR REPLACE logs the row it replaces
    con.execute("CREATE TEMP TABLE IF NOT EXISTS undo_cur (grp INTEGER)")
    con.execute("DELETE FROM undo_cur")
    con.execute("INSERT INTO undo_cur VALUES (?)", (grp,))
    for sql in trigger_sql(con):
        con.execute(sql)
    return con


def attach(con):
    """Called by db.connect: during a page action that edits, record this connection's changes as one step."""
    if not has_request_context() or request.method != "POST" or NOT_STEPS.match(request.path):
        return con
    if "undo_grp" not in g:
        g.undo_grp = time.time_ns() // 1000
        g.undo_cons = []
    record(con, g.undo_grp)
    g.undo_cons.append(con)
    return con


def _label():
    flashes = session.get("_flashes") or []
    if flashes:
        return str(flashes[-1][1])[:160]
    words = [w for w in request.path.strip("/").split("/") if not w.isdigit()]
    return " ".join(words).replace("-", " ").capitalize()[:160] or "Change"


def finish(response):
    """After a page action: keep its step if it changed anything, and clear Redo (a new change ends that branch)."""
    grp = g.pop("undo_grp", None)
    if grp is None:
        return response
    cons = g.pop("undo_cons", [])
    con = cons[0]
    try:
        for c in cons:
            c.execute("DELETE FROM undo_cur")
        if con.execute("SELECT 1 FROM undo_log WHERE grp = ? LIMIT 1", (grp,)).fetchone():
            con.execute("INSERT OR REPLACE INTO undo_steps (grp, kind, label, page, at) VALUES (?, 'undo', ?, ?, ?)",
                        (grp, _label(), request.referrer or "", time.strftime("%Y-%m-%dT%H:%M:%S")))
            _drop(con, "kind = 'redo'")
            old = [r[0] for r in con.execute("SELECT grp FROM undo_steps WHERE kind = 'undo' ORDER BY grp DESC LIMIT -1 OFFSET ?",
                                             (KEEP,))]
            for o in old:
                _drop(con, "grp = ?", (o,))
    except Exception:  # never break the page over the undo history
        current_app.logger.exception("undo: couldn't save the step")
    return response


def _drop(con, where, args=()):
    grps = [r[0] for r in con.execute(f"SELECT grp FROM undo_steps WHERE {where}", args)]
    con.executemany("DELETE FROM undo_log WHERE grp = ?", [(x,) for x in grps])
    con.executemany("DELETE FROM undo_steps WHERE grp = ?", [(x,) for x in grps])


def top(con, kind):
    con.executescript(SCHEMA)
    row = con.execute("SELECT * FROM undo_steps WHERE kind = ? ORDER BY grp DESC LIMIT 1", (kind,)).fetchone()
    return dict(row) if row else None


def state(con):
    u, r = top(con, "undo"), top(con, "redo")
    return {"undo": u and u["label"], "redo": r and r["label"],
            "undo_count": con.execute("SELECT COUNT(*) FROM undo_steps WHERE kind = 'undo'").fetchone()[0],
            "redo_count": con.execute("SELECT COUNT(*) FROM undo_steps WHERE kind = 'redo'").fetchone()[0]}


def step(con, kind):
    """Undo (kind 'undo') or redo the newest step; what it reverses is saved as the step the other way.
    Returns the step, or None when there's nothing to do."""
    s = top(con, kind)
    if not s:
        return None
    back = "redo" if kind == "undo" else "undo"
    grp = max(time.time_ns() // 1000, s["grp"] + 1)
    sqls = [r[0] for r in con.execute("SELECT sql FROM undo_log WHERE grp = ? ORDER BY seq DESC", (s["grp"],))]
    record(con, grp)
    con.execute("BEGIN IMMEDIATE")
    try:
        for sql in sqls:
            con.execute(sql)
        con.execute("DELETE FROM undo_cur")
        _drop(con, "grp = ?", (s["grp"],))
        con.execute("INSERT INTO undo_steps (grp, kind, label, page, at) VALUES (?, ?, ?, ?, ?)",
                    (grp, back, s["label"], s["page"], time.strftime("%Y-%m-%dT%H:%M:%S")))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute("DELETE FROM undo_cur")
        raise
    return s


def _go(kind):
    from . import db
    con = db.connect(current_app.config.get("DB_PATH"))
    try:
        s = step(con, kind)
    except Exception as e:  # e.g. the row was changed by a sync since; say so rather than half-undo
        current_app.logger.exception("undo failed")
        s, err = None, str(e)
    else:
        err = None
    wants_json = request.accept_mimetypes.best == "application/json" or request.is_json
    if wants_json:
        return jsonify({"done": s and s["label"], "error": err, **state(con)})
    if err:
        flash(f"Couldn't {kind} that: {err}")
    elif s:
        flash(("Undid: " if kind == "undo" else "Redid: ") + s["label"])
    else:
        flash("Nothing to " + kind + ".")
    return redirect(request.referrer or "/")


@bp.post("/undo")
def undo():
    return _go("undo")


@bp.post("/redo")
def redo():
    return _go("redo")


@bp.get("/undo/state")
def undo_state():
    from . import db
    return jsonify(state(db.connect(current_app.config.get("DB_PATH"))))


def init_app(app):
    app.register_blueprint(bp)
    app.after_request(finish)

    @app.context_processor
    def undo_flags():
        if not has_request_context() or request.path.startswith("/m/"):
            return {}
        try:
            from . import db
            return {"undo_state": state(db.connect(app.config.get("DB_PATH")))}
        except Exception:
            return {}
