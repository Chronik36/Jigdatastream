"""BotTrack's launcher on Windows (the Start menu shortcut runs it with the app's own pythonw.exe).

Like BotTrack.app's launcher on a Mac: it picks the newest code (an in-app update, or the code shipped with this
install) and starts it. Everything BotTrack stores lives in %LOCALAPPDATA%\\BotTrack.
"""
import os
import runpy
import shutil
import sys
import time
from pathlib import Path

if os.environ.pop("BOTTRACK_RELAUNCH", None):
    time.sleep(2)  # an update restart: let the copy that started this one close first
B = Path(__file__).resolve().parent
D = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "BotTrack"
(D / "logs").mkdir(parents=True, exist_ok=True)
log = open(D / "logs" / "launch.log", "a", buffering=1, encoding="utf-8", errors="replace")
sys.stdout = sys.stderr = log  # pythonw has no console
try:
    shipped = (B / "app" / "VERSION").read_text().strip()
except OSError:
    shipped = ""
mark = D / "shipped-version"
# A newer installer replaces code that in-app updates installed for the previous one.
if (mark.read_text().strip() if mark.exists() else None) != shipped:
    for name in ("app", "pydeps", "backups"):
        shutil.rmtree(D / name, ignore_errors=True)
    mark.write_text(shipped + "\n")
code = D / "app" if (D / "app" / "jigstream" / "macapp.py").exists() else B / "app"
os.environ.update(BOTTRACK_BUNDLE=str(B), BOTTRACK_CODE_DIR=str(D / "app"), BOTTRACK_PIP_TARGET=str(D / "pydeps"),
                  PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.pathsep.join([str(code), str(D / "pydeps")]))
sys.path[:0] = [str(code), str(D / "pydeps")]
sys.dont_write_bytecode = True
print(time.strftime("%Y-%m-%d %H:%M:%S"), "starting BotTrack from", code)
runpy.run_module("jigstream.macapp", run_name="__main__", alter_sys=True)
