"""Run one AIOpsLab localization case with a local SLM through Ollama."""

import argparse
import asyncio
import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path

from aiopslab.orchestrator import Orchestrator

from .slm_agent import SLMAgent
from .system_evidence_collector import SystemEvidenceCollector


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("problem_id")
    parser.add_argument("--model", default=None)
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--output", default="research/local_agent_runs")
    parser.add_argument("--condition", choices=["baseline", "graph"], default="baseline")
    parser.add_argument("--preload-evidence", action="store_true", help="collect evidence before the model's first RCA decision")
    args = parser.parse_args()

    agent = SLMAgent(model=args.model, base_url=args.ollama_url, condition=args.condition)
    orchestrator = Orchestrator()
    orchestrator.register_agent(agent, name="slm-ollama")
    description, instructions, actions = orchestrator.init_problem(args.problem_id)
    agent.init_context(description, instructions, actions)
    run_started = time.time()
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    evaluation_dir = Path("research/test_evaluations") / f"{run_stamp}_{args.condition}_{args.problem_id}"
    agent.configure_run(evaluation_dir)
    if args.preload_evidence:
        trace_response = orchestrator.session.problem.perform_action("get_traces", agent.namespace, 5)
        trace_path_match = re.search(r"(/[^ ]+\.csv)", str(trace_response))
        trace_path = trace_path_match.group(1) if trace_path_match else ""
        evidence_parts = [f"TRACE ACTION RESULT:\n{trace_response}"]
        context_paths = []

        collector = SystemEvidenceCollector(orchestrator.session.problem.perform_action, agent.namespace)

        # Discover all candidate services from the cluster, ensuring dead services are not omitted
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

        # Place the objective cluster & system anomaly signals right before the final decision
        # to ensure the model focuses on root anomalies rather than getting distracted by noisy logs.
        # Note: Bounded log extraction is handled uniformly inside collector without stdout spam or context explosion.
        system_context = collector.build_summary_context(services=all_services if all_services else None, trace_csv_path=trace_path)
        evidence_parts.append(system_context)

        candidate_header = f"CANDIDATE SERVICE IDENTIFIERS:\n{all_services}\n\n"
        agent.preload_evidence(candidate_header + "\n\n".join(evidence_parts), context_paths)
    result = asyncio.run(orchestrator.start_problem(max_steps=args.max_steps))
    elapsed = time.time() - run_started

    output_dir = Path(args.output) / str(orchestrator.session.session_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "result.json").write_text(
        json.dumps({"problem_id": args.problem_id, "condition": args.condition, "result": result, "model_metrics": agent.metrics(), "context_paths": agent.context_paths}, default=str, indent=2),
        encoding="utf-8",
    )
    calls = agent.metrics()["calls"]
    in_tokens = sum(call.get("prompt_tokens", 0) for call in calls)
    out_tokens = sum(call.get("completion_tokens", 0) for call in calls)
    actions_used = re.findall(r"(?:get_logs|get_traces|get_metrics|read_logs|read_traces|read_metrics|exec_shell|submit)\s*\(", "\n".join(str(item) for item in getattr(orchestrator.session, "history", [])))
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
    (evaluation_dir / "run_metadata.json").write_text(json.dumps({"problem_id": args.problem_id, "condition": args.condition, "elapsed_seconds": elapsed, "context_paths": agent.context_paths}, indent=2), encoding="utf-8")
    results_dir = Path("reports")
    results_dir.mkdir(parents=True, exist_ok=True)
    results_csv = results_dir / "evaluation_results.csv"
    root_results_csv = Path("evaluation_results.csv")
    result_values = result.get("results", {}) if isinstance(result, dict) else {}
    history_text = "\n".join(str(item) for item in getattr(orchestrator.session, "history", []))
    tools = sorted(set(re.findall(r"(?:get_logs|get_traces|get_metrics|read_logs|read_traces|read_metrics|exec_shell|submit)\s*\(", history_text)))
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
    # Keep the primary comparison table at repository root for easy access.
    root_results_csv.write_text(results_csv.read_text(encoding="utf-8"), encoding="utf-8")
    tracker = Path("tracker.md")
    with tracker.open("a", encoding="utf-8") as file:
        file.write(
            f"\n- {row['timestamp']} — {row['model']} | {row['condition']} | "
            f"{row['problem_id']} | accuracy={row['localization_accuracy']} | "
            f"success={row['success']} | steps={row['steps']} | "
            f"input_tokens={row['input_tokens']} | output_tokens={row['output_tokens']}\n"
        )
    print(f"Updated project tracker: {tracker}")
    print(f"Updated root comparison CSV: {root_results_csv}")
    print(f"Updated comparison CSV: {results_csv}")
    print(f"Saved evaluation report to {evaluation_dir}")
    print(f"Saved run to {output_dir}")


if __name__ == "__main__":
    main()
