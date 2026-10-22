import io
import json
import os
from pathlib import Path

import jigstream.app as appmod
from jigstream import db, engines, finetune, tasks
from jigstream.llm import LLMClient


class FakeLLM:
    """Echoes the input upper-cased, and records what it was shown."""
    seen = []

    def __init__(self, *a, **k):
        self.adapter = k.get("adapter")

    def chat_text(self, system, user, examples=()):
        FakeLLM.seen.append((system, user, list(examples), self.adapter))
        return user.upper()

    def chat_json(self, system, user, examples=()):
        FakeLLM.seen.append((system, user, list(examples), self.adapter))
        return {"text": user}


def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "GMAIL"))})
    monkeypatch.setattr(tasks, "LLMClient", FakeLLM)
    FakeLLM.seen = []
    path = str(tmp_path / "t.db")
    return appmod.create_app(path).test_client(), path


def new_task(c, answer="text"):
    r = c.post("/training/tasks", data={"name": "Shout", "instructions": "Repeat it loudly.", "answer": answer})
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def test_training_is_always_in_the_nav_and_lists_the_email_reader(tmp_path, monkeypatch):
    c, _ = client(tmp_path, monkeypatch)
    page = c.get("/training").get_data(as_text=True)
    assert "Email reader" in page and "New task" in page
    assert 'href="/training"' in c.get("/").get_data(as_text=True)
    assert c.get("/training/email").status_code == 200


def test_create_try_and_save_examples(tmp_path, monkeypatch):
    c, path = client(tmp_path, monkeypatch)
    tid = new_task(c)
    page = c.post(f"/training/tasks/{tid}/try", data={"input": "hello"}).get_data(as_text=True)
    assert "HELLO" in page and "Save as example" in page
    c.post(f"/training/tasks/{tid}/examples", data={"input": "hi", "output": "HI!"})
    c.post(f"/training/tasks/{tid}/try", data={"input": "yo"})
    system, user, shown, adapter = FakeLLM.seen[-1]
    assert system.startswith("Repeat it loudly.") and user == "yo" and shown == [("hi", "HI!")] and adapter is None
    assert "HI!" in c.get(f"/training/tasks/{tid}").get_data(as_text=True)


def test_json_tasks_reject_answers_that_arent_json(tmp_path, monkeypatch):
    c, path = client(tmp_path, monkeypatch)
    tid = new_task(c, "json")
    c.post(f"/training/tasks/{tid}/examples", data={"input": "a", "output": "not json"})
    c.post(f"/training/tasks/{tid}/examples", data={"input": "b", "output": '{"site": "Nike"}'})
    rows = tasks.examples(db.connect(path), tid)
    assert [r["output"] for r in rows] == ['{"site": "Nike"}']
    page = c.post(f"/training/tasks/{tid}/try", data={"input": "x"}).get_data(as_text=True)
    assert "Reply with JSON only." in FakeLLM.seen[-1][0] and "&#34;text&#34;" in page


