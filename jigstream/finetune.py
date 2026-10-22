"""Local fine-tuning: teach a small model a task for real, on this Mac, for free.

Examples on the Training page only steer the model. A fine-tune changes it: mlx-lm trains a
LoRA adapter (a small add-on file, tens of MB) on top of an abliterated Qwen3 model from
Hugging Face. The base model is never modified, so an adapter can be deleted at any time.

On a 16 GB Mac this works for 4B models and usually for 8B; 14B needs more memory than
training leaves free. The app's own model is unloaded while training runs, and syncing
pauses until it's done.
"""
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import engines, logs

logger = logging.getLogger("jigstream.finetune")

MIN_EXAMPLES = 10
TRAINABLE_SIZES = ("4", "8")
# Both the older "Iter 10: Train loss 1.234" lines and the newer table rows "  10   1.234 ↓".
LOSS_LINE = re.compile(r"Iter (\d+): Train loss ([\d.]+)|^\s*(\d+)\s+([\d.]+)\s*[↓↑]", re.M)

state = {"running": False, "task": None, "progress": "", "iter": 0, "iters": 0, "loss": None, "error": None,
         "finished_at": None, "result": None}
_proc = {"p": None, "cancelled": False}


def base_choices():
    """4B and 8B abliterated MLX builds that fit training on a 16 GB Mac, or [] off Apple Silicon."""
    return [m for m in engines.mlx_choices() if re.search(r"\b(4|8)b\b", m.lower().replace("-", " "))]


def default_iters(n):
    """About three passes over the examples, within limits that finish in minutes, not hours."""
    return max(60, min(600, 3 * n))


def write_data(messages, folder, seed=0):
    """messages: chat examples ({"messages": [...]}). A tenth (at least one) is held back to check progress."""
    folder.mkdir(parents=True, exist_ok=True)
    rows = list(messages)
    random.Random(seed).shuffle(rows)
    n_valid = max(1, len(rows) // 10)
    valid, train = rows[:n_valid], rows[n_valid:] or rows
    for name, part in (("train", train), ("valid", valid)):
        (folder / f"{name}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in part))
    return len(train), len(valid)


def command(base, data, adapter, iters):
    return [sys.executable, "-m", "mlx_lm", "lora", "--model", base, "--train", "--data", str(data),
            "--adapter-path", str(adapter), "--iters", str(iters), "--batch-size", "1", "--num-layers", "8",
            "--max-seq-length", "2048", "--grad-checkpoint", "--mask-prompt", "--steps-per-report", "5",
            "--steps-per-eval", str(max(50, iters // 4)), "--learning-rate", "1e-5"]


def read_progress(line):
    m = LOSS_LINE.search(line)
    if not m:
        return None
    it, loss = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return int(it), float(loss)


def train(key, label, messages, base, models_dir, iters=None, current=None, on_done=None):
    """Runs in the background; progress goes to `state`. key names the task ("email" or "task-<id>").
    current: the (engine, model) the app is using, unloaded first to make room.
    on_done(result) is called with {"base", "path", "examples", "iters", "loss"} when training worked."""
    messages = list(messages)
    iters = iters or default_iters(len(messages))
    state.update(running=True, task=key, progress="Starting", iter=0, iters=iters, loss=None, error=None, result=None)
    _proc["cancelled"] = False
    restart_mlx = bool(current and current[0] == "mlx")
    try:
        if len(messages) < MIN_EXAMPLES:
            raise RuntimeError(f"Add at least {MIN_EXAMPLES} examples first (this task has {len(messages)}).")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = Path("models") / "adapters" / f"{key}-{stamp}"  # relative: the MLX engine runs from this folder too
        n_train, n_valid = write_data(messages, folder / "data")
        state["progress"] = f"Downloading {base}"
        engines.mlx_download(base, models_dir, lambda text: state.update(progress=text))
        if current:
            state["progress"] = "Freeing memory for training"
            engines.unload(*current)
        engines.stop_mlx()
        env = dict(os.environ, HF_HOME=str(models_dir / "huggingface"), HF_HUB_DISABLE_TELEMETRY="1",
                   PYTHONUNBUFFERED="1", TERM="dumb", COLUMNS="120")
        log_path = Path(logs.LOG_DIR) / "training.log"
        log_path.parent.mkdir(exist_ok=True)
        state["progress"] = f"Training {label} on {n_train} examples ({n_valid} held back to check it)"
        logger.info("fine-tune %s: %s, %d examples, %d iterations -> %s", key, base, len(messages), iters, folder)
        with open(log_path, "a") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M')} {label} on {base} ===\n")
            p = _proc["p"] = subprocess.Popen(command(base, folder / "data", folder, iters), env=env,
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in p.stdout:
                log.write(line)
                got = read_progress(line)
                if got:
                    state.update(iter=got[0], loss=got[1],
                                 progress=f"Training {label}: step {got[0]} of {iters}, loss {got[1]:.3f}")
            p.wait()
        if _proc["cancelled"]:
            raise RuntimeError("Training was stopped.")
        if p.returncode != 0 or not (folder / "adapters.safetensors").exists():
            raise RuntimeError("Training didn't finish; see training.log on the Logs page.")
        state["result"] = {"base": base, "path": str(folder), "examples": len(messages), "iters": iters,
                           "loss": state["loss"]}
        if on_done:
            on_done(state["result"])
        state["progress"] = f"Done. {label} is trained."
        logger.info("fine-tune %s done, final loss %s", key, state["loss"])
    except Exception as e:
        logger.exception("fine-tune %s failed", key)
        state["error"] = str(e)
    finally:
        _proc["p"] = None
        state.update(running=False, finished_at=time.strftime("%Y-%m-%d %H:%M"))
        if restart_mlx:
            try:
                engines.start_mlx(models_dir)  # the app's own engine was stopped to make room
            except Exception:
                logger.exception("couldn't restart the MLX engine after training")


def start(*args, **kwargs):
    if state["running"]:
        return False
    threading.Thread(target=train, args=args, kwargs=kwargs, daemon=True).start()
    return True


def cancel():
    if _proc["p"] and _proc["p"].poll() is None:
        _proc["cancelled"] = True
        _proc["p"].terminate()
