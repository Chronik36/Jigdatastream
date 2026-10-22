"""Saved lists of your email addresses, in named groups, to fill profiles from.

Only addresses are kept: no passwords and no sign-in to these accounts. A group can feed the
Email field in Mass Distribute on the Profiles page.
"""
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger("jigstream.emailbook")

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_groups (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS saved_emails (
    id INTEGER PRIMARY KEY,
    group_name TEXT,
    address TEXT,
    created_at TEXT,
    UNIQUE (group_name, address)
);
"""
ADDRESS = re.compile(r"[^@\s:;,]+@[^@\s:;,]+\.[A-Za-z]{2,}")


def parse(text):
    """Addresses found in pasted text or an uploaded .txt/.csv (first address on each line)."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        m = ADDRESS.search(line)
        if m and m.group(0).lower() not in seen:
            seen.add(m.group(0).lower())
            out.append(m.group(0))
    return out


def add(con, group, addresses):
    """Returns (added, already in the group)."""
    group = group or ""
    if group:
        con.execute("INSERT OR IGNORE INTO email_groups VALUES (?)", (group,))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO saved_emails (group_name, address, created_at) VALUES (?,?,?)",
                    [(group, a, now) for a in addresses])
    con.commit()
    added = con.total_changes - before
    logger.info("added %d email address(es) to %r", added, group)
    return added, len(addresses) - added


def emails(con, group=None):
    """Addresses with the profile using each one (matched by the profile's email), newest first."""
    q = ("SELECT e.*, (SELECT GROUP_CONCAT(p.name, ', ') FROM profiles p WHERE lower(p.email) = lower(e.address))"
         " AS profile FROM saved_emails e" + (" WHERE e.group_name = ?" if group is not None else "") + " ORDER BY e.id DESC")
    return [dict(r) for r in con.execute(q, (group,) if group is not None else ())]


def groups(con):
    named = {r[0] for r in con.execute("SELECT name FROM email_groups")}
    counts = {r[0]: r[1] for r in con.execute("SELECT group_name, COUNT(*) FROM saved_emails GROUP BY group_name")}
    return [(g, counts.get(g, 0)) for g in sorted(named | {g for g in counts if g}, key=str.lower)], counts.get("", 0)


def addresses(con, group, unused_only=False):
    """A group's addresses in the order they were added, for Mass Distribute or export."""
    rows = emails(con, group)[::-1]
    return [r["address"] for r in rows if not (unused_only and r["profile"])]


def update(con, email_id, address):
    if not ADDRESS.fullmatch(address or ""):
        return "That doesn't look like an email address."
    try:
        con.execute("UPDATE saved_emails SET address=? WHERE id=?", (address, email_id))
    except Exception:
        return "That address is already in this group."
    con.commit()
    return None


def move(con, ids, group):
    if group:
        con.execute("INSERT OR IGNORE INTO email_groups VALUES (?)", (group,))
    con.executemany("UPDATE OR IGNORE saved_emails SET group_name=? WHERE id=?", [(group or "", i) for i in ids])
    con.commit()


def delete(con, ids):
    con.executemany("DELETE FROM saved_emails WHERE id=?", [(i,) for i in ids])
    con.commit()


def rename_group(con, old, new):
    con.execute("INSERT OR IGNORE INTO email_groups VALUES (?)", (new,))
    con.execute("UPDATE OR IGNORE saved_emails SET group_name=? WHERE group_name=?", (new, old))
    con.execute("DELETE FROM email_groups WHERE name=?", (old,))
    con.commit()


def delete_group(con, name, keep_emails=True):
    if keep_emails:
        con.execute("UPDATE OR IGNORE saved_emails SET group_name='' WHERE group_name=?", (name,))
    con.execute("DELETE FROM saved_emails WHERE group_name=?", (name,))
    con.execute("DELETE FROM email_groups WHERE name=?", (name,))
    con.commit()
