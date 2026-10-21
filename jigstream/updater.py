"""Check the git remote for new commits and fast-forward the local checkout.

Only ever fast-forwards (never merges, resets or discards local changes), and refuses to update
when the working tree has uncommitted changes to tracked files.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args, root=ROOT):
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


def status(root=ROOT):
    code, branch = _git("rev-parse", "--abbrev-ref", "HEAD", root=root)
    if code:
        return {"ok": False, "error": branch}
    code, out = _git("fetch", "origin", branch, root=root)
    if code:
        return {"ok": False, "error": out}
    _, behind = _git("rev-list", "--count", f"HEAD..origin/{branch}", root=root)
    _, log = _git("log", "--format=%h %s", f"HEAD..origin/{branch}", root=root)
    _, dirty = _git("status", "--porcelain", "--untracked-files=no", root=root)
    return {"ok": True, "branch": branch, "behind": int(behind or 0), "changes": log.splitlines(),
            "dirty": bool(dirty)}


def apply(root=ROOT):
    st = status(root)
    if not st["ok"] or not st["behind"]:
        return st
    if st["dirty"]:
        return {**st, "ok": False, "error": "Local uncommitted changes; not updating."}
    code, out = _git("merge", "--ff-only", f"origin/{st['branch']}", root=root)
    if code:
        return {**st, "ok": False, "error": out}
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=root)
    return {**st, "ok": True, "updated": True}


def restart():
    os.execv(sys.executable, [sys.executable] + sys.argv)