def test_parse_examples_from_csv_and_jsonl():
    assert tasks.parse_examples("a.csv", "input,output\nhi,HI\n,skip\nyo,YO\n") == [("hi", "HI"), ("yo", "YO")]
    assert tasks.parse_examples("a.csv", "Question,Answer\nq,a\n") == [("q", "a")]
    assert tasks.parse_examples("a.csv", "hi,HI\n") == [("hi", "HI")]
    jl = "\n".join([json.dumps({"input": "a", "output": "b"}), json.dumps({"prompt": "c", "completion": {"x": 1}}),
                    json.dumps({"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                                             {"role": "assistant", "content": "v"}]})])
    assert tasks.parse_examples("a.jsonl", jl) == [("a", "b"), ("c", '{"x": 1}'), ("u", "v")]
    assert tasks.parse_inputs("in.csv", "input,other\none,1\ntwo,2\n") == ["one", "two"]
    assert tasks.parse_inputs("in.txt", "one\n\ntwo\n") == ["one", "two"]


def test_import_export_and_run_over_a_file(tmp_path, monkeypatch):
    c, path = client(tmp_path, monkeypatch)
    tid = new_task(c)
    c.post(f"/training/tasks/{tid}/import", data={"file": (io.BytesIO(b"input,output\nhi,HI\nyo,YO\n"), "ex.csv")},
           content_type="multipart/form-data")
    assert len(tasks.examples(db.connect(path), tid)) == 2
    lines = c.get(f"/training/tasks/{tid}/export").get_data(as_text=True).splitlines()
    first = json.loads(lines[0])["messages"]
    assert [m["role"] for m in first] == ["system", "user", "assistant"] and first[1]["content"] == "hi"

    monkeypatch.setattr(tasks.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    c.post(f"/training/tasks/{tid}/run", data={"file": (io.BytesIO(b"one\ntwo\n"), "in.txt")},
           content_type="multipart/form-data")
    out = c.get("/training/run/download").get_data(as_text=True)
    assert out.splitlines() == ["input,output", "one,ONE", "two,TWO"]


def test_trained_task_runs_on_its_adapter(tmp_path, monkeypatch):
    c, path = client(tmp_path, monkeypatch)
    tid = new_task(c)
    started = []
    monkeypatch.setattr(engines, "mlx_available", lambda: True)
    monkeypatch.setattr(engines, "start_mlx", lambda d: started.append("start"))
    monkeypatch.setattr(engines, "stop_mlx", lambda: started.append("stop"))
    monkeypatch.setattr(engines, "unload", lambda e, m: started.append(f"unload {m}"))
    aid = tasks.save_adapter(db.connect(path), tasks.task_key(tid),
                             {"base": "x/Qwen3-4B-abliterated-4bit", "path": "models/adapters/task-1-a", "examples": 12,
                              "iters": 60, "loss": 0.5})
    assert tasks.task(db.connect(path), tid)["adapter_id"] == aid
    c.post(f"/training/tasks/{tid}/try", data={"input": "hey"})
    assert FakeLLM.seen[-1][3] == "models/adapters/task-1-a"
    assert started == ["unload huihui_ai/qwen3-abliterated:14b", "start", "stop"]  # Ollama app: MLX only while needed
    c.post(f"/training/tasks/{tid}/adapter", data={})
    c.post(f"/training/tasks/{tid}/try", data={"input": "hey"})
    assert FakeLLM.seen[-1][3] is None


def test_train_needs_enough_examples_and_a_model(tmp_path, monkeypatch):
    c, path = client(tmp_path, monkeypatch)
    tid = new_task(c)
    calls = []
    monkeypatch.setattr(engines, "mlx_available", lambda: True)
    monkeypatch.setattr(finetune, "base_choices", lambda: ["x/Qwen3-4B-abliterated-4bit"])
    monkeypatch.setattr(finetune, "start", lambda *a, **k: calls.append((a, k)))
    c.post(f"/training/train/task-{tid}", data={"base": "x/Qwen3-4B-abliterated-4bit"})
    assert not calls
    tasks.add_examples(db.connect(path), tid, [(f"in {i}", f"OUT {i}") for i in range(10)])
    c.post(f"/training/train/task-{tid}", data={"base": "someone/else"})
    assert not calls
    c.post(f"/training/train/task-{tid}", data={"base": "x/Qwen3-4B-abliterated-4bit", "iters": "5"})
    (key, label, rows, base, _), kw = calls[0]
    assert key == f"task-{tid}" and label == "Shout" and len(rows) == 10 and kw["iters"] == 20
    assert rows[0]["messages"][0]["content"].startswith("Repeat it loudly.")


def test_finetune_runs_mlx_lora_and_records_the_adapter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(finetune.logs, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(engines, "mlx_download", lambda *a: None)
    monkeypatch.setattr(engines, "stop_mlx", lambda: None)
    unloaded, ran = [], []
    monkeypatch.setattr(engines, "unload", lambda e, m: unloaded.append(m))

    class Proc:
        returncode = 0

        def __init__(self, cmd, **kw):
            ran.append(cmd)
            out = Path(cmd[cmd.index("--adapter-path") + 1])
            (out / "adapters.safetensors").write_text("w")
            self.stdout = iter(["Iter 5: Train loss 2.100, It/sec 1.0\n", "    10   0.812 ↓     300    1200\n"])

        def wait(self):
            pass

        def poll(self):
            return 0
    monkeypatch.setattr(finetune.subprocess, "Popen", Proc)
    rows = [{"messages": [{"role": "user", "content": str(i)}, {"role": "assistant", "content": "x"}]} for i in range(20)]
    done = []
    finetune.train("task-3", "Shout", rows, "x/Qwen3-4B-abliterated-4bit", tmp_path / "models", iters=10,
                   current=("ollama", "big:14b"), on_done=done.append)
    assert finetune.state["error"] is None and not finetune.state["running"]
    assert unloaded == ["big:14b"] and "--mask-prompt" in ran[0] and ran[0][3] == "lora"
    assert done[0]["loss"] == 0.812 and done[0]["examples"] == 20 and done[0]["path"].startswith("models/adapters/task-3-")
    data = Path(done[0]["path"]) / "data"
    assert len((data / "train.jsonl").read_text().splitlines()) == 18 and len((data / "valid.jsonl").read_text().splitlines()) == 2


def test_finetune_refuses_small_sets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    finetune.train("task-1", "Shout", [{"messages": []}] * 3, "x/m", tmp_path)
    assert "at least 10" in finetune.state["error"]


def test_email_reader_can_switch_to_its_trained_adapter(tmp_path, monkeypatch):
    c, path = client(tmp_path, monkeypatch)
    aid = tasks.save_adapter(db.connect(path), "email", {"base": "x/Qwen3-8B-abliterated-4bit",
                                                         "path": "models/adapters/email-1", "examples": 30,
                                                         "iters": 90, "loss": 0.4})
    c.post("/training/email/use", data={"adapter_id": aid})
    assert os.environ["LLM_ENGINE"] == "mlx" and os.environ["LLM_ADAPTER"] == "models/adapters/email-1"
    from jigstream.config import Config
    from jigstream.llm import from_config
    assert from_config(Config.from_env()).adapter == "models/adapters/email-1"
    c.post(f"/training/adapters/{aid}/delete")  # in use: kept
    assert tasks.adapter(db.connect(path), aid)
    c.post("/setup/engine", data={"engine": "ollama", "model": "huihui_ai/qwen3-abliterated:14b"})
    assert from_config(Config.from_env()).adapter is None


def test_sync_waits_while_training(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = appmod.create_app(str(tmp_path / "s.db"))
    monkeypatch.setitem(finetune.state, "running", True)
    assert app.start_sync() is False


def test_client_sends_adapter_and_reads_text(monkeypatch):
    sent = {}

    def fake(req, timeout):
        sent.update(json.loads(req.data))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": "<think>hm</think> Nike"}}]}).encode())
    monkeypatch.setattr("urllib.request.urlopen", fake)
    c = LLMClient("http://x/v1", "m", "k", 5, adapter="models/adapters/a")
    assert c.chat_text("sys", "hi") == "Nike"
    assert sent["adapters"] == "models/adapters/a" and "response_format" not in sent
