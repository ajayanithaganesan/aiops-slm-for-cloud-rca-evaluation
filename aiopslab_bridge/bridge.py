"""Simple HTTP bridge for running AIOpsLab localization experiments.

Allows an external agent or script to interact with AIOpsLab via REST API.
"""

import csv
import json
from pathlib import Path
import time
import uuid

from aiopslab.orchestrator import Orchestrator
from aiopslab.orchestrator.problems.registry import ProblemRegistry
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Root paths for storing bridge run logs
ROOT_DIR = Path(__file__).resolve().parent.parent
BRIDGE_RUNS_DIR = ROOT_DIR / "research" / "bridge_runs"

app = FastAPI(title="AIOpsLab Minimal Bridge", version="0.1.0")
active_run = None


# Request schemas for FastAPI endpoints
class StartRequest(BaseModel):
    problem_id: str


class ActionRequest(BaseModel):
    action: str
    args: list = Field(default_factory=list)
    kwargs: dict = Field(default_factory=dict)


class SubmitRequest(BaseModel):
    solution: object


class Run:
    """Represents a single active benchmark problem run."""

    def __init__(self, problem_id):
        self.run_id = uuid.uuid4().hex
        self.problem_id = problem_id
        self.started_at = time.time()
        self.directory = BRIDGE_RUNS_DIR / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.actions_file = self.directory / "actions.jsonl"
        self.closed = False

        # Initialize AIOpsLab orchestrator
        self.orchestrator = Orchestrator(results_dir=self.directory)
        self.orchestrator.register_agent(object(), name="bridge")

    def record_event(self, event_data):
        """Appends an event line to our actions.jsonl audit log."""
        with open(self.actions_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event_data, default=str) + "\n")

    def cleanup(self):
        """Recovers the cluster fault and ends the session."""
        if self.closed or self.orchestrator.session is None:
            return
        problem = self.orchestrator.session.problem
        try:
            problem.recover_fault()
        finally:
            self.closed = True


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/problems")
def list_problems():
    """Returns list of available localization problems."""
    return ProblemRegistry().get_problem_ids(task_type="localization")


@app.post("/start")
def start_experiment(request: StartRequest):
    """Starts a new incident experiment session."""
    global active_run
    if active_run is not None and not active_run.closed:
        raise HTTPException(status_code=409, detail="An experiment is already active")

    run = Run(request.problem_id)
    try:
        desc, instructions, actions = run.orchestrator.init_problem(request.problem_id)
        run.orchestrator.session.start()
    except Exception:
        run.cleanup()
        raise

    active_run = run
    run.record_event({"event": "started", "problem_id": request.problem_id})
    return {
        "run_id": run.run_id,
        "problem_id": request.problem_id,
        "description": desc,
        "instructions": instructions,
        "actions": actions,
    }


def get_current_run():
    """Helper to get the currently running experiment."""
    if active_run is None or active_run.closed:
        raise HTTPException(status_code=409, detail="No active experiment")
    return active_run


@app.post("/action")
def execute_action(request: ActionRequest):
    """Executes a telemetry action in the active problem."""
    run = get_current_run()

    # Bridge only permits safe read-only actions
    if request.action in {"exec_shell", "submit"}:
        raise HTTPException(
            status_code=400,
            detail="This bridge only permits read-only telemetry actions",
        )

    if request.action == "summarize_traces":
        if len(request.args) != 1:
            raise HTTPException(
                status_code=400, detail="summarize_traces requires one CSV file path"
            )
        action_result = summarize_traces(str(request.args[0]))
    else:
        try:
            action_result = run.orchestrator.session.problem.perform_action(
                request.action, *request.args, **request.kwargs
            )
        except Exception as err:
            action_result = f"{type(err).__name__}: {err}"

    # Log to orchestrator session history
    session = run.orchestrator.session
    session.add({
        "role": "assistant",
        "content": json.dumps(
            {"action": request.action, "args": request.args, "kwargs": request.kwargs},
            default=str,
        ),
    })
    session.add({"role": "env", "content": str(action_result)})

    run.record_event({
        "event": "action",
        "action": request.action,
        "args": request.args,
        "kwargs": request.kwargs,
        "result": action_result,
    })
    return {"run_id": run.run_id, "action": request.action, "result": action_result}


def summarize_traces(csv_path):
    """Simple trace summary helper counting calls and errors per service."""
    service_counts = {}
    error_counts = {}
    operation_counts = {}
    total_rows = 0

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total_rows += 1
                svc = row.get("service_name", "unknown")
                op = row.get("operation_name", "unknown")
                service_counts[svc] = service_counts.get(svc, 0) + 1
                operation_counts[op] = operation_counts.get(op, 0) + 1

                if row.get("has_error", "").lower() == "true":
                    error_counts[svc] = error_counts.get(svc, 0) + 1
    except OSError as err:
        return {"error": str(err), "path": csv_path}

    # Sort by highest frequency
    sorted_services = dict(sorted(service_counts.items(), key=lambda item: -item[1]))
    top_operations = dict(sorted(operation_counts.items(), key=lambda item: -item[1])[:20])

    return {
        "path": csv_path,
        "rows": total_rows,
        "services": sorted_services,
        "errors_by_service": error_counts,
        "top_operations": top_operations,
    }


@app.post("/submit")
def submit_solution(request: SubmitRequest):
    """Submits the root-cause candidate and evaluates accuracy."""
    global active_run
    run = get_current_run()
    session = run.orchestrator.session
    session.set_solution(request.solution)

    try:
        session.end()
        evaluation_results = session.problem.eval(
            request.solution, session.history, session.get_duration()
        )
        session.set_results(evaluation_results)
        session.to_json()

        run.record_event({
            "event": "submitted",
            "solution": request.solution,
            "results": evaluation_results,
        })
        return {
            "run_id": run.run_id,
            "results": evaluation_results,
            "artifacts": str(run.directory),
        }
    finally:
        run.cleanup()
        active_run = None


@app.post("/stop")
def stop_experiment():
    """Stops and cleans up the active experiment."""
    global active_run
    run = get_current_run()
    run.record_event({"event": "stopped"})
    run.cleanup()
    active_run = None
    return {"run_id": run.run_id, "status": "recovered"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("aiopslab_bridge.bridge:app", host="127.0.0.1", port=1818)
