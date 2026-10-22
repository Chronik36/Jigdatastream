"""Minimal client for an OpenAI-compatible chat endpoint (Ollama, LM Studio, llama.cpp)."""

import json
import logging
import re
import urllib.error
import urllib.request


logger = logging.getLogger("jigstream.llm")


class LLMError(RuntimeError):
    pass


def parse_json(text):
    """Pull a JSON object out of a model reply, tolerating code fences or chatter."""
    # Reasoning models (qwen3, deepseek-r1) may prepend their thinking.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class LLMClient:
    def __init__(self, base_url, model, api_key="ollama", timeout=180):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def chat_json(self, system, user):
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.URLError as e:
            logger.error("cannot reach model server at %s: %s", self.base_url, e)
            raise LLMError(
                f"Could not reach the local model at {self.base_url} ({e}). Is Ollama running?"
            ) from e
        try:
            content = body["choices"][0]["message"]["content"]
            return parse_json(content)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.warning("model %s returned unusable output: %s", self.model, str(body)[:2000])
            raise LLMError(f"Model did not return JSON: {str(body)[:300]}") from e


def installed_models(base_url, timeout=5):
    """Model names the local server has, or None if it isn't running."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/models", timeout=timeout) as resp:
            return [m.get("id") for m in json.load(resp).get("data", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return None
