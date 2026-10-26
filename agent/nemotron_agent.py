"""Nemotron agent using AIOpsLab's native action protocol."""

import json
import csv
import re
import os
import subprocess
import sys
from pathlib import Path

from .ollama_client import OllamaClient


class NemotronAgent:
    def __init__(self, model: str | None = None, base_url: str | None = None, condition: str = "baseline"):
        self.llm = OllamaClient(model=model, base_url=base_url)
        self.history: list[dict[str, str]] = []
        self.bootstrapped = False
        self.namespace = "default"
        self.evidence_context = ""
        self.condition = condition
        self.run_directory: Path | None = None
        self.context_paths: list[str] = []
        self.parsed_trace_paths: set[str] = set()
        self.evidence_context_sent = False
        self.action_history: list[str] = []

    def configure_run(self, run_directory: str | Path) -> None:
        self.run_directory = Path(run_directory)
        self.run_directory.mkdir(parents=True, exist_ok=True)

    def init_context(self, problem_desc: str, instructions: str, actions: dict[str, str]) -> None:
        action_text = "\n\n".join(f"{name}\n{description}" for name, description in actions.items())
        self.history = [
            {
                "role": "system",
                "content": (
                    "You are an AIOps incident localization agent.\n"
                    "Use only the APIs listed below. Inspect telemetry before submitting.\n"
                    "Return exactly one API call inside one Markdown code block.\n"
                    "Do not return JSON, XML, or multiple actions.\n"
                    "Use exact Kubernetes namespaces and lowercase service identifiers, not display names.\n"
                    "For this task, begin with this exact form: get_traces(\"test-hotel-reservation\", 5)\n"
                    "A valid log call is: get_logs(\"test-hotel-reservation\", \"geo\")\n"
                    "A valid submission is: submit([\"geo\"])\n"
                    "Always separate positional arguments with commas.\n"
                    "Do not use wildcards, shell pipes, redirects, semicolons, or extra text.\n"
                    "Use exec_shell only when a listed telemetry API cannot answer the question.\n\n"
                    f"TASK:\n{problem_desc}\n\n"
                    f"INSTRUCTIONS:\n{instructions}\n\n"
                    f"AVAILABLE APIS:\n{action_text}"
                ),
            }
        ]
        match = re.search(r"Namespace:\s*([\w-]+)", problem_desc)
        if match:
            self.namespace = match.group(1)
        self.bootstrapped = False
        self.evidence_context = ""
        self.context_paths = []
        self.parsed_trace_paths = set()
        self.evidence_context_sent = False
        self.action_history = []

    async def get_action(self, state: str) -> str:
        if not self.bootstrapped:
            self.bootstrapped = True
            action = f'```\nget_traces("{self.namespace}", 5)\n```'
            self._print_action("bootstrap", action)
            return action
        self.history.append({"role": "user", "content": self.augment_state(state)})
        raw_response = self.llm.chat(self.history)
        response = self.normalize_action(raw_response)
        response = self.avoid_duplicate_action(response)
        self._print_action("model", raw_response, response)
        self.history.append({"role": "assistant", "content": response})
        return response

    def avoid_duplicate_action(self, action: str) -> str:
        """Avoid wasting evaluation steps on identical telemetry requests."""
        normalized = action.strip()
        if normalized not in self.action_history:
            self.action_history.append(normalized)
            return action
        if normalized.startswith("```\nget_traces("):
            replacement = f'```\nget_metrics("{self.namespace}", 5)\n```'
        elif normalized.startswith("```\nget_logs("):
            replacement = f'```\nget_metrics("{self.namespace}", 5)\n```'
        else:
            return action
        self.action_history.append(replacement)
        print("===== DUPLICATE ACTION CONTROL =====", flush=True)
        print(f"Repeated action replaced with:\n{replacement}", flush=True)
        return replacement

    @staticmethod
    def _print_action(source: str, raw: str, normalized: str | None = None) -> None:
        """Print live model output without changing the agent protocol."""
        print(f"\n===== {source.upper()} MODEL OUTPUT =====", flush=True)
        print(raw, flush=True)
        if normalized is not None and normalized.strip() != raw.strip():
            print("===== NORMALIZED AIOPSLAB ACTION =====", flush=True)
            print(normalized, flush=True)

    def augment_state(self, state: str) -> str:
        """Add compact, model-neutral service inventory from exported traces."""
        if self.evidence_context and not self.evidence_context_sent:
            state = (
                f"{state}\n\n"
                "PARSER-GENERATED EVIDENCE (evidence only; make the RCA yourself):\n"
                f"{self.evidence_context}"
            )
            self.evidence_context_sent = True
        if self.evidence_context_sent and len(self.action_history) >= 3:
            state = (
                f"{state}\n\n"
                "STEP-BUDGET NOTICE: Fresh evidence and multiple telemetry actions "
                "are already available. Do not repeat an earlier tool call or invent "
                "file paths. Use one genuinely new signal only if necessary; otherwise "
                "submit your evidence-based conclusion now."
            )
        match = re.search(r"(?:exported to:|file )\s*(/[^\s]+\.csv)", state)
        if not match:
            return state

        path = match.group(1)
        if self.condition == "graph":
            self._prepare_fresh_context(path)
        services: set[str] = set()
        try:
            with open(path, newline="", encoding="utf-8") as file:
                for row in csv.DictReader(file):
                    service = row.get("service_name", "").strip()
                    if service:
                        services.add(service)
        except OSError:
            return state

        if not services:
            return state
        inventory = ", ".join(sorted(services))
        return (
            f"{state}\n\nSERVICE IDENTIFIERS DISCOVERED FROM TELEMETRY: {inventory}\n"
            "Use only one of these identifiers as the service argument to get_logs."
        )

    def _prepare_fresh_context(self, trace_path: str) -> None:
        """Parse exactly this run's trace file; never reuse stale context."""
        if trace_path in self.parsed_trace_paths or self.run_directory is None:
            return
        self.parsed_trace_paths.add(trace_path)
        parser_script = Path("research/trace_parser/trace_parser_universal_v7.py")
        if not parser_script.exists():
            return
        parser_output = self.run_directory / "parser"
        try:
            subprocess.run(
                [sys.executable, str(parser_script), trace_path, str(parser_output)],
                check=True, capture_output=True, text=True, timeout=180,
            )
            graph_context = parser_output / "slm_context_graph_augmented.txt"
            if not graph_context.exists():
                return
            from .universal_evidence_processor import process
            compact_output = self.run_directory / "context"
            process(graph_context, compact_output)
            compact_path = compact_output / "slm_context_compact.txt"
            # The Ollama context is configured externally. Keep the complete
            # compact evidence available up to a generous safety ceiling.
            self.evidence_context = compact_path.read_text(encoding="utf-8").strip()[:50000]
            self.context_paths = [str(graph_context), str(compact_path), str(compact_output / "evidence_manifest.json")]
        except (OSError, subprocess.SubprocessError):
            self.evidence_context = ""

    def normalize_action(self, response: str) -> str:
        """Keep AIOpsLab's strict parser boundary to one API call."""
        # Repair only syntax that omits the required namespace. This does not
        # choose an RCA; it converts a model-generated keyword call into the
        # native AIOpsLab positional API form.
        service_call = re.search(
            r"get_logs\s*\(\s*service\s*=\s*[\"']([^\"']+)[\"']\s*\)",
            response or "",
        )
        if service_call:
            return f'```\nget_logs("{self.namespace}", "{service_call.group(1)}")\n```'
        known = r"(?:get_logs|get_traces|get_metrics|read_logs|read_traces|read_metrics|exec_shell|submit)\s*\([^\n]*\)"
        blocks = re.findall(r"```(?:\w+)?\s*\n?(.*?)```", response or "", re.DOTALL)
        candidates = []
        for block in blocks:
            candidates.extend(re.findall(known, block))
        if not candidates:
            candidates = re.findall(known, response or "")
        if candidates:
            return f"```\n{candidates[0].strip()}\n```"
        return f'```\nget_traces("{self.namespace}", 5)\n```'

    def metrics(self) -> dict:
        return {"calls": self.llm.calls, "call_count": len(self.llm.calls)}
