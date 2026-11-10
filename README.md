# AIOpsLab SLM for Cloud RCA Evaluation

Research project evaluating local Small Language Models (SLMs) on cloud incident root-cause localization (RCA) using Microsoft's AIOpsLab testbed.

## Architecture & Project Structure

- iopslab_local_agent/ — Universal SLM agent package (SLMAgent supporting Nemotron, Qwen, and Phi model families via Ollama), declarative telemetry collector (SystemEvidenceCollector), action normalizer, and single-case evaluation runner (un_case.py).
- iopslab_bridge/ — Lightweight HTTP REST bridge for orchestrator interaction and environment management.
- esearch/trace_parser/ — Universal causal graph parser (	race_parser_universal_v7.py) extracting call trees and compacting raw trace spans under token-budget limits.
- docs/ — Methodological documentation, architectural evolution registers, and batch evaluation procedures (docs/tracker.md).
- evaluation_results.csv — Primary empirical comparison dataset recording accuracy, TTL, step counts, token usage, and tool actions across evaluated benchmark incidents.

## Evaluation Conditions

- **Baseline:** The model receives bounded raw cluster telemetry (orchestrator pod/service states, bounded error logs) and raw CSV trace spans.
- **Graph (Proposed Method):** Fresh incident traces are transformed into structured, high-density causal dependency graphs and compact RPC failure mappings.

The evidence layer operates strictly without incident ground truth or heuristic labels. The SLM makes the final root-cause localization decision.

## Metrics & Reproducibility

Experiments record localization accuracy, exact/subset match success, Time-to-Localize (TTL), step counts, input/output token efficiency, and model invocation latency.

Ollama connection parameters are configured through command-line flags or environment variables:
`ash
python3 -m aiopslab_local_agent.run_case --problem-id <incident-id> --condition [baseline|graph] --model <model-tag> --ollama-host <url>
`

For complete empirical findings, failure registers, and benchmark tables, refer to [docs/tracker.md](docs/tracker.md).
