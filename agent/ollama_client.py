"""Small Ollama HTTP client."""

import os
import requests


class OllamaClient:
    def __init__(self, model: str | None = None, base_url: str | None = None):
        self.model = model or os.getenv("OLLAMA_MODEL", "nemotron-3-nano:4b")
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.calls = []

    def chat(self, messages: list[dict[str, str]]) -> str:
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={"model": self.model, "messages": messages, "stream": False},
            timeout=300,
        )
        response.raise_for_status()
        data = response.json()
        self.calls.append(
            {
                "model": self.model,
                "prompt_tokens": data.get("prompt_eval_count"),
                "completion_tokens": data.get("eval_count"),
                "total_duration_ns": data.get("total_duration"),
                "load_duration_ns": data.get("load_duration"),
                "prompt_eval_duration_ns": data.get("prompt_eval_duration"),
                "eval_duration_ns": data.get("eval_duration"),
            }
        )
        return data["message"]["content"]
