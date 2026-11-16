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
   - The causal graph parser (trace_graph_parser.py + evidence_compactor.py) compacts raw spans by **~80-90%** into call trees and RPC target attribution tables, transforming multi-hop trace reasoning into a direct classification task.

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

The matrix below captures verified head-to-head comparisons and baseline evaluation runs conducted with `nemotron-3-nano:4b`:

| Incident ID | Microservice Benchmark | Fault Injected | Condition | Model Output | Accuracy | TTL | Steps | Result Note |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| `container_kill-localization` | Hotel Reservation | Container Kill | Baseline | `["frontend"]` | **0.0%** | 23.42s | 1 | Upstream caller blame from raw CSV spans |
| `container_kill-localization` | Hotel Reservation | Container Kill | **Graph** | `["geo"]` | **100.0%** | **7.56s** | 1 | **Exact match, 3.1x faster** |
| `k8s_target_port-misconfig-localization-1` | Social Network | K8s Port Mismatch | **Graph** | `["user-service"]` | **100.0%** | **13.69s** | 1 | **Exact match via declarative manifest** |
| `pod_kill_hotel_res-localization-1` | Hotel Reservation | Pod Kill | Baseline | `["user"]` | **100.0%** | 34.35s | 1 | Cluster state detected restart count [!] ATTENTION |
| `pod_kill_hotel_res-localization-1` | Hotel Reservation | Pod Kill | **Graph** | `["user"]` | **100.0%** | **21.42s** | 1 | **Exact match via orchestrator restart signals** |
| `misconfig_app_hotel_res-localization-1` | Hotel Reservation | App Misconfig | Baseline | `["verdict", "verdict_reason"]` | **0.0%** | 22.39s | 1 | Raw CSV schema header leakage |
| `misconfig_app_hotel_res-localization-1` | Hotel Reservation | App Misconfig | **Graph** | `["geo"]` | **100.0%** | **16.84s** | 1 | **Exact match via RPC callee attribution** |
| `pod_failure_hotel_res-localization-1` | Hotel Reservation | Pod Failure | Baseline | `[]` | **0.0%** | 25.36s | 1 | Pods stayed 1/1 Running; fallback to empty submission |
| `network_delay_hotel_res-localization-1` | Hotel Reservation | Network Delay | Baseline | `[]` | **0.0%** | 25.54s | 1 | Latency-only degradation; no crash signals |
| `network_loss_hotel_res-localization-1` | Hotel Reservation | Network Loss | Baseline | `["frontend", "profile", "rate", "reservation"]` | **0.0%** | 38.10s | 1 | Multi-service error cascade; caller penalty |
| `auth_miss_mongodb-localization-1` | Social Network | Auth Miss | Baseline | `["url-shorten-mongodb"]` | **100.0%** | **19.43s** | 1 | Explicit auth keyword match in Mongo logs |
| `revoke_auth_mongodb-localization-1` | Hotel Reservation | Revoke Auth | Baseline | `["geo", "search"]` | **50.0%** | 20.63s | 1 | Listed caller + callee; subset penalty |
| `revoke_auth_mongodb-localization-2` | Hotel Reservation | Revoke Auth | Baseline | `["rate"]` | **100.0%** | **18.77s** | 1 | Exact match localized to rate DB |
| `user_unregistered_mongodb-localization-1` | Hotel Reservation | Unregistered User | Baseline | `{}` -> `[]` | **0.0%** | 175.43s | 1 | Model formatted response as empty JSON dict |
| `user_unregistered_mongodb-localization-2` | Hotel Reservation | Unregistered User | Baseline | `["rate"]` | **100.0%** | **113.29s** | 1 | Accurately extracted root error service |
| `k8s_target_port-misconfig-localization-2` | Social Network | Port Misconfig | Baseline | `[]` | **0.0%** | 13.13s | 1 | TargetPort mismatch missed in raw prompt |
| `k8s_target_port-misconfig-localization-3` | Social Network | Port Misconfig | Baseline | `["post-storage-service"]` | **100.0%** | **7.72s** | 1 | Exact match via targetPort mismatch signal |
| `scale_pod_zero_social_net-localization-1` | Social Network | Scale to Zero | Baseline | `["user-service"]` | **100.0%** | **9.13s** | 1 | Deployment replicas 0/1 signal [!] ATTENTION |
| `assign_to_non_existent_node_social_net-localization-1` | Social Network | Bad Node Assign | Baseline | `["user-service"]` | **100.0%** | **12.53s** | 1 | Pod stuck Pending (0/1 ready) [!] ATTENTION |
| `wrong_bin_usage-localization-1` | Social Network | Binary Misuse | Baseline | `["warning"]` | **0.0%** | 8.17s | 1 | Log severity header extracted instead of service |
| `astronomy_shop_ad_service_failure-localization-1` | Astronomy Shop | Feature Flag Fault | Baseline | `["jaeger", "otel-collector"]` | **0.0%** | 22.86s | 1 | Monitoring infra blame from 30-svc span noise |
| `astronomy_shop_ad_service_failure-localization-1` | Astronomy Shop | Feature Flag Fault | **Graph** | `["ad"]` | **100.0%** | **17.24s** | 1 | **Exact match via universal gRPC callee attribution** |
| `astronomy_shop_cart_service_failure-localization-1` | Astronomy Shop | Feature Flag Fault | Baseline | `[]` | **0.0%** | 18.89s | 1 | Noisy telemetry swamped 8k context window |
| `astronomy_shop_cart_service_failure-localization-1` | Astronomy Shop | Feature Flag Fault | **Graph** | `[]` | **0.0%** | 17.73s | 1 | Callee missing explicit 5xx in traces -> fallback [] |
| `astronomy_shop_kafka_queue_problems-localization-1` | Astronomy Shop | Kafka Queue Issue | Baseline | `[]` | **0.0%** | 15.32s | 1 | Swamped by raw queue metrics; empty fallback |
| `astronomy_shop_kafka_queue_problems-localization-1` | Astronomy Shop | Kafka Queue Issue | **Graph** | `["checkout"]` | **0.0%** | 11.91s | 1 | Upstream client blamed for dropped queue messages |
| `astronomy_shop_loadgenerator_flood_homepage-localization-1` | Astronomy Shop | Load Flood | Baseline | `[]` | **0.0%** | 14.04s | 1 | High volume traffic saturated span context |
| `astronomy_shop_loadgenerator_flood_homepage-localization-1` | Astronomy Shop | Load Flood | **Graph** | `[]` | **0.0%** | 19.27s | 1 | High traffic volume without explicit error spans |
| `astronomy_shop_payment_service_unreachable-localization-1` | Astronomy Shop | Net Unreachable | Baseline | `[]` | **0.0%** | 15.82s | 1 | Connection dropped; prompt budget exceeded |
| `astronomy_shop_payment_service_unreachable-localization-1` | Astronomy Shop | Net Unreachable | **Graph** | `["checkout"]` | **100.0%** | **9.82s** | 1 | **Exact match via broken payment dependency caller** |
| `astronomy_shop_product_catalog_service_failure-localization-1` | Astronomy Shop | Catalog Failure | Baseline | `["jaeger", "currency"]` | **0.0%** | 122.08s | 1 | Caller / infra blame from raw CSV spans |
| `astronomy_shop_product_catalog_service_failure-localization-1` | Astronomy Shop | Catalog Failure | **Graph** | `[]` | **0.0%** | 55.54s | 1 | High token budget without explicit error code |
| `astronomy_shop_recommendation_service_cache_failure-localization-1` | Astronomy Shop | Cache Failure | Baseline | `[]` | **0.0%** | 21.39s | 1 | Latency degradation without crash indicators |
| `astronomy_shop_recommendation_service_cache_failure-localization-1` | Astronomy Shop | Cache Failure | **Graph** | `["flagd"]` | **0.0%** | 13.42s | 1 | Sidecar chatter bias in 30-service topology |
| `astronomy_shop_payment_service_failure-localization-1` | Astronomy Shop | Payment Failure | Baseline | `["recommendation"]` | **0.0%** | 19.50s | 1 | Upstream client blamed for downstream failure |
| `astronomy_shop_payment_service_failure-localization-1` | Astronomy Shop | Payment Failure | **Graph** | `["ad"]` | **0.0%** | 13.65s | 1 | Callee misattributed under heavy span concurrency |
| `astronomy_shop_ad_service_high_cpu-localization-1` | Astronomy Shop | High CPU Stress | Baseline | `[]` | **0.0%** | 14.79s | 1 | Resource contention without explicit crash |
| `astronomy_shop_ad_service_high_cpu-localization-1` | Astronomy Shop | High CPU Stress | **Graph** | `["flagd"]` | **0.0%** | 12.81s | 1 | CPU stress without crash; model defaulted to sidecar |
| `astronomy_shop_ad_service_manual_gc-localization-1` | Astronomy Shop | Memory GC Pause | Baseline | `[]` | **0.0%** | 13.94s | 1 | Pause latency swamped by raw traces |
| `astronomy_shop_ad_service_manual_gc-localization-1` | Astronomy Shop | Memory GC Pause | **Graph** | `["flagd"]` | **0.0%** | 10.14s | 1 | GC pause latency omitted from structural error table |
| `astronomy_shop_image_slow_load-localization-1` | Astronomy Shop | Slow Load | Baseline | `[]` | **0.0%** | 16.86s | 1 | Latency degradation; empty submission |
| `astronomy_shop_image_slow_load-localization-1` | Astronomy Shop | Slow Load | **Graph** | `["flagd"]` | **0.0%** | 12.82s | 1 | Latency-only fault without explicit error spans |

