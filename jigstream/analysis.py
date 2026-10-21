"""Join checkouts to profiles and surface fixable problems."""
from collections import defaultdict

from . import address as A


def profile_issues(p):
    issues = A.diagnose(p["ship_address1"], p["ship_address2"], p["ship_city"], p["ship_state"],
                        p["ship_zip"], p["ship_country"])
    if A.avs_key(p["ship_address1"], p["ship_zip"]) != A.avs_key(p["bill_address1"], p["bill_zip"]) \
            and p["bill_address1"]:
        issues.append("shipping and billing differ on street number/ZIP (AVS mismatch risk)")
    return issues


def profile_stats(con):
    stats = defaultdict(lambda: {"success": 0, "decline": 0, "failure": 0, "unknown": 0})
    for r in con.execute("SELECT profile,status FROM checkouts"):
        stats[r["profile"]][r["status"]] += 1
    return stats


def failure_report(con):
    """One row per profile with failures: stats, address issues, suggested fix."""
    stats = profile_stats(con)
    rows = []
    for p in con.execute("SELECT * FROM profiles"):
        s = stats.get(p["name"])
        if not s:
            continue
        issues = profile_issues(p)
        bad = s["decline"] + s["failure"]
        total = bad + s["success"]
        if not bad:
            continue
        rows.append({
            "profile": p["name"], **s, "total": total,
            "rate": round(100 * s["success"] / total) if total else 0,
            "issues": issues,
            "suggested": A.suggest(p["ship_address1"], p["ship_address2"], p["ship_city"],
                                   p["ship_state"], p["ship_zip"]) if issues else None,
        })
    unmatched = [n for n in stats if n and not con.execute("SELECT 1 FROM profiles WHERE name=?", (n,)).fetchone()]
    return sorted(rows, key=lambda r: r["rate"]), unmatched


def by_dimension(con, col):
    assert col in ("site", "bot", "profile")
    return con.execute(
        f"""SELECT {col} AS k, SUM(status='success') s, SUM(status='decline') d,
        SUM(status='failure') f, COUNT(*) n FROM checkouts GROUP BY {col} ORDER BY n DESC""").fetchall()


def cancelled_after_success(con):
    from .tracker import link
    return [r["checkout"] for r in link(con) if r["status"] == "cancelled"]
