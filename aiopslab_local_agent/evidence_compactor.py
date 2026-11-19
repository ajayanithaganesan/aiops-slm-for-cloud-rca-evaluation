"""
Evidence Compactor for Local SLM Context Window

When investigating microservice failures, raw distributed traces and logs can
quickly exceed 50,000+ characters. Small Language Models (e.g., Llama-3-8B / Qwen-2.5-7B)
have limited attention spans and strict context windows.

This module shrinks raw telemetry down to a compact, budget-limited prompt without
losing critical signals (error leaf nodes, RPC calls, bottleneck operations).
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re


# Regular expressions for identifying traces and parsing tabular output
REPEATED_RECORD = re.compile(r"^\s*- .*\btrace=[^|]+\|.*\bspan=")
IDENTIFIERS = re.compile(r"\b(trace|span)=[^| ]+")
OPERATION_ROW = re.compile(r"^-\s+([^|]+)\|\s+([^|]+)\|")
ERROR_OPERATION_ROW = re.compile(
    r"^\s*-\s*service=([^|]+)\|\s*operation=([^|]+)\|.*?response=([^|\n]+)"
)

# Default context character budget to fit comfortably into the SLM prompt
DEFAULT_CONTEXT_CHAR_BUDGET = 12000


def _apply_context_budget(lines: list[str], budget: int) -> tuple[list[str], int]:
    """
    Trims low-priority lines so the prompt fits inside our SLM character budget.
    We prioritize high-value sections like error propagation, service failures,
    and bottleneck summaries over long lists of individual raw spans.
    """
    if len("\n".join(lines)) <= budget:
        return lines, 0

    # These section headers and labels represent our highest-value analytical clues
    priority_terms = (
        "Incident scope:", "Behavioural evidence:", "Dependency evidence:",
        "Error/status evidence:", "Performance evidence:", "Temporal failure evidence",
        "Differential trace execution:", "Topological ambient utility",
        "Error propagation DAG terminal leaves", "Critical-path span self-time bottlenecks",
        "Service failure comparison", "explicit_errors=", "failed_operations=",
        "Evidence limitations", "score=", "Path ",
        "observed relationships:", "repeated evidence pattern",
        "EVIDENCE-ONLY DECISION CONTRACT", "service_name identifies",
        "OPERATION FAILURE SUMMARY", "emitter=", "operation=",
    )

    selected_lines: list[str] = []
    used_characters = 0

    for index, line in enumerate(lines):
        # The explicit-error section can contain hundreds of raw spans.
        # The operation failure summary captures this more compactly,
        # so we keep only the early representative records.
        if index >= 95 and line.lstrip().startswith("- service=") and "duration=" in line:
            continue

        keep = index < 95 or any(term in line for term in priority_terms)
        if not keep:
            continue

        line_length = len(line) + (1 if selected_lines else 0)
        is_operation_summary = "OPERATION FAILURE SUMMARY" in line or "emitter=" in line
        available_budget = budget - 180 if is_operation_summary else budget - 2200

        if used_characters + line_length > available_budget:
            continue

        selected_lines.append(line)
        used_characters += line_length

    omitted_count = len(lines) - len(selected_lines)
    selected_lines.extend([
        "",
        f"CONTEXT BUDGET: {omitted_count} lower-priority parser lines omitted from the SLM prompt.",
        "Complete parser evidence remains available in the preserved graph artifact.",
    ])
    return selected_lines, omitted_count


def _summarize_repeated_records(lines: list[str]) -> tuple[list[str], int]:
    """
    Groups repeated identical trace/span patterns to avoid filling context
    with dozens of duplicate HTTP or RPC spans.
    """
    kept_lines: list[str] = []
    signatures: Counter[str] = Counter()
    summarized_count = 0

    for line in lines:
        if not REPEATED_RECORD.search(line):
            kept_lines.append(line)
            continue

        # Removes trace and span IDs to find duplicate structural patterns
        signature = IDENTIFIERS.sub("", line).strip()
        signatures[signature] += 1

        # Keep the first 3 examples of any pattern
        if signatures[signature] <= 3:
            kept_lines.append(line)
        else:
            summarized_count += 1

    # Report how many times duplicate patterns occurred
    for signature, count in signatures.items():
        if count > 3:
            kept_lines.append(f"- repeated evidence pattern observed {count} times: {signature}")

    return kept_lines, summarized_count


def build_compact_context(full_context: str, char_budget: int = DEFAULT_CONTEXT_CHAR_BUDGET) -> tuple[str, dict]:
    """
    Compresses parser evidence into a clean, concise prompt context for the SLM.
    Preserves exact error relations, caller-callee mappings, and failure summaries.
    """
    raw_lines = full_context.splitlines()
    selected_lines, summarized_count = _summarize_repeated_records(raw_lines)

    # 1. Parse observed operation mappings (clarify emitter vs RPC target)
    operation_pairs = []
    for line in raw_lines:
        match = OPERATION_ROW.match(line.strip())
        if match:
            emitter, operation = match.groups()
            if "=" in emitter or "=" in operation:
                continue
            operation_pairs.append(f"{emitter.strip()} emits {operation.strip()}")

    if operation_pairs:
        unique_pairs = list(dict.fromkeys(operation_pairs))[:30]
        selected_lines.extend([
            "",
            "TELEMETRY FIELD INTERPRETATION",
            "- service_name identifies the service that emitted the span.",
            "- operation_name identifies the observed operation and may name a remote RPC target.",
            "- Do not treat an emitting service as the operation target without supporting evidence.",
            "- Observed operation mappings:",
            *[f"  - {pair}" for pair in unique_pairs],
        ])

    # 2. Aggregate explicit operation failures
    operation_failures: Counter[tuple[str, str, str]] = Counter()
    for line in raw_lines:
        match = ERROR_OPERATION_ROW.match(line)
        if not match:
            continue
        emitter, operation, response = (item.strip() for item in match.groups())
        operation_failures[(emitter, operation, response)] += 1

    if operation_failures:
        selected_lines.extend(["", "OPERATION FAILURE SUMMARY"])
        for (emitter, operation, response), count in operation_failures.most_common(20):
            target = "unknown"
            # Multi-protocol target extraction:
            # Check for gRPC naming: e.g., CartService/AddItem -> cart-service
            grpc_match = re.search(r"/?(?:[a-zA-Z0-9_]+\.)*([a-zA-Z0-9]+)Service/", operation)
            if grpc_match:
                service_class = grpc_match.group(1)
                target = re.sub(r"(?<!^)(?=[A-Z])", "-", service_class).lower()
            else:
                # Check for REST route naming: e.g., /api/v1/orders -> orders
                route_match = re.search(r"/(?:api(?:/v\d+)?)?/([a-zA-Z0-9_-]+)", operation)
                if route_match:
                    target = route_match.group(1).lower()

            selected_lines.append(
                f"- emitter={emitter} | operation={operation} | observations={count} "
                f"| response={response} | operation_target_label={target}"
            )

    # 3. Decision contract rules for the SLM
    selected_lines.extend([
        "",
        "EVIDENCE-ONLY DECISION CONTRACT",
        "- service_name identifies the component that emitted a span.",
        "- operation_name identifies observed behaviour and may represent a downstream RPC.",
        "- Separate callers from downstream dependencies before localizing a fault.",
        "- Passive telemetry collectors transport observation signals and are not root-cause application services.",
        "- Use explicit status/error evidence before latency-only evidence.",
        "- Consider the deepest supported failing component, not merely its caller.",
        "- Submit only identifiers present in the telemetry evidence.",
        "- These rules are interpretation guidance; the parser does not select an RCA.",
    ])

    # 4. Implement context budget constraints
    selected_lines, omitted_lines = _apply_context_budget(selected_lines, char_budget)
    compact_text = "\n".join(selected_lines).strip() + "\n"

    manifest = {
        "source": "parser-produced graph context",
        "source_line_count": len(raw_lines),
        "compact_line_count": len(selected_lines),
        "budget_chars": char_budget,
        "budget_lines_omitted": omitted_lines,
        "compact_char_count": len(compact_text),
        "repeated_records_summarized": summarized_count,
        "raw_parser_artifacts_preserved": True,
        "root_cause_decision_made_by_processor": False,
        "ground_truth_used": False,
    }
    return compact_text, manifest


def process(parser_output: str | Path, output_directory: str | Path, char_budget: int = DEFAULT_CONTEXT_CHAR_BUDGET) -> dict:
    """Reads raw parser context, builds compact view, and saves artifacts with manifest."""
    source_path = Path(parser_output)
    output_path = Path(output_directory)
    output_path.mkdir(parents=True, exist_ok=True)

    full_context = source_path.read_text(encoding="utf-8")
    compact_context, manifest = build_compact_context(full_context, char_budget)

    (output_path / "slm_context_compact.txt").write_text(compact_context, encoding="utf-8")
    (output_path / "evidence_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parser_output", type=Path, help="Path to raw parser context file")
    parser.add_argument("output_directory", type=Path, help="Output directory for compacted context")
    parser.add_argument("--char-budget", type=int, default=DEFAULT_CONTEXT_CHAR_BUDGET, help="Maximum character budget")
    args = parser.parse_args()

    manifest = process(args.parser_output, args.output_directory, args.char_budget)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