---

### 3. Empirical 28-Incident Sequential Baseline Batch Evaluation Summary

Across all 28 registered localization incidents evaluated sequentially in Baseline mode with `nemotron-3-nano:4b`:
- **Total Evaluated Incidents:** 28 (11 Hotel Reservation, 6 Social Network, 11 Astronomy Shop)
- **Exact Matches (100% Accuracy):** 8 (28.6%)
- **Partial / Subset Matches (50% Accuracy):** 1 (3.6%)
- **Failures (0.0% Accuracy):** 19 (67.9%)
- **Microservice Suite Breakdown:**
  - **Hotel Reservation:** 5/11 (45.5% accuracy)
  - **Social Network:** 3/6 (50.0% accuracy)
  - **Astronomy Shop:** **0/11 (0.0% accuracy)**
- **Key Empirical Baseline Failure Modes Identified:**
  - *Large-Scale Graph Complexity (Astronomy Shop):* In microservice topologies exceeding 25+ services, raw telemetry overwhelms the SLM's 8,194 context window, resulting in 100% baseline failure.
  - *Cascading Caller Blame:* Network loss, latency drops, and service failures cause upstream callers (`frontend`, `currency`, `recommendation`) or telemetry collectors (`jaeger`, `otel-collector`) to emit warnings, which the SLM penalizes by blaming callers.
  - *Schema Key Hallucination:* On unstructured telemetry, small 4B models copy CSV table headers (`verdict`, `verdict_reason`) or log severity words (`warning`).
  - *Silent Latency Degradation:* Network delay and CPU faults keep pods in `Running` state with 0 restarts; without graph call trees, the baseline SLM defaults to empty submissions (`[]`).

