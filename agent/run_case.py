"""Run one AIOpsLab localization case with Nemotron through Ollama."""

import argparse
import asyncio
import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path

from aiopslab.orchestrator import Orchestrator

from .nemotron_agent import NemotronAgent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("problem_id")
    parser.add_argument("--model", default=None)
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--output", default="research/local_agent_runs")
    parser.add_argument("--condition", choices=["baseline", "graph"], default="baseline")
    args = parser.parse_args()

    agent = NemotronAgent(model=args.model, base_url=args.ollama_url, condition=args.condition)
    orchestrator = Orchestrator()
    orchestrator.register_agent(agent, name="nemotron-ollama")
    description, instructions, actions = orchestrator.init_problem(args.problem_id)
    agent.init_context(description, instructions, actions)
    run_started = time.time()
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    evaluation_dir = Path("research/test_evaluations") / f"{run_stamp}_{args.condition}_{args.problem_id}"
    agent.configure_run(evaluation_dir)
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
    }
    with results_csv.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        if file.tell() == 0:
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
