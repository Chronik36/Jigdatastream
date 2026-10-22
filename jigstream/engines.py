"""AI engines BotTrack can run, and a side-by-side model comparison on your own labelled emails.

Two free engines, both inside the app:
- Ollama (the default): models like huihui_ai/qwen3-abliterated:14b, kept in BotTrack's folder.
- MLX (Apple's own engine for Apple Silicon, via mlx-lm): usually faster on an M-series Mac. Its
  models come from Hugging Face; abliterated Qwen3 builds are found by searching there.

The comparison runs every corrected training example (from the Inbox page or order corrections)
through each model and reports how often it matched your answer and how long each email took.
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from .extract import SYSTEM_PROMPT
from .llm import LLMClient

logger = logging.getLogger("jigstream.engines")

OLLAMA_URL = os.environ.get("BOTTRACK_OLLAMA_URL", "http://127.0.0.1:11435")
MLX_PORT = 11436
MLX_URL = f"http://127.0.0.1:{MLX_PORT}"
OLLAMA_CANDIDATES = ["huihui_ai/qwen3-abliterated:4b", "huihui_ai/qwen3-abliterated:8b",
                     "huihui_ai/qwen3-abliterated:14b"]

_mlx = {"proc": None}
compare_state = {"running": False, "progress": "", "results": [], "error": None, "finished_at": None}


def mlx_available():
    try:
        import mlx_lm  # noqa: F401  (Apple Silicon only)
        return True
    except Exception:
        return False


def base_url(engine):
    return (MLX_URL if engine == "mlx" else OLLAMA_URL) + "/v1"


def start_mlx(models_dir):
    """Start mlx-lm's OpenAI-compatible server once; models download into BotTrack's folder."""
    if _mlx["proc"] and _mlx["proc"].poll() is None:
        return
    env = dict(os.environ, HF_HOME=str(models_dir / "huggingface"), HF_HUB_DISABLE_TELEMETRY="1")
    log = open(models_dir.parent / "logs" / "mlx-engine.log", "ab")
    _mlx["proc"] = subprocess.Popen([sys.executable, "-m", "mlx_lm.server", "--host", "127.0.0.1",
                                     "--port", str(MLX_PORT)], env=env, stdout=log, stderr=log)
    for _ in range(120):
        try:
            urllib.request.urlopen(MLX_URL + "/v1/models", timeout=2).close()
            return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("The MLX engine didn't start; see mlx-engine.log on the Logs page.")


def stop_mlx():
    if _mlx["proc"] and _mlx["proc"].poll() is None:
        _mlx["proc"].terminate()
        try:
            _mlx["proc"].wait(10)
        except subprocess.TimeoutExpired:
            _mlx["proc"].kill()


def mlx_download(model, models_dir, on_progress=lambda text: None):
    """Fetch an MLX model from Hugging Face into BotTrack's folder (skips files already there)."""
    os.environ["HF_HOME"] = str(models_dir / "huggingface")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from huggingface_hub import snapshot_download  # comes with mlx-lm
    on_progress(f"Downloading {model} from Hugging Face")
    snapshot_download(model)


def unload(engine, model):
    """Free the model's memory before the next one loads; a 16 GB Mac can't hold two at once."""
    if engine == "mlx":
        stop_mlx()
        return
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/generate", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"model": model, "keep_alive": 0}).encode())
        urllib.request.urlopen(req, timeout=30).close()
    except OSError:
        pass


_found = {}


def find_mlx_models(query="qwen3 abliterated", limit=30):
    """Abliterated Qwen3 builds converted for MLX on Hugging Face, smallest first (looked up once)."""
    if query in _found:
        return _found[query]
    url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
        {"search": query, "filter": "mlx", "sort": "downloads", "direction": "-1", "limit": limit})
    with urllib.request.urlopen(url, timeout=15) as r:
        ids = [m["id"] for m in json.load(r)]
    picks = [i for i in ids if "abliterat" in i.lower() and re.search(r"\b(4|8|14)b\b", i.lower().replace("-", " "))]

    def size(i):
        m = re.search(r"(\d+)b", i.lower())
        return int(m.group(1)) if m else 99
    _found[query] = sorted(picks, key=lambda i: (size(i), "4bit" not in i.lower(), i))
    return _found[query]


