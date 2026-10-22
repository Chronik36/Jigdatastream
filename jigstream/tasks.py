"""The Training page: teach the local AI any task, not just reading emails.

A task is a name, instructions, and examples of the right answer (input -> output), typed in,
saved from a test run, or imported from a CSV or JSONL file. Running a task shows the model its
instructions plus a few examples, so new examples help straight away. A task with at least
10 examples can also be fine-tuned on this Mac (see finetune.py); the trained adapter then
answers that task. The built-in email reader is listed too: its examples come from the
Inbox page and order corrections, and a trained email reader can be used for syncing.
"""
import csv
import io
import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Response, flash, jsonify, redirect, render_template, request, url_for

from . import db, engines, finetune, training, updater
from .config import Config, save_env
from .llm import LLMClient, LLMError
from .orderstore import Store

logger = logging.getLogger("jigstream.tasks")

EMAIL = "email"
PROMPT_EXAMPLES = 6  # most examples shown before each input
PROMPT_CHARS = 8000  # and at most this much example text; local models have limited room
MAX_ROWS = 1000  # per "run over a file"

run_state = {"running": False, "task": None, "done": 0, "total": 0, "errors": 0, "csv": None, "name": "",
             "finished_at": None}


def now():
    return datetime.now(timezone.utc).isoformat()


def task_key(task_id):
    return f"task-{task_id}"


# --- storage ---------------------------------------------------------------------------------

def tasks(c):
    rows = c.execute("SELECT t.*, (SELECT COUNT(*) FROM ai_task_examples e WHERE e.task_id = t.id) AS n"
                     " FROM ai_tasks t ORDER BY t.id").fetchall()
    return [dict(r) for r in rows]


def task(c, task_id):
    r = c.execute("SELECT * FROM ai_tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(r) if r else None


def examples(c, task_id):
    return [dict(r) for r in c.execute("SELECT * FROM ai_task_examples WHERE task_id = ? ORDER BY id DESC", (task_id,))]


def add_examples(c, task_id, pairs):
    c.executemany("INSERT INTO ai_task_examples (task_id, input, output, created_at) VALUES (?,?,?,?)",
                  [(task_id, i, o, now()) for i, o in pairs])
    c.commit()


def adapters(c, key):
    return [dict(r) for r in c.execute("SELECT * FROM ai_adapters WHERE task_key = ? ORDER BY id DESC", (key,))]


def adapter(c, adapter_id):
    r = c.execute("SELECT * FROM ai_adapters WHERE id = ?", (adapter_id,)).fetchone() if adapter_id else None
    return dict(r) if r else None


def save_adapter(c, key, result):
    cur = c.execute("INSERT INTO ai_adapters (task_key, base_model, path, examples, iters, loss, created_at)"
                    " VALUES (?,?,?,?,?,?,?)", (key, result["base"], result["path"], result["examples"],
                                                result["iters"], result["loss"], now()))
    if key.startswith("task-"):  # a new adapter is used for its task straight away
        c.execute("UPDATE ai_tasks SET adapter_id = ? WHERE id = ?", (cur.lastrowid, int(key[5:])))
    c.commit()
    return cur.lastrowid


# --- answers ---------------------------------------------------------------------------------

def check_output(t, output):
    """The output as it should be stored, or ValueError when a JSON task gets something that isn't JSON."""
    output = output.strip()
    if t["answer"] == "json":
        try:
            return json.dumps(json.loads(output), ensure_ascii=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"This task answers in JSON, and that isn't valid JSON ({e.msg}).") from None
    return output


def system_prompt(t):
    tail = (" Reply with JSON only." if t["answer"] == "json"
            else " Reply with the answer only, no explanation.")
    return (t["instructions"] or f"Do this task: {t['name']}.").strip() + tail


def prompt_examples(c, t, trained=False):
    """Recent examples that fit the prompt (fewer for a trained task: it already knows them)."""
    picked, used = [], 0
    for ex in examples(c, t["id"])[: 2 if trained else PROMPT_EXAMPLES]:
        size = len(ex["input"]) + len(ex["output"])
        if used + size > PROMPT_CHARS:
            continue
        picked.append((ex["input"], ex["output"]))
        used += size
    return list(reversed(picked))


def chat_messages(t, pairs):
    """Examples in the chat format mlx-lm and most fine-tuning tools take."""
    sys_msg = system_prompt(t)
    return [{"messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": i},
                          {"role": "assistant", "content": o}]} for i, o in pairs]


