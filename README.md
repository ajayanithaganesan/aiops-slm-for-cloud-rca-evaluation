# AIOpsLab: Local SLM for Cloud Incident Root-Cause Analysis (RCA)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Platform: Kubernetes](https://img.shields.io/badge/platform-Kubernetes-326CE5.svg)](https://kubernetes.io/)
[![Evaluator: AIOpsLab](https://img.shields.io/badge/evaluator-AIOpsLab-blueviolet.svg)](https://github.com/microsoft/AIOpsLab)
[![Local Inference: Ollama](https://img.shields.io/badge/inference-Ollama-black.svg)](https://ollama.ai/)

An empirical research framework evaluating local **Small Language Models (SLMs: Nemotron-3-nano:4b, Qwen-2.5-Coder:7b, Phi-4-mini)** on automated cloud incident root-cause localization (RCA) across microservice architectures (Hotel Reservation, Social Network, Astronomy Shop).

The project investigates whether **domain-specific topological causal graph parsing** can bridge the reasoning gap of resource-constrained local models against noisy raw distributed telemetry, achieving deterministic 1-step localization without cloud-scale LLMs.

---

## 🔬 Core Research Hypotheses & Findings

1. **The "Silent Service" Telemetry Blindspot:**
   - When a microservice container is abruptly killed or fails to start, it produces **0 distributed trace spans**.
   - Trace-only diagnostic pipelines commit systematic false-positive errors (blaming the calling upstream frontend). Correlating cluster orchestrator state (kubectl get pods, kubectl get endpoints, service port configurations) with request traces is mandatory for robust RCA.

2. **Graph-Augmented Evidence vs. Raw Context Overflow:**
   - Ingesting raw CSV spans (70,000+ characters) causes extreme attention dilution ("Lost in the Middle") and prompt inflation in local SLMs, leading to inference timeouts or caller-blame hallucinations.
   - The causal graph parser (	race_parser_universal_v7.py + universal_evidence_processor.py) compacts raw spans by **~80-90%** into call trees and RPC target attribution tables, transforming multi-hop trace reasoning into a direct classification task.

3. **Level-Ground Scientific Evaluation:**
   - Baseline and Graph conditions share **identical** cluster platform telemetry and bounded logs. The single independent variable is:
     - **Baseline:** Raw Tabular CSV Trace Spans
     - **Graph (Proposed Method):** Causal Dependency Graph (slm_context_compact.txt)

---

## 📊 Benchmark Highlights & Progress

### 1. Head-to-Head Comparison: Raw Spans vs. Causal Graph
Evaluated under identical cluster conditions using 
emotron-3-nano:4b on container_kill-localization:

| Evaluation Metric | Baseline Condition (Raw CSV Spans) | Graph Condition (Causal Graph Parser) | Relative Improvement |
| :--- | :---: | :---: | :---: |
| **Prediction** | ["frontend"] (Caller Blame) | ["geo"] (True Root Cause) | **Exact Match** |
| **Accuracy** | **0.0%** | **100.0%** | **+100.0% Gain** |
| **Time to Localize (TTL)** | 23.42s | **7.56s** | **3.1x Faster Inference** |
| **Steps** | 1 | 1 | Deterministic 1-step resolution |
| **Failure Analysis** | Copied raw CSV table tokens | Disambiguated caller from callee target | **Eliminated caller hallucination** |

---

### 2. Verified Benchmark Evaluation Matrix (Baseline vs. Graph)

| Incident ID | Microservice Benchmark | Fault Injected | Condition | Model Output | Accuracy | TTL | Steps | Result Note |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| container_kill-localization | Hotel Reservation | Container Kill | Baseline | ["frontend"] | **0.0%** | 23.42s | 1 | Upstream caller blame |
| container_kill-localization | Hotel Reservation | Container Kill | **Graph** | ["geo"] | **100.0%** | **7.56s** | 1 | **Exact match, 3.1x faster** |
| k8s_target_port-misconfig-localization-1 | Social Network | K8s Port Mismatch | **Graph** | ["user-service"] | **100.0%** | **13.69s** | 1 | **Exact match via declarative manifest** |
| pod_kill_hotel_res-localization-1 | Hotel Reservation | Pod Kill | **Graph** | ["user"] | **100.0%** | **21.42s** | 1 | Exact match via orchestrator restart signals |
| misconfig_app_hotel_res-localization-1 | Hotel Reservation | App Misconfig | **Graph** | ["geo"] | **100.0%** | **16.84s** | 1 | Exact match via RPC callee attribution |

---

### 3. Empirical 17-Incident Sequential Baseline Batch Evaluation Summary

Across a 17-incident sequential baseline evaluation batch run with 
emotron-3-nano:4b:
- **Total Evaluated Incidents:** 17
- **Exact Matches (100% Accuracy):** 8 (47.1%)
- **Partial / Subset Matches (50% Accuracy):** 1 (5.9%)
- **Failures (0.0% Accuracy):** 8 (47.1%)
- **Key Baseline Failure Categories Identified:**
  - *Cascading Caller Blame:* Network loss and database auth drops caused upstream callers (rontend, eservation) to emit 5xx errors; the model penalized accuracy by submitting all callers.
  - *Schema Key Hallucination:* On unstructured app errors, 4B models copied raw CSV table headers ("verdict", "verdict_reason") or log severity strings ("warning").
  - *Silent Latency Degradation:* Network delay faults left pods in 1/1 Running state with 0 restarts; without graph call trees, the baseline model defaulted to empty submissions ([]).

---

## 🏗️ Repository Architecture

`	ext
aiops-slm-for-cloud-rca-evaluation/
├── aiopslab_local_agent/                # Core autonomous SLM agent package
│   ├── slm_agent.py                     # Universal SLMAgent (Nemotron/Qwen/Phi) with action normalizer
│   ├── system_evidence_collector.py     # Declarative cluster & platform evidence collector
│   ├── telemetry_manifest.json          # Catalog of K8s commands, error filters, and log caps
│   ├── universal_evidence_processor.py  # Graph evidence budget compactor
│   ├── run_case.py                      # Level-ground single-case evaluation runner
│   ├── ollama_client.py                 # Streaming Ollama client with token metrics
│   └── README.md
├── aiopslab_bridge/                     # REST HTTP bridge wrapping AIOpsLab orchestrator
│   ├── bridge.py
│   └── README.md
├── research/
│   └── trace_parser/
│       └── trace_parser_universal_v7.py # Universal causal graph dependency parser
├── docs/
│   └── tracker.md                       # Complete research register, findings, and batch scripts
├── evaluation_results.csv               # Primary empirical CSV dataset tracking all runs
├── .gitignore                           # Excludes heavy logs, trace outputs, and virtualenvs
└── README.md                            # Project research and progress documentation
`

---

## 🚀 Getting Started & Execution

### Prerequisites
- Python 3.10+
- Running Kubernetes cluster (Minikube / Kind / K3s) with AIOpsLab installed
- Locally running [Ollama](https://ollama.ai/) instance with target SLM loaded (e.g. 
emotron-3-nano:4b)

### Single Incident Execution

Run an incident under **Graph** mode:
`ash
python3 -m aiopslab_local_agent.run_case \
    --problem-id container_kill-localization \
    --condition graph \
    --model nemotron-3-nano:4b \
    --ollama-host http://127.0.0.1:11434 \
    --max-steps 1
`

Run an incident under **Baseline** mode:
`ash
python3 -m aiopslab_local_agent.run_case \
    --problem-id container_kill-localization \
    --condition baseline \
    --model nemotron-3-nano:4b \
    --ollama-host http://127.0.0.1:11434 \
    --max-steps 1
`

### Complete Incident Catalog (28 Incidents)
The full test suite encompasses 28 active incidents across three microservice platforms:
- **Hotel Reservation (11):** container_kill, pod_kill, misconfig_app, 
etwork_loss, 
etwork_delay, 
etwork_corrupt, 
etwork_duplicate, cpu_hog (1 & 2), mem_hog (1 & 2).
- **Social Network (6):** k8s_target_port-misconfig, scale_pod_zero, uth_miss_mongodb, wrong_bin_usage (1, 2, & 3).
- **Astronomy Shop (11):** payment_service_unreachable, kafka_queue_problems, loadgenerator_flood, raud_detection_failure, ecommendation_cache_failure, productcatalog_failure, shipping_service_slow, email_service_failure, currency_service_failure, d_service_failure, cart_service_failure.

For batch execution bash scripts and complete experimental details, see [docs/tracker.md](docs/tracker.md).
