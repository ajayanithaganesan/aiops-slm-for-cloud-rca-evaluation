"""Build an SLM-sized view without changing the parser's evidence artifacts.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


REPEATED_RECORD = re.compile(r"^\s*- .*\btrace=[^|]+\|.*\bspan=")
IDENTIFIERS = re.compile(r"\b(trace|span)=[^| ]+")
OPERATION_ROW = re.compile(r"^-\s+([^|]+)\|\s+([^|]+)\|")
ERROR_OPERATION_ROW = re.compile(
    r"^\s*-\s*service=([^|]+)\|\s*operation=([^|]+)\|.*?response=([^|\n]+)"
)
DEFAULT_CONTEXT_CHAR_BUDGET = 12000


def _apply_context_budget(lines: list[str], budget: int) -> tuple[list[str], int]:
    """Bound prompt size while retaining summaries and representative evidence."""
    if len("\n".join(lines)) <= budget:
        return lines, 0
    priority_terms = (
        "Incident scope:", "Behavioural evidence:", "Dependency evidence:",
        "Error/status evidence:", "Performance evidence:", "Temporal failure evidence",
        "Service failure comparison", "explicit_errors=", "failed_operations=",
        "Evidence limitations", "score=", "Path ",
        "observed relationships:", "repeated evidence pattern",
        "EVIDENCE-ONLY DECISION CONTRACT", "service_name identifies",
        "OPERATION FAILURE SUMMARY", "emitter=", "operation=",
    )
    selected: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        # The full explicit-error section can contain hundreds of raw spans.
        # The operation-failure summary below represents that evidence more
        # efficiently, so retain only the early representative records here.
        if index >= 95 and line.lstrip().startswith("- service=") and "duration=" in line:
            continue
        keep = index < 95 or any(term in line for term in priority_terms)
        if not keep:
            continue
        addition = len(line) + (1 if selected else 0)
        is_operation_summary = "OPERATION FAILURE SUMMARY" in line or "emitter=" in line
        available = budget - 180 if is_operation_summary else budget - 2200
        if used + addition > available:
            continue
        selected.append(line)
        used += addition
    omitted = len(lines) - len(selected)
    selected.extend([
        "",
        f"CONTEXT BUDGET: {omitted} lower-priority parser lines omitted from the SLM prompt.",
        "Complete parser evidence remains available in the preserved graph artifact.",
    ])
    return selected, omitted


def _summarize_repeated_records(lines: list[str]) -> tuple[list[str], int]:
    """Keep representative records and report how many were summarized."""
    kept: list[str] = []
    signatures: Counter[str] = Counter()
    summarized = 0
    for line in lines:
        if not REPEATED_RECORD.search(line):
            kept.append(line)
            continue
        signature = IDENTIFIERS.sub("", line).strip()
        signatures[signature] += 1
        if signatures[signature] <= 3:
            kept.append(line)
        else:
            summarized += 1
    for signature, count in signatures.items():
        if count > 3:
            kept.append(f"- repeated evidence pattern observed {count} times: {signature}")
    return kept, summarized


def build_compact_context(full_context: str, char_budget: int = DEFAULT_CONTEXT_CHAR_BUDGET) -> tuple[str, dict]:
    """Create a deterministic compact context from parser-produced text."""
    lines = full_context.splitlines()
    selected, summarized = _summarize_repeated_records(lines)

    # This is a telemetry-schema clarification, not a diagnosis. In
    # distributed traces the service_name is the span emitter; an operation
    # may represent a remote RPC target or dependency.
    operation_pairs = []
    for line in lines:
        match = OPERATION_ROW.match(line.strip())
        if match:
            emitter, operation = match.groups()
            if "=" in emitter or "=" in operation:
                continue
            operation_pairs.append(f"{emitter.strip()} emits {operation.strip()}")
    if operation_pairs:
        unique_pairs = list(dict.fromkeys(operation_pairs))[:30]
        selected.extend([
            "",
            "TELEMETRY FIELD INTERPRETATION",
            "- service_name identifies the service that emitted the span.",
            "- operation_name identifies the observed operation and may name a remote RPC target.",
            "- Do not treat an emitting service as the operation target without supporting evidence.",
            "- Observed operation mappings:",
            *[f"  - {pair}" for pair in unique_pairs],
        ])

    # Aggregate explicit operation failures without converting them into an
    # RCA. The target label is derived from RPC naming convention only.
    operation_failures: Counter[tuple[str, str, str]] = Counter()
    for line in lines:
        match = ERROR_OPERATION_ROW.match(line)
        if not match:
            continue
        emitter, operation, response = (item.strip() for item in match.groups())
        operation_failures[(emitter, operation, response)] += 1
    if operation_failures:
        selected.extend(["", "OPERATION FAILURE SUMMARY"])
        for (emitter, operation, response), count in operation_failures.most_common(20):
            target = "unknown"
            rpc_match = re.match(r"/?([^./]+)\.[^/]+/", operation)
            if rpc_match:
                target = rpc_match.group(1)
            selected.append(
                f"- emitter={emitter} | operation={operation} | observations={count} "
                f"| response={response} | operation_target_label={target}"
            )

    # A generic evidence contract helps an SLM interpret telemetry consistently
    # across incident types without selecting or naming a root cause.
    selected.extend([
        "",
        "EVIDENCE-ONLY DECISION CONTRACT",
        "- service_name identifies the component that emitted a span.",
        "- operation_name identifies observed behaviour and may represent a downstream RPC.",
        "- Separate callers from downstream dependencies before localizing a fault.",
        "- Use explicit status/error evidence before latency-only evidence.",
        "- Consider the deepest supported failing component, not merely its caller.",
        "- Submit only identifiers present in the telemetry evidence.",
        "- These rules are interpretation guidance; the parser does not select an RCA.",
    ])

    # Preserve the complete parser summary and the first representative records,
    # while preventing repeated raw records from dominating the SLM prompt.
    selected, omitted = _apply_context_budget(selected, char_budget)
    compact = "\n".join(selected).strip() + "\n"
    manifest = {
        "source": "parser-produced graph context",
        "source_line_count": len(lines),
        "compact_line_count": len(selected),
        "budget_chars": char_budget,
        "budget_lines_omitted": omitted,
        "compact_char_count": len(compact),
        "repeated_records_summarized": summarized,
        "raw_parser_artifacts_preserved": True,
        "root_cause_decision_made_by_processor": False,
        "ground_truth_used": False,
    }
    return compact, manifest


def process(parser_output: str | Path, output_directory: str | Path, char_budget: int = DEFAULT_CONTEXT_CHAR_BUDGET) -> dict:
    """Write compact context and an audit manifest beside parser artifacts."""
    source = Path(parser_output)
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    full_context = source.read_text(encoding="utf-8")
    compact, manifest = build_compact_context(full_context, char_budget)
    (destination / "slm_context_compact.txt").write_text(compact, encoding="utf-8")
    (destination / "evidence_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parser_output", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--char-budget", type=int, default=DEFAULT_CONTEXT_CHAR_BUDGET)
    args = parser.parse_args()
    manifest = process(args.parser_output, args.output_directory, args.char_budget)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
