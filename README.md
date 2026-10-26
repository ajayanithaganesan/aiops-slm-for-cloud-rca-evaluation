# AIOpsLab SLM for Cloud RCA Evaluation

Research project for evaluating a locally hosted small language model on cloud incident root-cause localization using the AIOpsLab testbed.

## Project components

- `agent/` — local Ollama agent and evaluation runner.
- `parser/` — universal telemetry evidence parser.
- `docs/` — experiment methodology and reproducibility notes.
- `reports/` — selected evaluation outputs and result tables.

## Evaluation conditions

- **Baseline:** the model uses AIOpsLab's native telemetry actions without parser-generated evidence.
- **Graph-augmented:** fresh traces are processed into structured, graph-aware evidence before being supplied to the model.

The evidence layer does not select a root cause or use incident ground truth. AIOpsLab performs the final localization evaluation.

## Metrics

Experiments record localization accuracy, success status, subset status, steps, TTL, elapsed time, input/output tokens, model calls, tools used, submissions, and run artifact paths.

## Reproducibility

The agent expects AIOpsLab and Ollama to be available in the execution environment. Model endpoint and model name are configured through environment variables:

```text
OLLAMA_BASE_URL
OLLAMA_MODEL
```

See the experiment documentation in `docs/` for the complete procedure.