def client_for(c, t, config, models_dir):
    """(client, trained). A trained task runs on its adapter in the MLX engine; others on the Setup model."""
    a = adapter(c, t.get("adapter_id"))
    if a and engines.mlx_available():
        if config.llm_engine != "mlx":
            engines.unload("ollama", config.llm_model)  # a 16 GB Mac can't hold both comfortably
        engines.start_mlx(models_dir)
        return LLMClient(engines.base_url("mlx"), a["base_model"], "local", config.llm_timeout, adapter=a["path"]), True
    return LLMClient(config.llm_base_url, config.llm_model, config.llm_api_key, config.llm_timeout), False


def answer(llm, c, t, text, trained=False):
    pairs = prompt_examples(c, t, trained)
    if t["answer"] == "json":
        return json.dumps(llm.chat_json(system_prompt(t), text, examples=pairs), ensure_ascii=False)
    return llm.chat_text(system_prompt(t), text, examples=pairs)


def done_with_mlx(config, trained):
    """Stop the MLX engine again when it was only started for a trained task."""
    if trained and config.llm_engine != "mlx":
        engines.stop_mlx()


# --- files -----------------------------------------------------------------------------------

IN_NAMES = ("input", "prompt", "question", "text", "in")
OUT_NAMES = ("output", "completion", "answer", "response", "label", "out")


def _pick(row, names):
    lower = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    return next((lower[n] for n in names if lower.get(n) not in (None, "")), None)


def parse_examples(filename, text):
    """(input, output) pairs from a CSV (input/output columns, or the first two) or JSONL file
    ({"input", "output"}, {"prompt", "completion"} or chat {"messages": [...]})."""
    pairs = []
    if filename.lower().endswith((".jsonl", ".json", ".ndjson")):
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"Line {n} isn't valid JSON.") from None
            if isinstance(row.get("messages"), list):
                users = [m.get("content", "") for m in row["messages"] if m.get("role") == "user"]
                bots = [m.get("content", "") for m in row["messages"] if m.get("role") == "assistant"]
                i, o = (users[-1] if users else None), (bots[-1] if bots else None)
            else:
                i, o = _pick(row, IN_NAMES), _pick(row, OUT_NAMES)
            if i is None or o is None:
                raise ValueError(f"Line {n} needs an input and an output.")
            pairs.append((str(i) if not isinstance(i, str) else i,
                          o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)))
        return pairs
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    i_col = next((head.index(n) for n in IN_NAMES if n in head), None)
    o_col = next((head.index(n) for n in OUT_NAMES if n in head), None)
    body = rows[1:] if i_col is not None or o_col is not None else rows
    i_col = 0 if i_col is None else i_col
    o_col = (1 if i_col == 0 else 0) if o_col is None else o_col
    for row in body:
        if len(row) > max(i_col, o_col) and row[i_col].strip():
            pairs.append((row[i_col], row[o_col]))
    return pairs


def parse_inputs(filename, text):
    """Inputs for a run: a CSV's input column (or first column), or one per line of a text file."""
    if not filename.lower().endswith(".csv"):
        return [line for line in text.splitlines() if line.strip()]
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    col = next((head.index(n) for n in IN_NAMES if n in head), None)
    return [r[col or 0] for r in (rows[1:] if col is not None else rows) if len(r) > (col or 0) and r[col or 0].strip()]


def run_file(c, t, inputs, config, models_dir):
    """Answer every input in the background; the results become a CSV to download."""
    run_state.update(running=True, task=t["id"], done=0, total=len(inputs), errors=0, csv=None,
                     name=f"{t['name']} results.csv")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["input", "output"])
    trained = False
    try:
        llm, trained = client_for(c, t, config, models_dir)
        for text in inputs:
            try:
                w.writerow([text, answer(llm, c, t, text, trained)])
            except (LLMError, ValueError) as e:
                run_state["errors"] += 1
                w.writerow([text, f"(no answer: {e})"])
            run_state["done"] += 1
        logger.info("task %s: answered %d inputs (%d failed)", t["name"], len(inputs), run_state["errors"])
    except Exception as e:
        logger.exception("task %s: run failed", t["name"])
        w.writerow(["", f"(stopped: {e})"])
    finally:
        done_with_mlx(config, trained)
        run_state.update(running=False, csv=out.getvalue(), finished_at=time.strftime("%Y-%m-%d %H:%M"))


