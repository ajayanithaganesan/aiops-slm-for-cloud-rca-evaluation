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


def build_compact_context(full_context: str) -> tuple[str, dict]:
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

    # Preserve the complete parser summary and the first representative records,
    # while preventing repeated raw records from dominating the SLM prompt.
    compact = "\n".join(selected).strip() + "\n"
    manifest = {
        "source": "parser-produced graph context",
        "source_line_count": len(lines),
        "compact_line_count": len(selected),
        "repeated_records_summarized": summarized,
        "raw_parser_artifacts_preserved": True,
        "root_cause_decision_made_by_processor": False,
        "ground_truth_used": False,
    }
    return compact, manifest


def process(parser_output: str | Path, output_directory: str | Path) -> dict:
    """Write compact context and an audit manifest beside parser artifacts."""
    source = Path(parser_output)
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    full_context = source.read_text(encoding="utf-8")
    compact, manifest = build_compact_context(full_context)
    (destination / "slm_context_compact.txt").write_text(compact, encoding="utf-8")
    (destination / "evidence_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parser_output", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    manifest = process(args.parser_output, args.output_directory)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
