# Native local SLM agent

This package uses AIOpsLab's existing `Orchestrator` and telemetry actions.
It provides a generic `SLMAgent` interface supporting various local Small Language Models (Nemotron, Qwen, Phi, Llama, etc.) served via Ollama.
It does not implement a second log, trace, or metrics collector.

Configure Ollama:

```bash
# Default or custom model:
export OLLAMA_MODEL=nemotron-3-nano:4b
# Or another SLM:
# export OLLAMA_MODEL=qwen2.5-coder:7b
# export OLLAMA_MODEL=phi4-mini

export OLLAMA_BASE_URL=http://<ollama-host>:11434
```

Run one localization case from the repository root:

```bash
poetry run python -m aiopslab_local_agent.run_case \
  misconfig_app_hotel_res-localization-1 --preload-evidence
```

Results are saved under `research/local_agent_runs/`.
