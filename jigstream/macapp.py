"""BotTrack.app's entry point: everything runs inside the app and one data folder.

The app bundle carries its own Python, this code, and an Ollama engine. Nothing is installed
elsewhere: settings, the database, logs, code updates and AI models live in
~/Library/Application Support/BotTrack. Deleting the app (and that folder) removes everything.

While the window is open BotTrack serves the dashboard, runs its own AI engine on a private
port, syncs on a timer and checks for updates. Closing the window stops all of it.
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
DATA = Path(os.environ.get("BOTTRACK_HOME", HOME / "Library/Application Support/BotTrack"))
OLD_HOME = HOME / "Library/Application Support/OrderTracker"
OLD_AGENT = "com.jigdatastream.ordertracker"
AI_PORT = 11435  # not Ollama's usual 11434, so a separately installed Ollama never clashes
logger = logging.getLogger("jigstream.macapp")

# What the dashboard shows while the AI engine starts or a model downloads.
ai_state = {"ready": False, "message": "Starting the AI engine...", "percent": None, "error": None}


def free_port(preferred):
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue


def migrate_old_install(data=DATA, old=OLD_HOME, run=subprocess.run):
    """Bring settings and data over from the earlier installer version, then remove its pieces."""
    if not old.exists() or (data / ".migrated").exists():
        return False
    logger.info("moving data from the old Order Tracker install at %s", old)
    data.mkdir(parents=True, exist_ok=True)
    for name in (".env", "data", "logs"):
        src, dst = old / name, data / name
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
    env = data / ".env"
    if env.exists():  # earlier versions defaulted to a smaller model
        env.write_text(env.read_text().replace("LLM_MODEL=llama3.1:8b\n", ""))
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{OLD_AGENT}"], capture_output=True)
    (HOME / f"Library/LaunchAgents/{OLD_AGENT}.plist").unlink(missing_ok=True)
    for app in ("Order Tracker.app", "BotTrack.app"):
        shutil.rmtree(HOME / "Applications" / app, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    (data / ".migrated").write_text(time.strftime("%Y-%m-%d %H:%M:%S\n"))
    return True


def copy_model_from_ollama(model, models_dir, source=HOME / ".ollama/models"):
    """Reuse a model already downloaded by a separate Ollama install instead of downloading it again.
    On APFS `cp -c` makes copy-on-write clones, so this takes no extra disk space."""
    name, _, tag = model.partition(":")
    if "/" not in name:
        name = f"library/{name}"
    manifest = source / "manifests/registry.ollama.ai" / name / (tag or "latest")
    if not manifest.exists():
        return False
    layers = json.loads(manifest.read_text())
    digests = [layers["config"]["digest"]] + [l["digest"] for l in layers.get("layers", [])]
    blobs = [source / "blobs" / d.replace(":", "-") for d in digests]
    if not all(b.exists() for b in blobs):
        return False
    (models_dir / "blobs").mkdir(parents=True, exist_ok=True)
    for b in blobs:
        dst = models_dir / "blobs" / b.name
        if not dst.exists():
            subprocess.run(["cp", "-c", str(b), str(dst)], check=False) if sys.platform == "darwin" \
                else shutil.copy2(b, dst)
    dst_manifest = models_dir / manifest.relative_to(source)
    dst_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest, dst_manifest)
    logger.info("reused %s from %s", model, source)
    return True


def _api(path, payload=None, timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{AI_PORT}{path}",
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def has_model(model):
    with _api("/api/tags") as r:
        names = {m["name"] for m in json.load(r).get("models", [])}
    return model in names or f"{model}:latest" in names


def pull(model):
    """Download a model through the bundled engine, keeping ai_state up to date for the page banner."""
    ai_state.update(message=f"Downloading the AI model {model}", percent=0)
    with _api("/api/pull", {"model": model, "stream": True}, timeout=None) as r:
        for line in r:
            event = json.loads(line or b"{}")
            if event.get("error"):
                raise RuntimeError(event["error"])
            if event.get("total"):
                ai_state["percent"] = int(100 * event.get("completed", 0) / event["total"])
            if event.get("status") == "success":
                return


def start_ai(bundle, models_dir, model):
    """Run the bundled Ollama on a private port with its models in BotTrack's folder."""
    engine = bundle / "Contents/Resources/ollama/ollama"
    env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{AI_PORT}", OLLAMA_MODELS=str(models_dir),
               OLLAMA_KEEP_ALIVE="10m", OLLAMA_NOPRUNE="1")
    log = open(DATA / "logs" / "ai-engine.log", "ab")
    proc = subprocess.Popen([str(engine), "serve"], env=env, stdout=log, stderr=log)

    def prepare():
        try:
            for _ in range(60):
                try:
                    _api("/api/version", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.5)
            if not has_model(model) and not copy_model_from_ollama(model, models_dir):
                pull(model)
            ai_state.update(ready=True, message="", percent=None)
            logger.info("AI engine ready with %s", model)
        except Exception as e:
            logger.exception("AI engine setup failed")
            ai_state.update(error=f"The AI model couldn't be set up: {e}. Details are on the Logs page.")
    threading.Thread(target=prepare, daemon=True).start()
    return proc


def main():
    bundle = Path(os.environ.get("BOTTRACK_BUNDLE") or Path(sys.executable).resolve().parents[4])
    DATA.mkdir(parents=True, exist_ok=True)
    os.chdir(DATA)
    (DATA / "logs").mkdir(exist_ok=True)
    os.environ.setdefault("JIGSTREAM_LOGS", str(DATA / "logs"))

    from .config import load_dotenv
    from .logs import setup_logging
    setup_logging(DATA / "logs")
    try:
        migrate_old_install()
    except Exception:
        logger.exception("couldn't move data from the old install")
    load_dotenv(DATA / ".env")
    os.environ["LLM_BASE_URL"] = f"http://127.0.0.1:{AI_PORT}/v1"  # always the bundled engine
    from .config import Config
    model = Config.from_env().llm_model
    models_dir = DATA / "models"
    models_dir.mkdir(exist_ok=True)

    ai = start_ai(bundle, models_dir, model)
    from . import updater
    from .app import create_app

    def relaunch():
        ai.terminate()
        subprocess.Popen(["/bin/sh", "-c", f"sleep 2; open -n '{bundle}'"], start_new_session=True)
    updater.relaunch = relaunch
    app = create_app()
    app.config["AI_STATE"] = ai_state
    port = free_port(int(os.environ.get("PORT", 8765)))
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
                     daemon=True).start()

    def timers():
        """Sync every few hours (first once the AI is ready) and check for updates every few hours."""
        sync_every = float(os.environ.get("SYNC_EVERY_HOURS", 4)) * 3600
        check_every = float(os.environ.get("UPDATE_CHECK_HOURS", 6)) * 3600
        last_sync = last_check = 0
        time.sleep(30)
        while True:
            now = time.time()
            if ai_state["ready"] and sync_every > 0 and now - last_sync >= sync_every:
                try:
                    app.start_sync()
                except Exception:
                    logger.exception("scheduled sync failed to start")
                last_sync = now
            if check_every > 0 and now - last_check >= check_every:
                updater.check()
                last_check = now
            time.sleep(60)
    threading.Thread(target=timers, daemon=True).start()

    from .window import open_window
    try:
        open_window(f"http://127.0.0.1:{port}", bundle / "Contents/Resources/AppIcon.icns")
    finally:
        ai.terminate()
        try:
            ai.wait(10)
        except subprocess.TimeoutExpired:
            ai.kill()
        logger.info("BotTrack closed")


if __name__ == "__main__":
    main()
