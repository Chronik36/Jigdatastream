import json

from jigstream import app as appmod, db, engines
from jigstream.mail import Email
from jigstream.orderstore import Store


def mail(i):
    return Email(f"<m{i}@shop>", "Shop <s@shop.com>", f"Order {i}", "2026-10-20T10:00:00+00:00", "body", "me@gmail.com")


def seed(store, n=6):
    for i in range(n):
        status = "cancelled" if i % 2 else "delivered"
        store.add_inbox_example(mail(i), f"email {i} {status}", {"kind": "order", "shipping_status": "ordered"},
                                {"kind": "order", "shipping_status": status}, status)


class Reader:
    """Gets the cancelled emails right and the delivered ones wrong."""
    def __init__(self, base_url, model, api_key, timeout):
        self.model = model

    def chat_json(self, system, user):
        return {"kind": "order", "shipping_status": "cancelled" if "cancelled" in user else "shipped"}


def test_compare_scores_each_model_and_picks_best(tmp_path, monkeypatch):
    store = Store(db.connect(str(tmp_path / "c.db")))
    seed(store)
    unloaded = []
    monkeypatch.setattr(engines, "LLMClient", Reader)
    monkeypatch.setattr(engines, "ollama_models", lambda: ["a:4b", "b:8b"])
    monkeypatch.setattr(engines, "unload", lambda e, m: unloaded.append(m))
    engines.compare(store, [("ollama", "a:4b"), ("ollama", "b:8b")], tmp_path, current=("ollama", "c:14b"))
    results = engines.compare_state["results"]
    assert [(r["model"], r["right"], r["total"], r["accuracy"]) for r in results] == [("a:4b", 3, 6, 50), ("b:8b", 3, 6, 50)]
    assert unloaded == ["c:14b", "a:4b", "b:8b"]
    assert engines.best(results)["model"] in ("a:4b", "b:8b") and not engines.compare_state["running"]


def test_compare_needs_labelled_emails(tmp_path):
    store = Store(db.connect(str(tmp_path / "e.db")))
    seed(store, 2)
    engines.compare(store, [("ollama", "a:4b")], tmp_path)
    assert "at least 5" in engines.compare_state["error"]


def test_same_treats_shipped_as_in_transit():
    assert engines._same({"kind": "order", "shipping_status": "shipped"}, {"kind": "order", "shipping_status": "in_transit"})
    assert engines._same({"kind": "expense"}, {"kind": "expense"})
    assert not engines._same({"kind": "other"}, {"kind": "expense"})


def test_make_default_saves_engine_and_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("LLM_ENGINE", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    client = appmod.create_app(str(tmp_path / "a.db")).test_client()
    client.post("/setup/engine", data={"engine": "mlx", "model": "someone/Qwen3-8B-abliterated-4bit"})
    env = (tmp_path / ".env").read_text()
    assert "LLM_ENGINE=mlx" in env and "LLM_MODEL=someone/Qwen3-8B-abliterated-4bit" in env
    assert client.post("/setup/engine", data={"engine": "bogus", "model": "x"}).status_code == 302
    assert json.loads(client.get("/api/compare").data)["running"] is False
