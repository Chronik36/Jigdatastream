"""BotTrack's entry point on a Mac (BotTrack.app) and on Windows: everything runs inside the app and one data folder.

The app carries its own Python, this code, and an Ollama engine. Nothing is installed elsewhere: settings, the
database, logs, code updates and AI models live in ~/Library/Application Support/BotTrack on a Mac and in
%LOCALAPPDATA%\BotTrack on Windows. Deleting the app (and that folder) removes everything.

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
WINDOWS = sys.platform == "win32"


def default_data():
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData/Local") / "BotTrack"
    return HOME / "Library/Application Support/BotTrack"


DATA = Path(os.environ.get("BOTTRACK_HOME") or default_data())
RESTART_MARK = DATA / ".restarting"  # left by an update or model switch for the next launch
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
    if WINDOWS or not old.exists() or (data / ".migrated").exists():
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


LOG_KEEP_BYTES = 5 * 1024 * 1024
LOG_MAX_AGE_DAYS = 30


def cleanup(data=DATA, home=HOME, running_bundled_code=True):
    """Remove what BotTrack no longer needs. Never touches .env, data/ (orders, profiles, inbox
    labels, training examples) or the AI model in use. Returns what was removed, for the log."""
    removed = []
    from . import updater
    for old in sorted((data / "backups").glob("app-*"), reverse=True)[updater.KEEP_BACKUPS:] \
            if (data / "backups").exists() else []:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    for leftover in (data / "app.new",):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
            removed.append(leftover.name)
    if running_bundled_code and (data / "pydeps").exists():  # packages for an update that's been undone
        shutil.rmtree(data / "pydeps", ignore_errors=True)
        removed.append("pydeps")
    for stale in (home / "Applications/Order Tracker.app", home / f"Library/LaunchAgents/{OLD_AGENT}.plist"):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True) if stale.is_dir() else stale.unlink()
            removed.append(stale.name)
    logs = data / "logs"
    if logs.exists():
        cutoff = time.time() - LOG_MAX_AGE_DAYS * 86400
        for f in logs.iterdir():
            if not f.is_file():
                continue
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed.append(f"logs/{f.name}")
            elif f.suffix == ".log" and f.name != "app.log" and f.stat().st_size > LOG_KEEP_BYTES:
                with open(f, "rb") as fh:  # keep the recent end of logs that don't rotate themselves
                    fh.seek(-LOG_KEEP_BYTES // 2, os.SEEK_END)
                    tail = fh.read()
                f.write_bytes(tail)
                removed.append(f"older part of logs/{f.name}")
    if removed:
        logger.info("cleaned up: %s", ", ".join(removed))
    return removed


def remove_unused_models(keep):
    """Delete models in BotTrack's own folder other than the ones in use (e.g. after switching models)."""
    try:
        with _api("/api/tags") as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
        for name in names:
            if name not in keep and name.removesuffix(":latest") not in keep:
                req = urllib.request.Request(f"http://127.0.0.1:{AI_PORT}/api/delete", method="DELETE",
                                             data=json.dumps({"model": name}).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=30).close()
                logger.info("removed unused AI model %s", name)
    except Exception:
        logger.warning("couldn't tidy up unused AI models", exc_info=True)


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


def resources(bundle):
    """Where the app keeps its Python, code and engines: inside BotTrack.app, or the Windows install folder."""
    return bundle if WINDOWS else bundle / "Contents/Resources"


