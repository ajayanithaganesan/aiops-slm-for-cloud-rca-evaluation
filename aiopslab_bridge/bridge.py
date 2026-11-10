"""Minimal bridge for one read-only localization experiment at a time."""

from __future__ import annotations

import json
import csv
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from aiopslab.orchestrator import Orchestrator
from aiopslab.orchestrator.problems.registry import ProblemRegistry


ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "research" / "bridge_runs"

app = FastAPI(title="AIOpsLab Minimal Bridge", version="0.1.0")
active_run: "Run | None" = None


class StartRequest(BaseModel):
    problem_id: str


class ActionRequest(BaseModel):
    action: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class SubmitRequest(BaseModel):
    solution: Any


class Run:
    def __init__(self, problem_id: str):
        self.id = uuid.uuid4().hex
        self.problem_id = problem_id
        self.started_at = time.time()
        self.orchestrator = Orchestrator(results_dir=RUNS_DIR / self.id)
        self.orchestrator.register_agent(object(), name="bridge")
        self.directory = RUNS_DIR / self.id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.actions_file = self.directory / "actions.jsonl"
        self.closed = False

    def record(self, item: dict[str, Any]) -> None:
        with self.actions_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(item, default=str) + "\n")

    def recover(self) -> None:
        if self.closed or self.orchestrator.session is None:
            return
        problem = self.orchestrator.session.problem
        try:
            problem.recover_fault()
        finally:
            self.closed = True


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/problems")
def problems() -> list[str]:
    return ProblemRegistry().get_problem_ids(task_type="localization")


@app.post("/start")
def start(request: StartRequest) -> dict[str, Any]:
    global active_run
    if active_run is not None and not active_run.closed:
        raise HTTPException(409, "An experiment is already active")

    run = Run(request.problem_id)
    try:
        description, instructions, actions = run.orchestrator.init_problem(request.problem_id)
        run.orchestrator.session.start()
    except Exception:
        run.recover()
        raise

    active_run = run
    run.record({"event": "started", "problem_id": request.problem_id})
    return {
        "run_id": run.id,
        "problem_id": request.problem_id,
        "description": description,
        "instructions": instructions,
        "actions": actions,
    }


def get_run() -> Run:
    if active_run is None or active_run.closed:
        raise HTTPException(409, "No active experiment")
    return active_run


@app.post("/action")
def action(request: ActionRequest) -> dict[str, Any]:
    run = get_run()
    if request.action in {"exec_shell", "submit"}:
        raise HTTPException(400, "This bridge only permits read-only telemetry actions")

    if request.action == "summarize_traces":
        if len(request.args) != 1:
            raise HTTPException(400, "summarize_traces requires one CSV file path")
        result = summarize_traces(str(request.args[0]))
    else:
        try:
            result = run.orchestrator.session.problem.perform_action(
                request.action, *request.args, **request.kwargs
            )
        except Exception as error:
            result = f"{type(error).__name__}: {error}"

    session = run.orchestrator.session
    session.add({
        "role": "assistant",
        "content": json.dumps({"action": request.action, "args": request.args, "kwargs": request.kwargs}, default=str),
    })
    session.add({"role": "env", "content": str(result)})
    run.record({"event": "action", "action": request.action, "args": request.args, "kwargs": request.kwargs, "result": result})
    return {"run_id": run.id, "action": request.action, "result": result}


def summarize_traces(path: str) -> dict[str, Any]:
    """Return compact evidence while keeping raw trace rows on disk."""
    service_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    operation_counts: dict[str, int] = {}
    rows = 0

    try:
        with open(path, newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                rows += 1
                service = row.get("service_name", "unknown")
                operation = row.get("operation_name", "unknown")
                service_counts[service] = service_counts.get(service, 0) + 1
                operation_counts[operation] = operation_counts.get(operation, 0) + 1
                if row.get("has_error", "").lower() == "true":
                    error_counts[service] = error_counts.get(service, 0) + 1
    except OSError as error:
        return {"error": str(error), "path": path}

    return {
        "path": path,
        "rows": rows,
        "services": dict(sorted(service_counts.items(), key=lambda item: -item[1])),
        "errors_by_service": error_counts,
        "top_operations": dict(sorted(operation_counts.items(), key=lambda item: -item[1])[:20]),
    }


@app.post("/submit")
def submit(request: SubmitRequest) -> dict[str, Any]:
    global active_run
    run = get_run()
    session = run.orchestrator.session
    session.set_solution(request.solution)
    try:
        session.end()
        results = session.problem.eval(request.solution, session.history, session.get_duration())
        session.set_results(results)
        session.to_json()
        run.record({"event": "submitted", "solution": request.solution, "results": results})
        return {"run_id": run.id, "results": results, "artifacts": str(run.directory)}
    finally:
        run.recover()
        active_run = None


@app.post("/stop")
def stop() -> dict[str, str]:
    global active_run
    run = get_run()
    run.record({"event": "stopped"})
    run.recover()
    active_run = None
    return {"run_id": run.id, "status": "recovered"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("aiopslab_bridge.bridge:app", host="127.0.0.1", port=1818)
