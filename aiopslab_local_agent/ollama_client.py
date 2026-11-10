"""Simple client to send chat messages to a local Ollama model."""

import os
import requests


class OllamaClient:

    def __init__(self, model=None, base_url=None):
        # Use environment variable if set, otherwise default to nemotron 4b
        self.model = model or os.getenv("OLLAMA_MODEL", "nemotron-3-nano:4b")

        # Default to local Ollama port 11434
        self.base_url = (
            base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        ).rstrip("/")

        # Track statistics for each API call made during an evaluation
        self.calls = []

    def chat(self, messages):
        """Sends chat messages to Ollama's /api/chat endpoint with a 300s timeout."""
        endpoint_url = f"{self.base_url}/api/chat"
        payload = {"model": self.model, "messages": messages, "stream": False}

        # Post request to Ollama with the exact original 300s timeout
        response = requests.post(endpoint_url, json=payload, timeout=300)
        response.raise_for_status()

        data = response.json()

        # Record token usage and timing stats returned by Ollama
        self.calls.append(
            {
                "model": self.model,
                "prompt_tokens": data.get("prompt_eval_count"),
                "completion_tokens": data.get("eval_count"),
                "total_duration_ns": data.get("total_duration"),
                "load_duration_ns": data.get("load_duration"),
                "prompt_eval_duration_ns": data.get("prompt_eval_duration"),
                "eval_duration_ns": data.get("eval_duration"),
                "prompt_chars": sum(
                    len(str(item.get("content", ""))) for item in messages
                ),
                "prompt_messages": len(messages),
            }
        )

        # Return the text response from the model
        return data["message"]["content"]