def no_window():
    """Popen flags that keep a Windows console from flashing up for a background program."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


def start_ai(bundle, models_dir, model):
    """Run the bundled Ollama on a private port with its models in BotTrack's folder."""
    engine = resources(bundle) / "ollama" / ("ollama.exe" if WINDOWS else "ollama")
    # Tuned for one 14B model on a 16 GB Mac. 8K context fits the prompt, a long email and two examples
    # (the 4K default cut prompts short); flash attention and an 8-bit cache keep that context small and fast;
    # one model and one request at a time, so a second copy never pushes the Mac into swap.
    env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{AI_PORT}", OLLAMA_MODELS=str(models_dir),
               OLLAMA_KEEP_ALIVE="10m", OLLAMA_NOPRUNE="1", OLLAMA_CONTEXT_LENGTH="8192",
               OLLAMA_FLASH_ATTENTION="1", OLLAMA_KV_CACHE_TYPE="q8_0", OLLAMA_NUM_PARALLEL="1",
               OLLAMA_MAX_LOADED_MODELS="1")
    log = open(DATA / "logs" / "ai-engine.log", "ab")
    proc = subprocess.Popen([str(engine), "serve"], env=env, stdout=log, stderr=log, **no_window())

    def prepare():
        try:
            for _ in range(60):
                try:
                    _api("/api/version", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.5)
            if model is None:  # MLX is the engine in use; Ollama only serves model comparisons
                remove_unused_models(set(filter(None, os.environ.get("KEEP_MODELS", "").split(","))))
                return
            if not has_model(model) and not copy_model_from_ollama(model, models_dir):
                pull(model)
            remove_unused_models({model} | set(filter(None, os.environ.get("KEEP_MODELS", "").split(","))))
            shutil.rmtree(models_dir / "huggingface", ignore_errors=True)  # MLX models, unused on Ollama
            ai_state.update(ready=True, message="", percent=None)
            logger.info("AI engine ready with %s", model)
        except Exception as e:
            logger.exception("AI engine setup failed")
            ai_state.update(error=f"The AI model couldn't be set up: {e}. Details are on the Logs page.")
    threading.Thread(target=prepare, daemon=True).start()
    return proc


def start_mlx_engine(models_dir, model):
    """Run the chosen model on Apple's MLX engine instead of Ollama."""
    from . import engines

    def prepare():
        try:
            hub = models_dir / "huggingface" / "hub"
            keep = "models--" + model.replace("/", "--")
            for old in hub.glob("models--*") if hub.exists() else ():
                if old.name != keep:  # MLX models tried in Compare models but not chosen
                    shutil.rmtree(old, ignore_errors=True)
                    logger.info("removed unused MLX model %s", old.name)
            engines.mlx_download(model, models_dir, lambda text: ai_state.update(message=text, percent=None))
            ai_state.update(message="Starting the MLX engine")
            engines.start_mlx(models_dir)
            ai_state.update(ready=True, message="", percent=None)
            logger.info("MLX engine ready with %s", model)
        except Exception as e:
            logger.exception("MLX engine setup failed")
            ai_state.update(error=f"The MLX model couldn't be set up: {e}. Switch back to Ollama on the Setup page.")
    threading.Thread(target=prepare, daemon=True).start()