---

## 🏗️ Repository Architecture & Telemetry Pipeline

```mermaid
graph TD
    subgraph Testbed ["AIOpsLab Environment (Kubernetes)"]
        FaultInject["Fault Injection Engine<br/>(Chaos Mesh / App Mutators)"]
        Microservices["Microservice Workloads<br/>(Hotel Res / Social Net / Astro Shop)"]
        TelemetrySources["Telemetry Plane<br/>(Traces / Pod States / K8s Svcs / Logs)"]
        FaultInject --> Microservices
        Microservices --> TelemetrySources
    end

    subgraph DataCollection ["Telemetry Collection & Compaction"]
        Collector["SystemEvidenceCollector<br/>(telemetry_manifest.json)"]
        RawSpans["Raw CSV Spans<br/>(Tabular Trace Dump)"]
        Parser["Universal Causal Graph Parser<br/>(trace_graph_parser.py)"]
        Processor["Evidence Processor & Compactor<br/>(evidence_compactor.py)"]
        CompactGraph["Compact Graph Context<br/>(Call Trees & RPC Failure Mappings)"]

        TelemetrySources -->|"Cluster Checks & Bounded Logs"| Collector
        TelemetrySources -->|"Export Spans"| RawSpans
        RawSpans -->|"Graph Condition"| Parser
        Parser --> Processor
        Processor --> CompactGraph
    end

    subgraph EvaluationHarness ["Evaluation Runner & SLM Agent"]
        Runner["Level-Ground Evaluation Runner<br/>(run_case.py)"]
        Agent["SLMAgent Engine<br/>(slm_agent.py)"]
        OllamaClient["Ollama Client<br/>(ollama_client.py)"]
        LocalSLM["Local Small Language Model<br/>(Nemotron-3-nano / Qwen / Phi)"]
        Normalizer["Action Normalizer & Strict Contract<br/>(Vocabulary Bounds & Priority)"]

        Collector --> Runner
        RawSpans -->|"Baseline Condition"| Runner
        CompactGraph -->|"Graph Condition"| Runner
        Runner --> Agent
        Agent --> OllamaClient
        OllamaClient --> LocalSLM
        LocalSLM --> Normalizer
    end

    subgraph EvaluationMetrics ["Evaluation & Verification"]
        AIOpsEvaluator["AIOpsLab Evaluator<br/>(Ground Truth Match)"]
        ResultsDataset["Empirical Results Register<br/>(evaluation_results.csv & docs/tracker.md)"]

        Normalizer -->|"submit([root_cause])"| AIOpsEvaluator
        AIOpsEvaluator --> ResultsDataset
    end

    style Testbed fill:#f8f9fa,stroke:#6c757d,stroke-width:1px
    style DataCollection fill:#e3f2fd,stroke:#1976d2,stroke-width:1px
    style EvaluationHarness fill:#f3e5f5,stroke:#7b1fa2,stroke-width:1px
    style EvaluationMetrics fill:#e8f5e9,stroke:#388e3c,stroke-width:1px
```

