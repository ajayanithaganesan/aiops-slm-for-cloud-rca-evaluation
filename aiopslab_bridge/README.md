# Minimal AIOpsLab bridge

This isolated bridge runs one localization experiment at a time and exposes
only read-only telemetry actions to an external controller.

Start it from the repository root:

```bash
poetry run python -m aiopslab_bridge.bridge
```

Endpoints:

```text
GET  /health
POST /start   {"problem_id": "..."}
POST /action  {"action": "get_logs", "args": ["namespace", "service"]}
POST /action  {"action": "summarize_traces", "args": ["/path/to/traces.csv"]}
POST /submit  {"solution": ["geo"]}
POST /stop
```

Run artifacts are written under `research/bridge_runs/<run_id>/`.