def main():
    bundle = Path(os.environ.get("BOTTRACK_BUNDLE") or Path(sys.executable).resolve().parents[1 if WINDOWS else 4])
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
    try:
        cleanup(running_bundled_code=not (DATA / "app" / "jigstream").exists())
    except Exception:
        logger.exception("cleanup failed")
    load_dotenv(DATA / ".env")
    os.environ["LLM_BASE_URL"] = f"http://127.0.0.1:{AI_PORT}/v1"  # always a bundled engine
    from . import engines
    from .config import Config
    config = Config.from_env()
    model = config.llm_model
    models_dir = DATA / "models"
    models_dir.mkdir(exist_ok=True)

    use_mlx = config.llm_engine == "mlx"
    if use_mlx and not engines.mlx_available():
        logger.warning("MLX isn't available on this Mac; using Ollama with the default model")
        use_mlx, model = False, Config.llm_model
        os.environ["LLM_ENGINE"], os.environ["LLM_MODEL"] = "ollama", model
    ai = start_ai(bundle, models_dir, None if use_mlx else model)
    if use_mlx:
        os.environ["LLM_BASE_URL"] = engines.base_url("mlx")
        start_mlx_engine(models_dir, model)
    from . import carriers, db, updater
    from .app import create_app

    def relaunch():
        RESTART_MARK.touch()  # the next launch opens on the loading screen, then Setup
        ai.terminate()
        engines.stop_mlx()
        if WINDOWS:  # the launcher next to the app's Python picks the new code, like BotTrack.app's does
            subprocess.Popen([str(bundle / "python" / "pythonw.exe"), str(bundle / "BotTrack.pyw")],
                             env=dict(os.environ, BOTTRACK_RELAUNCH="1"),  # it waits for this copy to close
                             creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
        else:
            subprocess.Popen(["/bin/sh", "-c", f"sleep 2; open -n '{bundle}'"], start_new_session=True)
    updater.relaunch = relaunch
    app = create_app()
    app.config["AI_STATE"] = ai_state
    port = free_port(int(os.environ.get("PORT", 8765)))
    # Phones come in through the phone link, which connects to 127.0.0.1; only phones on the same
    # Wi-Fi need BotTrack listening on the network. jigstream/mobile.py checks every request.
    phones = os.environ.get("PHONE_ACCESS") == "1"
    host = "0.0.0.0" if phones and os.environ.get("PHONE_ALLOW_LAN") == "1" else "127.0.0.1"
    app.config.update(PORT=port, BIND_HOST=host, PHONE_LINK=True)
    threading.Thread(target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
                     daemon=True).start()
    from . import peersync, tunnel
    if phones or (os.environ.get("SYNC_LINK") == "1" and peersync.link_wanted(app.config.get("DB_PATH"))):
        tunnel.ensure(port)  # reuses the link left running by an update restart, so its address stays
    else:
        tunnel.stop()
    peersync.start(app)  # keeps a linked Mac or PC the same as this one
    from . import backup
    backup.start(app)  # a locked backup every day, once a backup password is set

    def timers():
        """Sync every few hours (first once the AI is ready) and check for updates every few hours."""
        sync_every = float(os.environ.get("SYNC_EVERY_HOURS", 4)) * 3600
        check_every = float(os.environ.get("UPDATE_CHECK_HOURS", 6)) * 3600
        track_every = float(os.environ.get("TRACK_EVERY_HOURS", 3)) * 3600
        last_sync = last_check = last_track = 0
        time.sleep(30)
        while True:
            now = time.time()
            if ai_state["ready"] and sync_every > 0 and now - last_sync >= sync_every:
                try:
                    app.start_sync()
                except Exception:
                    logger.exception("scheduled sync failed to start")
                last_sync = now
            if track_every > 0 and now - last_track >= track_every and carriers.available(os.environ):
                # its own thread: reading tracking pages one at a time can take several minutes
                threading.Thread(target=carriers.run, args=(db.connect(), dict(os.environ)), daemon=True).start()
                last_track = now
            if check_every > 0 and now - last_check >= check_every:
                updater.check()
                last_check = now
            time.sleep(60)
    threading.Thread(target=timers, daemon=True).start()

    from .window import open_window
    restarted = RESTART_MARK.exists()
    RESTART_MARK.unlink(missing_ok=True)
    try:
        open_window(f"http://127.0.0.1:{port}", resources(bundle) / ("BotTrack.ico" if WINDOWS else "AppIcon.icns"),
                    restarted=restarted)
    finally:
        # The phone link keeps running after BotTrack closes, so it keeps the same address and paired
        # phones (iPhones too) reconnect by themselves when BotTrack opens again. Turning phone access
        # off stops it; restarting the Mac gives it a new address, which the Android app follows.
        engines.stop_mlx()
        ai.terminate()
        try:
            ai.wait(10)
        except subprocess.TimeoutExpired:
            ai.kill()
        logger.info("BotTrack closed")


if __name__ == "__main__":
    main()
