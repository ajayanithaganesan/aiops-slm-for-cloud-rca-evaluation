"""
AIOpsLab Evaluation Case Runner for Local SLMs

Executes an incident localization benchmark case on AIOpsLab using a local SLM
served via Ollama. It handles:
1. Orchestrator initialization and agent registration
2. Optional preloading of telemetry evidence (traces + system signals)
3. Model evaluation step execution
4. Detailed telemetry, token counts, and result logging to evaluation_results.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime
import json
from pathlib import Path
import re
import time

from aiopslab.orchestrator import Orchestrator

from .slm_agent import SLMAgent
from .system_evidence_collector import SystemEvidenceCollector


def parse_arguments() -> argparse.Namespace:
    """Parses command-line arguments for running an evaluation benchmark case."""
    parser = argparse.ArgumentParser(description="Run an AIOpsLab localization case with a local SLM.")
    
    # Problem ID to evaluate (e.g., astronomy_shop_ad_service_failure-localization-1)
    parser.add_argument("problem_id")
    
    # Local Ollama model tag to use (e.g., nemotron-3-nano:4b, qwen2.5:7b)
    parser.add_argument("--model", default=None)
    
    # Local Ollama endpoint URL
    parser.add_argument("--ollama-url", default=None)
    
    # Maximum agent steps before timing out
    parser.add_argument("--max-steps", type=int, default=10)
    
    # Output directory for saving session run JSON files
    parser.add_argument("--output", default="research/local_agent_runs")
    
    # Evaluation condition: 'baseline' (raw traces) or 'graph' (compacted graph parser)
    parser.add_argument("--condition", choices=["baseline", "graph"], default="baseline")
    
    # Whether to preload traces and cluster state before the model's first decision
    parser.add_argument("--preload-evidence", action="store_true")
    
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()

    # 1. Initialize local agent and connect to AIOpsLab orchestrator
    agent = SLMAgent(model=args.model, base_url=args.ollama_url, condition=args.condition)
    orchestrator = Orchestrator()
    orchestrator.register_agent(agent, name="slm-ollama")

    description, instructions, actions = orchestrator.init_problem(args.problem_id)
    agent.init_context(description, instructions, actions)

    run_started = time.time()
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    evaluation_dir = Path("research/test_evaluations") / f"{run_stamp}_{args.condition}_{args.problem_id}"
    agent.configure_run(evaluation_dir)

    # 2. Preload evidence pipeline if requested
    if args.preload_evidence:
        trace_response = orchestrator.session.problem.perform_action("get_traces", agent.namespace, 5)
        trace_path_match = re.search(r"(/[^ ]+\.csv)", str(trace_response))
        trace_path = trace_path_match.group(1) if trace_path_match else ""
        evidence_parts = [f"TRACE ACTION RESULT:\n{trace_response}"]
        context_paths = []

        collector = SystemEvidenceCollector(orchestrator.session.problem.perform_action, agent.namespace)

        # Discover cluster services so dead or stopped components are not missed
        cluster_services = collector.discover_cluster_services()
        trace_services = set()
        if trace_path:
            trace_file = Path(trace_path)
            if trace_file.exists():
                with trace_file.open(newline="", encoding="utf-8") as file:
                    for row in csv.DictReader(file):
                        if row.get("service_name"):
                            trace_services.add(row["service_name"].strip())

        all_services = sorted(set(cluster_services) | trace_services)

        if trace_path:
            context_paths.append(trace_path)
            if args.condition == "graph":
                agent._prepare_fresh_context(trace_path)
                compact_path = evaluation_dir / "context" / "slm_context_compact.txt"
                if compact_path.exists():
                    evidence_parts.append("PARSER GRAPH CONTEXT:\n" + compact_path.read_text(encoding="utf-8"))
                    context_paths.append(str(compact_path))
            else:
                trace_file = Path(trace_path)
                if trace_file.exists():
                    evidence_parts.append("RAW TRACE TELEMETRY (CSV SPANS):\n" + trace_file.read_text(encoding="utf-8"))

        # Gather cluster objective signals (pod crashes, scaling, port mismatches, logs etc.)
        system_context = collector.build_summary_context(services=all_services if all_services else None, trace_csv_path=trace_path)
        evidence_parts.append(system_context)

        candidate_header = f"CANDIDATE SERVICE IDENTIFIERS:\n{all_services}\n\n"
        agent.preload_evidence(candidate_header + "\n\n".join(evidence_parts), context_paths)

    # 3. Execute problem
    result = asyncio.run(orchestrator.start_problem(max_steps=args.max_steps))
    elapsed = time.time() - run_started

    # 4. Save session results
    output_dir = Path(args.output) / str(orchestrator.session.session_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "result.json").write_text(
        json.dumps({
            "problem_id": args.problem_id,
            "condition": args.condition,
            "result": result,
            "model_metrics": agent.metrics(),
            "context_paths": agent.context_paths,
        }, default=str, indent=2),
        encoding="utf-8",
    )

    calls = agent.metrics()["calls"]
    in_tokens = sum(call.get("prompt_tokens", 0) for call in calls)
    out_tokens = sum(call.get("completion_tokens", 0) for call in calls)
    history_text = "\n".join(str(item) for item in getattr(orchestrator.session, "history", []))
    actions_used = re.findall(r"(?:get_logs|get_traces|get_metrics|read_logs|read_traces|read_metrics|exec_shell|submit)\s*\(", history_text)

    # 5. Write simple test report
    report = [
        "AIOpsLab LOCAL SLM EVALUATION",
        f"timestamp: {datetime.now().isoformat(timespec='seconds')}",
        f"problem_id: {args.problem_id}",
        f"condition: {args.condition}",
        f"model: {agent.llm.model}",
        f"elapsed_seconds: {elapsed:.3f}",
        f"max_steps: {args.max_steps}",
        f"steps: {result.get('results', {}).get('steps', 'unknown') if isinstance(result, dict) else 'unknown'}",
        f"input_tokens: {in_tokens}",
        f"output_tokens: {out_tokens}",
        f"model_calls: {len(calls)}",
        f"tools_observed: {', '.join(sorted(set(action.rstrip('(') for action in actions_used))) or 'not captured'}",
        f"context_files: {', '.join(agent.context_paths) or 'none (baseline)'}",
        "",
        "RESULTS",
        json.dumps(result, default=str, indent=2),
        "",
        "MODEL_CALL_METRICS",
        json.dumps(calls, default=str, indent=2),
    ]
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    (evaluation_dir / f"{run_stamp}_{args.condition}.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    (evaluation_dir / "run_metadata.json").write_text(
        json.dumps({"problem_id": args.problem_id, "condition": args.condition, "elapsed_seconds": elapsed, "context_paths": agent.context_paths}, indent=2),
        encoding="utf-8",
    )

    # 6. Append metrics row to evaluation CSVs
    results_dir = Path("reports")
    results_dir.mkdir(parents=True, exist_ok=True)
    results_csv = results_dir / "evaluation_results.csv"
    root_results_csv = Path("evaluation_results.csv")
    result_values = result.get("results", {}) if isinstance(result, dict) else {}

    tools = sorted(set(actions_used))
    submission = ""
    submissions = re.findall(r"submit\s*\(([^)]*)\)", history_text)
    if submissions:
        submission = submissions[-1]

    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "session_id": str(orchestrator.session.session_id),
        "problem_id": args.problem_id,
        "condition": args.condition,
        "model": agent.llm.model,
        "localization_accuracy": result_values.get("Localization Accuracy", ""),
        "success": result_values.get("success", ""),
        "is_subset": result_values.get("is_subset", ""),
        "ttl_seconds": result_values.get("TTL", ""),
        "steps": result_values.get("steps", ""),
        "elapsed_seconds": round(elapsed, 3),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "model_calls": len(calls),
        "tools_used": ",".join(tool.rstrip("(") for tool in tools),
        "submission": submission,
        "evaluation_report": str(evaluation_dir),
        "run_artifacts": str(output_dir),
        "context_source_chars": getattr(agent, "context_stats", {}).get("source_chars", 0),
        "context_sent_chars": getattr(agent, "context_stats", {}).get("sent_chars", len(getattr(agent, "evidence_context", ""))),
        "context_estimated_sent_tokens": getattr(agent, "context_stats", {}).get("estimated_sent_tokens", max(0, len(getattr(agent, "evidence_context", "")) // 4)),
        "duplicate_actions": getattr(agent, "duplicate_actions", 0),
        "decision_prompt_sent": getattr(agent, "decision_prompt_sent", False),
        "preload_enabled": getattr(agent, "preloaded", False),
        "preload_decision_calls": getattr(agent, "preload_decision_calls", 0),
        "invalid_model_actions": getattr(agent, "invalid_model_actions", 0),
        "decision_contract_passed": getattr(agent, "decision_contract_passed", False),
        "prompt_chars": sum(call.get("prompt_chars", 0) or 0 for call in calls),
    }

    with results_csv.open("a", newline="", encoding="utf-8") as file:
        existing_header = []
        if file.tell() > 0:
            with results_csv.open("r", newline="", encoding="utf-8") as read_file:
                existing_header = next(csv.reader(read_file), [])
        fieldnames = existing_header or list(row)
        for name in row:
            if name not in fieldnames:
                fieldnames.append(name)
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        if not existing_header:
            writer.writeheader()
        writer.writerow(row)

    # Sync resultsto root evaluation table
    root_results_csv.write_text(results_csv.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"Updated comparison CSV: {results_csv}")
    print(f"Updated root comparison CSV: {root_results_csv}")
    print(f"Saved evaluation report to {evaluation_dir}")
    print(f"Saved run to {output_dir}")


if __name__ == "__main__":
    main()