def mlx_choices():
    """One 4-bit MLX build per size (4B, 8B, 14B), or [] when offline or not on Apple Silicon."""
    if not mlx_available():
        return []
    try:
        found = find_mlx_models()
    except Exception:
        logger.warning("couldn't search Hugging Face for MLX models", exc_info=True)
        return []
    picks = {}
    for i in found:
        m = re.search(r"(\d+)b", i.lower())
        if m and m.group(1) in ("4", "8", "14") and re.search(r"4.?bit|q4", i.lower()):
            picks.setdefault(m.group(1), i)
    return [picks[k] for k in ("4", "8", "14") if k in picks]


def ollama_pull(model, on_progress=lambda pct: None):
    req = urllib.request.Request(OLLAMA_URL + "/api/pull", data=json.dumps({"model": model}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=None) as r:
        for line in r:
            event = json.loads(line or b"{}")
            if event.get("error"):
                raise RuntimeError(event["error"])
            if event.get("total"):
                on_progress(int(100 * event.get("completed", 0) / event["total"]))


def ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except OSError:
        return []


def _same(answer, expected):
    if answer.get("kind") != expected.get("kind"):
        return False
    if expected.get("kind") != "order":
        return True
    group = lambda s: "in_transit" if s in ("shipped", "in_transit") else s  # noqa: E731
    return group(answer.get("shipping_status")) == group(expected.get("shipping_status"))


def score(llm, examples):
    """(correct, total, seconds per email, unreadable replies) for one model."""
    right = bad = 0
    started = time.time()
    for ex in examples:
        try:
            answer = llm.chat_json(SYSTEM_PROMPT, ex["prompt"])
        except Exception:
            bad += 1
            continue
        right += _same(answer, json.loads(ex["corrected"]))
    n = len(examples)
    return right, n, (time.time() - started) / max(n, 1), bad


def compare(store, candidates, models_dir, current=None, limit=30, timeout=300):
    """candidates: [(engine, model)]; current: the (engine, model) in use, unloaded first to make room.
    Runs in the background; results go to compare_state."""
    compare_state.update(running=True, results=[], error=None, progress="Starting")
    try:
        examples = [e for e in store.examples() if e["corrected"]][:limit]
        if len(examples) < 5:
            raise RuntimeError("Label at least 5 emails on the Inbox page first, so there's something to score.")
        if current:
            unload(*current)
        for engine, model in candidates:
            name = f"{model} ({'MLX' if engine == 'mlx' else 'Ollama'})"
            try:
                if engine == "mlx":
                    mlx_download(model, models_dir, lambda t, n=name: compare_state.update(progress=f"{n}: {t}"))
                    compare_state["progress"] = f"{name}: starting the MLX engine"
                    start_mlx(models_dir)
                elif model not in ollama_models():
                    ollama_pull(model, lambda p, n=name: compare_state.update(progress=f"{n}: downloading {p}%"))
                llm = LLMClient(base_url(engine), model, "local", timeout)
                compare_state["progress"] = f"{name}: warming up"
                score(llm, examples[:1])  # loads the model so its load time isn't counted
                compare_state["progress"] = f"{name}: reading {len(examples)} labelled emails"
                right, n, secs, bad = score(llm, examples)
                compare_state["results"].append({"engine": engine, "model": model, "right": right, "total": n,
                                                 "accuracy": round(100 * right / n), "seconds": round(secs, 1),
                                                 "unreadable": bad})
                logger.info("compare %s: %d/%d right, %.1fs per email", name, right, n, secs)
            except Exception as e:
                logger.exception("compare %s failed", name)
                compare_state["results"].append({"engine": engine, "model": model, "error": str(e)})
            finally:
                unload(engine, model)
        compare_state["progress"] = "Done"
        if current and current[0] == "mlx":
            start_mlx(models_dir)  # the app's own engine was stopped to make room
    except Exception as e:
        compare_state["error"] = str(e)
    finally:
        compare_state.update(running=False, finished_at=time.strftime("%Y-%m-%d %H:%M"))


def best(results):
    """The most accurate model, the faster one on a tie."""
    ok = [r for r in results if "error" not in r]
    return max(ok, key=lambda r: (r["accuracy"], -r["seconds"])) if ok else None


def start_compare(store, candidates, models_dir, current=None):
    if compare_state["running"]:
        return False
    threading.Thread(target=compare, args=(store, candidates, models_dir, current), daemon=True).start()
    return True