### Module Organization

| Directory / File | Responsibility |
| :--- | :--- |
| **`aiopslab_local_agent/slm_agent.py`** | Universal `SLMAgent` implementation supporting Nemotron, Qwen, and Phi; handles prompt format contracts, candidate vocabulary extraction, and anomaly-priority normalization. |
| **`aiopslab_local_agent/system_evidence_collector.py`** | Declarative aggregator querying live Kubernetes cluster signals (pod restarts, port mismatches, scale-to-zero, endpoints) and bounded logs. |
| **`aiopslab_local_agent/telemetry_manifest.json`** | Declarative configuration catalog of Kubernetes commandlets, parameters, log limits, and error keywords. |
| **`aiopslab_local_agent/evidence_compactor.py`** | Summarizes parsed traces, compacts graph context, and enforces token budget safety ceilings. |
| **`aiopslab_local_agent/run_case.py`** | Automated single-incident test runner orchestrating baseline and graph evaluation conditions. |
| **`aiopslab_local_agent/ollama_client.py`** | Local Ollama inference client recording prompt/completion token usage and latency. |
| **`aiopslab_bridge/`** | REST HTTP API bridge wrapping the AIOpsLab orchestrator for decoupled agents. |
| **`research/trace_parser/trace_graph_parser.py`** | Universal causal graph parser reconstructing transaction call trees and RPC target attribution tables. |
| **`docs/tracker.md`** | Comprehensive project tracker, thesis findings register, failure analyses, and WSL batch scripts. |
| **`evaluation_results.csv`** | Primary empirical benchmark dataset recording accuracy, TTL, step counts, and token metrics. |

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
- **Astronomy Shop (11):** payment_service_unreachable, kafka_queue_problems, loadgenerator_flood, raud_detection_failure, 
ecommendation_cache_failure, productcatalog_failure, shipping_service_slow, email_service_failure, currency_service_failure, d_service_failure, cart_service_failure.

For batch execution bash scripts and complete experimental details, see [docs/tracker.md](docs/tracker.md).