def remove_adapter_files(path):
    """Delete an adapter folder, but only one inside models/adapters."""
    root = (Path("models") / "adapters").resolve()
    p = Path(path).resolve()
    if root in p.parents and p.exists():
        shutil.rmtree(p, ignore_errors=True)


# --- pages -----------------------------------------------------------------------------------

def register(app, db_path=None):
    def con():
        return db.connect(db_path)

    def models_dir():
        return Path("models").resolve()

    def current():
        config = Config.from_env()
        return (config.llm_engine, config.llm_model) if app.config.get("AI_STATE") else None

    def busy():
        """Why training or a run can't start right now, or None."""
        if finetune.state["running"]:
            return "A model is training. Wait for it to finish (or stop it) first."
        if run_state["running"]:
            return "A task is running over a file. Wait for it to finish first."
        if engines.compare_state["running"]:
            return "Models are being compared on Setup. Wait for that to finish first."
        return None

    def email_messages(store):
        return [json.loads(line) for line in training.export_jsonl(store).splitlines() if line.strip()]

    # Syncing reads every email with the model, which can't share memory with training.
    email_sync = app.start_sync

    def start_sync(full=False):
        return False if finetune.state["running"] else email_sync(full)

    app.start_sync = start_sync

    def train_context(key):
        return {"trainable": finetune.base_choices(), "mlx_ok": engines.mlx_available(), "ft": finetune.state,
                "ft_here": finetune.state["task"] == key, "min_examples": finetune.MIN_EXAMPLES,
                "adapters": adapters(con(), key), "key": key}

    @app.get("/training")
    def training_page():
        c = con()
        store = Store(c)
        trained = {a["task_key"] for a in c.execute("SELECT task_key FROM ai_adapters")}
        return render_template("training_hub.html", tasks=tasks(c), email_n=len(store.examples()), trained=trained,
                               config=Config.from_env(), ft=finetune.state)

    @app.post("/training/tasks")
    def task_create():
        name = request.form.get("name", "").strip()
        if not name:
            flash("Give the task a name.")
            return redirect(url_for("training_page"))
        c = con()
        cur = c.execute("INSERT INTO ai_tasks (name, instructions, answer, created_at) VALUES (?,?,?,?)",
                        (name[:120], request.form.get("instructions", "").strip(),
                         "json" if request.form.get("answer") == "json" else "text", now()))
        c.commit()
        logger.info("training: created task %s", name)
        return redirect(url_for("task_page", task_id=cur.lastrowid))

    def task_or_404(c, task_id):
        t = task(c, task_id)
        if not t:
            flash("That task no longer exists.")
        return t

    @app.get("/training/tasks/<int:task_id>")
    def task_page(task_id, tried=None, tried_input="", tried_error=None):
        c = con()
        t = task_or_404(c, task_id)
        if not t:
            return redirect(url_for("training_page"))
        return render_template("training_task.html", t=t, examples=examples(c, task_id), tried=tried,
                               tried_input=tried_input, tried_error=tried_error, run=run_state,
                               active=adapter(c, t["adapter_id"]), in_prompt=len(prompt_examples(c, t)),
                               **train_context(task_key(task_id)))

    @app.post("/training/tasks/<int:task_id>/edit")
    def task_edit(task_id):
        c = con()
        c.execute("UPDATE ai_tasks SET name = ?, instructions = ?, answer = ? WHERE id = ?",
                  (request.form.get("name", "").strip()[:120] or "Untitled task",
                   request.form.get("instructions", "").strip(),
                   "json" if request.form.get("answer") == "json" else "text", task_id))
        c.commit()
        flash("Saved.")
        return redirect(url_for("task_page", task_id=task_id))

    @app.post("/training/tasks/<int:task_id>/delete")
    def task_delete(task_id):
        c = con()
        for a in adapters(c, task_key(task_id)):
            remove_adapter_files(a["path"])
        c.execute("DELETE FROM ai_adapters WHERE task_key = ?", (task_key(task_id),))
        c.execute("DELETE FROM ai_task_examples WHERE task_id = ?", (task_id,))
        c.execute("DELETE FROM ai_tasks WHERE id = ?", (task_id,))
        c.commit()
        flash("Task deleted.")
        return redirect(url_for("training_page"))

    @app.post("/training/tasks/<int:task_id>/try")
    def task_try(task_id):
        c = con()
        t = task_or_404(c, task_id)
        if not t:
            return redirect(url_for("training_page"))
        text = request.form.get("input", "")
        if not text.strip():
            flash("Type or paste something for the task to work on.")
            return redirect(url_for("task_page", task_id=task_id))
        if finetune.state["running"]:
            return task_page(task_id, tried_input=text, tried_error=busy())
        config = Config.from_env()
        trained = False
        try:
            llm, trained = client_for(c, t, config, models_dir())
            out = answer(llm, c, t, text, trained)
            logger.info("task %s: tried once%s", t["name"], " (trained)" if trained else "")
            return task_page(task_id, tried=out, tried_input=text)
        except Exception as e:
            logger.warning("task %s: try failed: %s", t["name"], e)
            return task_page(task_id, tried_input=text, tried_error=str(e))
        finally:
            done_with_mlx(config, trained)

    @app.post("/training/tasks/<int:task_id>/examples")
    def example_add(task_id):
        c = con()
        t = task_or_404(c, task_id)
        if not t:
            return redirect(url_for("training_page"))
        text, output = request.form.get("input", ""), request.form.get("output", "")
        if not text.strip() or not output.strip():
            flash("An example needs both an input and the right answer.")
            return redirect(url_for("task_page", task_id=task_id) + "#examples")
        try:
            add_examples(c, task_id, [(text, check_output(t, output))])
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("task_page", task_id=task_id) + "#examples")
        flash("Example saved. It's shown to the model from the next run.")
        return redirect(url_for("task_page", task_id=task_id) + "#examples")

    @app.post("/training/tasks/<int:task_id>/examples/<int:example_id>/delete")
    def example_delete(task_id, example_id):
        c = con()
        c.execute("DELETE FROM ai_task_examples WHERE id = ? AND task_id = ?", (example_id, task_id))
        c.commit()
        return redirect(url_for("task_page", task_id=task_id) + "#examples")

    @app.post("/training/tasks/<int:task_id>/import")
    def example_import(task_id):
        c = con()
        t = task_or_404(c, task_id)
        f = request.files.get("file")
        if not t or not f or not f.filename:
            flash("Choose a CSV or JSONL file to import.")
            return redirect(url_for("task_page", task_id=task_id) + "#examples")
        try:
            pairs = parse_examples(f.filename, f.read().decode("utf-8-sig", "replace"))
            good, bad = [], 0
            for i, o in pairs:
                try:
                    good.append((i, check_output(t, o)))
                except ValueError:
                    bad += 1
            add_examples(c, task_id, good)
            flash(f"Imported {len(good)} example(s)" + (f"; skipped {bad} whose answer isn't valid JSON." if bad else "."))
            logger.info("task %s: imported %d examples from %s", t["name"], len(good), f.filename)
        except ValueError as e:
            flash(f"Couldn't import that file: {e}")
        return redirect(url_for("task_page", task_id=task_id) + "#examples")

    @app.get("/training/tasks/<int:task_id>/export")
    def example_export(task_id):
        c = con()
        t = task(c, task_id)
        if not t:
            return redirect(url_for("training_page"))
        rows = chat_messages(t, [(e["input"], e["output"]) for e in reversed(examples(c, task_id))])
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        name = "".join(ch if ch.isalnum() else "-" for ch in t["name"].lower()).strip("-") or "task"
        return Response(body, mimetype="application/x-ndjson",
                        headers={"Content-Disposition": f"attachment; filename={name}-examples.jsonl"})

    @app.post("/training/tasks/<int:task_id>/run")
    def task_run(task_id):
        c = con()
        t = task_or_404(c, task_id)
        f = request.files.get("file")
        if not t or not f or not f.filename:
            flash("Choose a CSV or text file with one input per row.")
            return redirect(url_for("task_page", task_id=task_id) + "#run")
        if busy():
            flash(busy())
            return redirect(url_for("task_page", task_id=task_id) + "#run")
        inputs = parse_inputs(f.filename, f.read().decode("utf-8-sig", "replace"))[:MAX_ROWS]
        if not inputs:
            flash("That file has no inputs in it.")
            return redirect(url_for("task_page", task_id=task_id) + "#run")
        run_state["running"] = True
        threading.Thread(target=run_file, args=(con(), t, inputs, Config.from_env(), models_dir()), daemon=True).start()
        flash(f"Answering {len(inputs)} input(s). The results download as a CSV when it's done.")
        return redirect(url_for("task_page", task_id=task_id) + "#run")

    @app.get("/training/run/download")
    def run_download():
        if not run_state["csv"]:
            return redirect(request.referrer or url_for("training_page"))
        return Response(run_state["csv"], mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=\"{run_state['name']}\""})

    @app.post("/training/tasks/<int:task_id>/adapter")
    def task_use_adapter(task_id):
        c = con()
        chosen = request.form.get("adapter_id", type=int)
        if chosen and not (adapter(c, chosen) or {}).get("task_key") == task_key(task_id):
            chosen = None
        c.execute("UPDATE ai_tasks SET adapter_id = ? WHERE id = ?", (chosen, task_id))
        c.commit()
        flash("This task now uses its trained model." if chosen else
              "This task now uses the model from Setup, guided by its examples.")
        return redirect(url_for("task_page", task_id=task_id) + "#train")

    # --- fine-tuning (custom tasks and the email reader) ---

    @app.post("/training/train/<key>")
    def train_start(key):
        c = con()
        back = url_for("email_training") if key == EMAIL else None
        if key == EMAIL:
            label, rows = "the email reader", email_messages(Store(c))
        elif key.startswith("task-") and key[5:].isdigit() and task(c, int(key[5:])):
            t = task(c, int(key[5:]))
            back = url_for("task_page", task_id=t["id"])
            label = t["name"]
            rows = chat_messages(t, [(e["input"], e["output"]) for e in examples(c, t["id"])])
        else:
            return redirect(url_for("training_page"))
        base = request.form.get("base", "")
        if not engines.mlx_available():
            flash("Training needs Apple's MLX engine, which runs on Apple Silicon Macs only.")
        elif base not in finetune.base_choices():
            flash("Pick a model to train.")
        elif busy():
            flash(busy())
        elif len(rows) < finetune.MIN_EXAMPLES:
            flash(f"Add at least {finetune.MIN_EXAMPLES} examples first ({len(rows)} so far).")
        else:
            iters = request.form.get("iters", type=int)
            iters = max(20, min(2000, iters)) if iters else None
            finetune.start(key, label, rows, base, models_dir(), iters=iters, current=current(),
                           on_done=lambda result: save_adapter(con(), key, result))
            flash(f"Training {label}. It takes a few minutes to an hour depending on the examples; "
                  "syncing pauses until it's done. You can keep using the rest of BotTrack.")
        return redirect(back + "#train")

    @app.post("/training/train/stop")
    def train_stop():
        finetune.cancel()
        flash("Stopping training.")
        return redirect(request.referrer or url_for("training_page"))

    @app.get("/api/training")
    def train_status():
        return jsonify({**finetune.state, "run": {k: v for k, v in run_state.items() if k != "csv"}})

    @app.post("/training/adapters/<int:adapter_id>/delete")
    def adapter_delete(adapter_id):
        c = con()
        a = adapter(c, adapter_id)
        back = request.referrer or url_for("training_page")
        if not a:
            return redirect(back)
        if Config.from_env().llm_adapter == a["path"]:
            flash("Email syncing uses this trained model. Pick another model on Setup first, then delete it.")
            return redirect(back)
        remove_adapter_files(a["path"])
        c.execute("UPDATE ai_tasks SET adapter_id = NULL WHERE adapter_id = ?", (adapter_id,))
        c.execute("DELETE FROM ai_adapters WHERE id = ?", (adapter_id,))
        c.commit()
        flash("Trained model deleted.")
        return redirect(back)

    @app.get("/training/email")
    def email_training():
        c = con()
        store = Store(c)
        config = Config.from_env()
        return render_template("training.html", examples=store.examples(only_corrected=False), config=config,
                               in_prompt=len(training.few_shot(store)), email_n=len(store.examples()),
                               **train_context(EMAIL))

    @app.post("/training/email/use")
    def email_use_adapter():
        a = adapter(con(), request.form.get("adapter_id", type=int))
        if not a or a["task_key"] != EMAIL:
            return redirect(url_for("email_training"))
        save_env({"LLM_ENGINE": "mlx", "LLM_MODEL": a["base_model"], "LLM_ADAPTER": a["path"]})
        logger.info("email reading switched to trained adapter %s on %s", a["path"], a["base_model"])
        if app.config.get("AI_STATE"):  # the Mac app restarts its engine with the new model
            updater.restart_soon()
            return render_template("restarting.html", what="Switched to your trained email reader")
        flash("New emails are read by your trained email reader.")
        return redirect(url_for("email_training") + "#train")
