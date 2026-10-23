"""Phone link: a free Cloudflare quick tunnel from a public https address to BotTrack on this Mac.

Phones reach BotTrack from anywhere through https://<random words>.trycloudflare.com with no app
or account on either side. BotTrack downloads Cloudflare's `cloudflared` the first time phone
access is turned on (checked against a pinned checksum), runs it in the background, and reads
the address it is given from its log. Every phone request still has to pair or sign in
(jigstream/mobile.py).

The address changes whenever cloudflared starts again, so cloudflared keeps running when BotTrack
restarts for an update and only stops when BotTrack is closed. Each new address is also posted
to a private, randomly named ntfy.sh topic that only paired phones know, so the Android app can
find the Mac again by itself.
"""
import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("jigstream.tunnel")

VERSION = "2026.10.2"
DOWNLOAD = "https://github.com/cloudflare/cloudflared/releases/download/%s/cloudflared-darwin-%s.tgz"
SHA256 = {"arm64": "529a07089ac2d46762578389230fa148e5cb6ce2689200fc006956864042d17a",
          "amd64": "b64efd5a2772fc124b855f60eb3d33ba1a4f2ba67b19ddc58d32b705d42b82c9"}
URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
NTFY = "https://ntfy.sh/"
ANNOUNCE_EVERY = 6 * 3600  # ntfy.sh keeps messages for 12 hours

# Kept in BotTrack's data folder (the working directory of the Mac app).
BIN, STATE, LOG = Path("bin/cloudflared"), Path("tunnel.json"), Path("logs/tunnel.log")

state = {"status": "off", "url": "", "error": ""}  # status: off, starting, on, error
_gen = [0]  # bumped by ensure() and stop(); an older watcher thread notices and ends


def _arch():
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"


def binary(get=urllib.request.urlopen):
    """Our copy of cloudflared, downloading it the first time; or one installed with Homebrew."""
    if BIN.exists():
        return str(BIN)
    found = shutil.which("cloudflared")
    if found:
        return found
    arch = _arch()
    logger.info("downloading cloudflared %s (%s)", VERSION, arch)
    with get(DOWNLOAD % (VERSION, arch), timeout=120) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != SHA256[arch]:
        raise RuntimeError("the cloudflared download didn't match its checksum")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        exe = tar.extractfile("cloudflared").read()
    BIN.parent.mkdir(parents=True, exist_ok=True)
    tmp = BIN.with_suffix(".part")
    tmp.write_bytes(exe)
    tmp.chmod(0o755)
    tmp.replace(BIN)
    return str(BIN)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    try:  # the pid could belong to something else after a reboot
        out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True
    return "cloudflared" in out


def _saved():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def url_in_log():
    try:
        found = URL.findall(LOG.read_text(errors="replace"))
    except OSError:
        return ""
    return found[-1] if found else ""


def _start(port):
    exe = binary()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "wb") as log:
        # Its own session, so it keeps running (and keeps its address) while BotTrack restarts.
        p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                             stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    STATE.write_text(json.dumps({"pid": p.pid, "port": port}))
    logger.info("started cloudflared (pid %s) for port %s", p.pid, port)
    return p.pid


def ensure(port):
    """Make sure a tunnel to this port is running, reusing one left from before a restart."""
    _gen[0] += 1
    state.update(status="starting", url="", error="")
    threading.Thread(target=_watch, args=(port, _gen[0]), daemon=True).start()


def _watch(port, gen):
    pid, announced, last_post, wait = None, "", 0, 5
    saved = _saved()
    if saved.get("port") == port and _alive(saved.get("pid")):
        pid = saved["pid"]
        logger.info("reusing cloudflared (pid %s)", pid)
    elif saved.get("pid") and _alive(saved["pid"]):
        _kill(saved["pid"])  # left over for another port
    while gen == _gen[0]:
        try:
            if not pid or not _alive(pid):
                if pid:
                    logger.warning("cloudflared stopped; starting it again")
                    state.update(status="starting", url="")
                pid = _start(port)
            url = url_in_log()
            if url:
                state.update(status="on", url=url, error="")
                wait = 5
                if url != announced or time.time() - last_post > ANNOUNCE_EVERY:
                    announce(url)
                    announced, last_post = url, time.time()
        except Exception as e:  # no internet, or the download failed: try again a bit later
            logger.warning("phone link problem: %s", e)
            state.update(status="error", error=str(e))
            wait = min(wait * 2, 300)
        time.sleep(wait if state["status"] == "error" else 3)


def announce(url, post=urllib.request.urlopen):
    """Post the current address to this Mac's private topic, where the Android app looks for it."""
    topic = os.environ.get("PHONE_PAIR_TOPIC", "")
    if not topic:
        return
    try:
        req = urllib.request.Request(NTFY + topic, data=url.encode(), method="POST",
                                     headers={"Title": "BotTrack", "Cache": "yes"})
        post(req, timeout=15).close()
        logger.info("posted the new phone link")
    except Exception as e:
        logger.warning("couldn't post the phone link to ntfy.sh: %s", e)


def _kill(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def stop():
    """Close the tunnel (phone access turned off, or BotTrack closed)."""
    _gen[0] += 1
    state.update(status="off", url="", error="")
    saved = _saved()
    if saved.get("pid") and _alive(saved["pid"]):
        _kill(saved["pid"])
        logger.info("stopped cloudflared")
    STATE.unlink(missing_ok=True)
