#!/usr/bin/env python3

"""
Cloud-Agnostic Distributed Trace Parser
========================================

Purpose
-------
Convert distributed-trace CSV data into structured RCA evidence.

Design goals
------------
1. Preserve raw telemetry.
2. Never assume a particular cloud provider.
3. Never assume Kubernetes, AWS, Azure, GCP, etc.
4. Automatically construct service dependency relationships.
5. Preserve operation-level evidence.
6. Preserve latency information.
7. Preserve error/status information without treating "Unknown" as an error.
8. Identify unusual latency using robust statistics.
9. Preserve temporal relationships.
10. Produce both detailed evidence and an SLM-friendly context.
11. Never use incident ground truth.
12. Avoid silently discarding telemetry.

Expected input columns
----------------------
trace_id
span_id
parent_span
service_name
operation_name
start_time
duration
has_error
response

The parser is tolerant of additional columns and preserves them.

Usage
-----
python trace_parser.py input.csv output_directory

Example
-------
python trace_parser.py \
    ~/AIOpsLab/trace_output/traces_1790951688.csv \
    ~/AIOpsLab/research/output/network_loss
"""

import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict
from statistics import mean, median, pstdev


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = {
    "trace_id",
    "span_id",
    "parent_span",
    "service_name",
    "operation_name",
    "start_time",
    "duration",
    "has_error",
    "response",
}

MAX_EXAMPLES_PER_CATEGORY = 25
TOP_OPERATIONS = 100
TOP_EDGES = 100
TOP_EVIDENCE = 100

# Universal evidence categories used by the SLM context.
EVIDENCE_CATEGORIES = (
    "INCIDENT_OVERVIEW",
    "BEHAVIOURAL_EVIDENCE",
    "DEPENDENCY_CAUSAL_EVIDENCE",
    "ERROR_STATUS_EVIDENCE",
    "PERFORMANCE_ANOMALY_EVIDENCE",
    "TELEMETRY_AVAILABILITY_LIMITATIONS",
)

# Values commonly used by telemetry systems to indicate that a response
# value was not available. These are NOT treated as failures.
UNKNOWN_RESPONSE_VALUES = {
    "",
    "unknown",
    "none",
    "null",
    "n/a",
    "na",
    "not_available",
    "not available",
}

# Status/error field names that may occur in telemetry exported by
# different tracing/observability systems. The parser does not require
# these fields, but will use them when available.
STATUS_FIELD_CANDIDATES = (
    "status",
    "status_code",
    "http_status",
    "http_status_code",
    "grpc_status",
    "grpc_status_code",
    "response_code",
    "return_code",
    "error_code",
)

ERROR_TEXT_FIELD_CANDIDATES = (
    "error",
    "error_message",
    "exception",
    "exception_message",
    "error_type",
    "exception_type",
    "message",
    "status_message",
)


# ---------------------------------------------------------------------------
# Generic conversion helpers
# ---------------------------------------------------------------------------

def safe_int(value, default=None):
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return default


def safe_float(value, default=None):
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return default


def normalize_bool(value):
    """
    Convert common boolean representations into True/False/None.

    Unknown values remain None rather than being interpreted as errors.
    """
    if value is None:
        return None

    v = str(value).strip().lower()

    if v in {"true", "1", "yes", "y", "error", "failed", "failure"}:
        return True

    if v in {"false", "0", "no", "n", "ok", "success"}:
        return False

    return None


def normalize_text(value):
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def percentile(values, p):
    """
    Simple percentile calculation without external dependencies.
    """
    if not values:
        return None

    values = sorted(values)

    if len(values) == 1:
        return values[0]

    index = (len(values) - 1) * p
    lower = math.floor(index)
    upper = math.ceil(index)

    if lower == upper:
        return values[lower]

    weight = index - lower

    return values[lower] * (1 - weight) + values[upper] * weight


def median_absolute_deviation(values):
    if not values:
        return 0.0

    med = median(values)

    deviations = [
        abs(x - med)
        for x in values
    ]

    return median(deviations)


def robust_z_score(value, med, mad):
    """
    Robust z-score using MAD.

    This is less sensitive to extreme outliers than mean/std.
    """
    if mad is None or mad == 0:
        return 0.0

    return 0.6745 * (value - med) / mad


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def load_csv(path):
    """
    Load telemetry and remove only exact duplicate spans.

    Span identity is (trace_id, span_id). Exact duplicates are collapsed.
    Conflicting records sharing a span key are retained and reported rather
    than silently discarded. Original CSV files are never modified.
    """
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise ValueError("CSV has no header.")

        fieldnames = reader.fieldnames
        columns = set(fieldnames)
        missing = REQUIRED_COLUMNS - columns

        if missing:
            raise ValueError(
                "Missing required columns: "
                + ", ".join(sorted(missing))
            )

        rows = []
        for row_number, row in enumerate(reader, start=2):
            normalized = {
                key: normalize_text(value)
                for key, value in row.items()
            }
            normalized["_row_number"] = row_number
            normalized["_duration_numeric"] = safe_float(
                normalized.get("duration")
            )
            normalized["_start_time_numeric"] = safe_int(
                normalized.get("start_time")
            )
            normalized["_has_error_bool"] = normalize_bool(
                normalized.get("has_error")
            )
            rows.append(normalized)

    original_count = len(rows)
    groups = {}
    first_seen_order = []
    missing_key_rows = []

    for row in rows:
        trace_id = row.get("trace_id", "")
        span_id = row.get("span_id", "")

        if not trace_id or not span_id:
            missing_key_rows.append(row)
            continue

        key = (trace_id, span_id)
        if key not in groups:
            groups[key] = []
            first_seen_order.append(key)
        groups[key].append(row)

    unique_rows = []
    duplicate_groups = 0
    exact_duplicate_rows = 0
    conflicting_groups = []

    # Compare original CSV fields only, excluding parser-generated metadata.
    for key in first_seen_order:
        records = groups[key]
        signatures = {
            tuple((field, record.get(field, "")) for field in fieldnames)
            for record in records
        }

        if len(records) > 1:
            duplicate_groups += 1

        if len(signatures) == 1:
            unique_rows.append(records[0])
            exact_duplicate_rows += len(records) - 1
        else:
            # Preserve every conflicting record for investigation.
            unique_rows.extend(records)
            conflicting_groups.append({
                "trace_id": key[0],
                "span_id": key[1],
                "occurrences": len(records),
                "source_rows": [r["_row_number"] for r in records],
            })

    # Keep rows without a complete identity key; never silently discard them.
    unique_rows.extend(missing_key_rows)

    audit = {
        "input_path": str(path),
        "original_row_count": original_count,
        "rows_after_exact_deduplication": len(unique_rows),
        "unique_span_keys": len(groups),
        "duplicate_groups": duplicate_groups,
        "exact_duplicate_rows_removed": exact_duplicate_rows,
        "conflicting_duplicate_groups": conflicting_groups,
        "rows_missing_identity_keys": len(missing_key_rows),
    }

    # Expose the audit to the main pipeline without changing the return shape.
    global DEDUPLICATION_AUDIT
    DEDUPLICATION_AUDIT = audit

    print(
        "Deduplication audit: "
        f"input_rows={original_count}, "
        f"rows_after_dedup={len(unique_rows)}, "
        f"exact_duplicates_removed={exact_duplicate_rows}, "
        f"conflicting_groups={len(conflicting_groups)}, "
        f"rows_missing_keys={len(missing_key_rows)}"
    )

    return fieldnames, unique_rows


# ---------------------------------------------------------------------------
# Basic statistics
# ---------------------------------------------------------------------------

def summarize_numeric(values):
    values = [
        x for x in values
        if x is not None and math.isfinite(x)
    ]

    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "stddev": None,
        }

    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
        "median": median(values),
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "stddev": pstdev(values) if len(values) > 1 else 0.0,
    }


# ---------------------------------------------------------------------------
# Service and operation summaries
# ---------------------------------------------------------------------------

def build_service_summary(rows):

    services = defaultdict(list)

    for row in rows:
        services[row["service_name"]].append(row)

    result = {}

    for service, service_rows in services.items():

        durations = [
            r["_duration_numeric"]
            for r in service_rows
            if r["_duration_numeric"] is not None
        ]

        errors = [
            r for r in service_rows
            if r["_has_error_bool"] is True
        ]

        status_values = Counter(
            r["response"]
            for r in service_rows
            if r["response"]
        )

        operations = Counter(
            r["operation_name"]
            for r in service_rows
            if r["operation_name"]
        )

        result[service] = {
            "span_count": len(service_rows),
            "duration": summarize_numeric(durations),
            "explicit_error_count": len(errors),
            "explicit_error_rate": (
                len(errors) / len(service_rows)
                if service_rows else 0
            ),
            "response_distribution": dict(status_values),
            "operation_distribution": dict(operations),
        }

    return result


def build_operation_summary(rows):

    operations = defaultdict(list)

    for row in rows:

        key = (
            row["service_name"],
            row["operation_name"],
        )

        operations[key].append(row)

    result = []

    for (service, operation), operation_rows in operations.items():

        durations = [
            r["_duration_numeric"]
            for r in operation_rows
            if r["_duration_numeric"] is not None
        ]

        errors = [
            r for r in operation_rows
            if r["_has_error_bool"] is True
        ]

        responses = Counter(
            r["response"]
            for r in operation_rows
            if r["response"]
        )

        result.append({
            "service": service,
            "operation": operation,
            "span_count": len(operation_rows),
            "duration": summarize_numeric(durations),
            "explicit_error_count": len(errors),
            "explicit_error_rate": (
                len(errors) / len(operation_rows)
                if operation_rows else 0
            ),
            "response_distribution": dict(responses),
        })

    result.sort(
        key=lambda x: x["span_count"],
        reverse=True,
    )

    return result[:TOP_OPERATIONS]


# ---------------------------------------------------------------------------
# Dependency graph
# ---------------------------------------------------------------------------

def build_dependency_graph(rows):

    span_index = {
        row["span_id"]: row
        for row in rows
        if row["span_id"]
    }

    edges = Counter()

    edge_examples = defaultdict(list)

    for row in rows:

        parent_id = row["parent_span"]

        if not parent_id:
            continue

        parent = span_index.get(parent_id)

        if parent is None:
            continue

        parent_service = parent["service_name"]
        child_service = row["service_name"]

        if not parent_service or not child_service:
            continue

        # We keep service self-spans out of the service graph.
        if parent_service == child_service:
            continue

        edge = (
            parent_service,
            child_service,
        )

        edges[edge] += 1

        if len(edge_examples[edge]) < MAX_EXAMPLES_PER_CATEGORY:

            edge_examples[edge].append({
                "trace_id": row["trace_id"],
                "parent_span": parent_id,
                "child_span": row["span_id"],
                "parent_service": parent_service,
                "child_service": child_service,
                "parent_operation": parent["operation_name"],
                "child_operation": row["operation_name"],
                "duration": row["_duration_numeric"],
            })

    edge_list = []

    for (parent, child), count in edges.most_common(TOP_EDGES):

        edge_list.append({
            "source": parent,
            "target": child,
            "span_relationship_count": count,
            "examples": edge_examples[(parent, child)],
        })

    services = sorted({
        row["service_name"]
        for row in rows
        if row["service_name"]
    })

    return {
        "services": services,
        "edges": edge_list,
    }


# ---------------------------------------------------------------------------
# Trace summaries
# ---------------------------------------------------------------------------

def build_trace_summary(rows):

    traces = defaultdict(list)

    for row in rows:
        traces[row["trace_id"]].append(row)

    summaries = []

    for trace_id, trace_rows in traces.items():

        durations = [
            r["_duration_numeric"]
            for r in trace_rows
            if r["_duration_numeric"] is not None
        ]

        services = sorted({
            r["service_name"]
            for r in trace_rows
            if r["service_name"]
        })

        operations = sorted({
            r["operation_name"]
            for r in trace_rows
            if r["operation_name"]
        })

        errors = [
            r for r in trace_rows
            if r["_has_error_bool"] is True
        ]

        start_times = [
            r["_start_time_numeric"]
            for r in trace_rows
            if r["_start_time_numeric"] is not None
        ]

        summaries.append({
            "trace_id": trace_id,
            "span_count": len(trace_rows),
            "service_count": len(services),
            "services": services,
            "operation_count": len(operations),
            "explicit_error_count": len(errors),
            "earliest_start": min(start_times)
            if start_times else None,
            "latest_start": max(start_times)
            if start_times else None,
            "duration": summarize_numeric(durations),
        })

    summaries.sort(
        key=lambda x: (
            x["duration"]["max"] or 0
        ),
        reverse=True,
    )

    return summaries


# ---------------------------------------------------------------------------
# Universal evidence extraction
# ---------------------------------------------------------------------------

def _first_non_empty_field(row, candidates):
    """Return the first populated field from a list of candidate names."""
    for field in candidates:
        value = normalize_text(row.get(field))
        if value:
            return field, value
    return None, ""


def _is_unknown_response(value):
    """Return True when a response value explicitly represents missing data."""
    return normalize_text(value).strip().lower() in UNKNOWN_RESPONSE_VALUES


def _looks_like_failure_status(value):
    """
    Conservative status classification.

    This is evidence extraction only. It does not infer root cause.
    Numeric HTTP-like status values >= 400 are treated as failure-like.
    Common textual failure values are also recognized.
    """
    value = normalize_text(value).strip().lower()

    if not value:
        return False

    numeric = safe_int(value)

    if numeric is not None:
        return numeric >= 400

    failure_tokens = (
        "error",
        "failed",
        "failure",
        "timeout",
        "timed_out",
        "cancelled",
        "canceled",
        "unavailable",
        "refused",
        "denied",
        "unauthorized",
        "forbidden",
        "invalid",
        "exception",
    )

    return any(token in value for token in failure_tokens)


def build_telemetry_availability(rows, fieldnames):
    """
    Describe which telemetry fields are actually available.

    This prevents the SLM from interpreting missing telemetry as
    evidence that a failure did not occur.
    """
    total = len(rows)

    availability = {}

    for field in fieldnames:
        populated = sum(
            1
            for row in rows
            if normalize_text(row.get(field))
        )

        availability[field] = {
            "present_in_schema": True,
            "populated_rows": populated,
            "population_rate": (
                round(populated / total, 4)
                if total
                else 0.0
            ),
        }

    return {
        "row_count": total,
        "fields": availability,
    }


def extract_status_error_evidence(rows):
    """
    Extract explicit status/error/message evidence from any telemetry
    columns that happen to be available.

    The parser remains cloud/provider agnostic because these fields are
    optional and discovered dynamically.
    """
    evidence = []

    for row in rows:

        status_field, status_value = _first_non_empty_field(
            row,
            STATUS_FIELD_CANDIDATES,
        )

        error_field, error_value = _first_non_empty_field(
            row,
            ERROR_TEXT_FIELD_CANDIDATES,
        )

        failure_like = _looks_like_failure_status(status_value)

        # The exported trace schema may carry HTTP-like failure evidence
        # in "response" rather than a dedicated status-code column.
        response_value = normalize_text(row.get("response")).strip()
        response_failure_like = _looks_like_failure_status(response_value)

        explicit_error = row["_has_error_bool"] is True
        error_text_present = bool(error_value)

        if not (
            explicit_error
            or failure_like
            or response_failure_like
            or error_text_present
        ):
            continue

        evidence.append({
            "trace_id": row["trace_id"],
            "span_id": row["span_id"],
            "parent_span": row["parent_span"],
            "service": row["service_name"],
            "operation": row["operation_name"],
            "duration": row["_duration_numeric"],
            "start_time": row["_start_time_numeric"],
            "has_error": row["_has_error_bool"],
            "response": row["response"],
            "status_field": status_field,
            "status_value": status_value,
            "error_field": error_field,
            "error_value": error_value,
            "failure_like_status": failure_like,
            "response_failure_like": response_failure_like,
            "explicit_error_flag": explicit_error,
        })

    return evidence


def build_status_summary(rows):
    """
    Aggregate response/status behaviour without interpreting it as RCA.
    """
    response_counts = Counter()
    status_counts = Counter()
    failure_like_counts = Counter()

    status_fields_seen = Counter()

    for row in rows:

        response = normalize_text(row.get("response"))

        if response:
            response_counts[response] += 1

        status_field, status_value = _first_non_empty_field(
            row,
            STATUS_FIELD_CANDIDATES,
        )

        if status_field:
            status_fields_seen[status_field] += 1

        if status_value:
            status_counts[status_value] += 1

            if _looks_like_failure_status(status_value):
                failure_like_counts[status_value] += 1

    return {
        "response_distribution": dict(
            response_counts.most_common(50)
        ),
        "status_distribution": dict(
            status_counts.most_common(50)
        ),
        "failure_like_status_distribution": dict(
            failure_like_counts.most_common(50)
        ),
        "status_fields_observed": dict(status_fields_seen),
    }


def build_temporal_evidence(rows):
    """
    Preserve temporal information when timestamps are available.

    The parser does not assume a particular timestamp unit or provider.
    It reports ordering/range rather than inventing calendar semantics.
    """
    timestamps = [
        row["_start_time_numeric"]
        for row in rows
        if row["_start_time_numeric"] is not None
    ]

    if not timestamps:
        return {
            "available": False,
            "count": 0,
            "earliest": None,
            "latest": None,
            "span": None,
        }

    earliest = min(timestamps)
    latest = max(timestamps)

    return {
        "available": True,
        "count": len(timestamps),
        "earliest": earliest,
        "latest": latest,
        "span": latest - earliest,
    }


def build_universal_evidence_profile(
    rows,
    fieldnames,
    graph,
    service_summary,
    operation_summary,
    error_evidence,
    status_error_evidence,
    latency_anomalies,
):
    """
    Construct the six-category evidence profile used by the SLM context.

    This function organizes observed telemetry. It deliberately does not
    determine the incident ground truth or predict the root cause.
    """
    availability = build_telemetry_availability(
        rows,
        fieldnames,
    )

    temporal = build_temporal_evidence(rows)

    traces = {
        row["trace_id"]
        for row in rows
        if row.get("trace_id")
    }

    return {
        "categories": list(EVIDENCE_CATEGORIES),
        "incident_overview": {
            "spans": len(rows),
            "traces": len(traces),
            "services": len(graph["services"]),
            "operations": len(operation_summary),
        },
        "behavioural_evidence": {
            "services": service_summary,
            "top_operations": operation_summary[:TOP_OPERATIONS],
        },
        "dependency_causal_evidence": {
            "services": graph["services"],
            "edges": graph["edges"],
        },
        "error_status_evidence": {
            "explicit_span_errors": len(error_evidence),
            "status_or_error_records": len(status_error_evidence),
            "status_summary": build_status_summary(rows),
        },
        "performance_anomaly_evidence": {
            "latency_anomalies": latency_anomalies[:TOP_EVIDENCE],
        },
        "telemetry_availability_limitations": {
            "availability": availability,
            "temporal": temporal,
            "limitations": [],
        },
    }


def add_evidence_limitations(evidence_profile):
    """
    Add explicit safeguards against over-interpreting missing telemetry.
    """
    availability = evidence_profile[
        "telemetry_availability_limitations"
    ]["availability"]["fields"]

    limitations = []

    observed_fields = set(availability)

    has_dedicated_status = any(
        field in observed_fields
        for field in STATUS_FIELD_CANDIDATES
    )

    has_response = "response" in observed_fields

    if not has_dedicated_status and not has_response:
        limitations.append(
            "No dedicated status-code or response field was present in the supplied telemetry."
        )
    elif not has_dedicated_status and has_response:
        limitations.append(
            "No dedicated status-code field was present; response values are available as status-related evidence."
        )

    if not any(
        field in observed_fields
        for field in ERROR_TEXT_FIELD_CANDIDATES
    ):
        limitations.append(
            "No dedicated error/exception text field was present in the supplied telemetry."
        )

    if "response" not in observed_fields:
        limitations.append(
            "No response field was available; response-level failure evidence cannot be assessed."
        )
    else:
        response_info = availability.get(
            "response",
            {}
        )

        populated = response_info.get(
            "populated_rows",
            0
        )

        if populated == 0:
            limitations.append(
                "A response field was present in the schema but contained no populated values."
            )

    if "has_error" not in observed_fields:
        limitations.append(
            "No explicit error flag was available; explicit span-error evidence cannot be assessed."
        )

    if "start_time" not in observed_fields:
        limitations.append(
            "No start-time field was available; temporal ordering evidence is limited."
        )

    if not evidence_profile[
        "dependency_causal_evidence"
    ]["edges"]:
        limitations.append(
            "No cross-service dependency edges could be constructed from the supplied parent-child relationships."
        )

    limitations.append(
        "Absence of observed errors is not evidence that the incident contained no failure."
    )

    limitations.append(
        "Latency anomalies identify unusual performance behaviour; they do not establish root cause."
    )

    limitations.append(
        "The parser does not use incident ground truth when constructing evidence."
    )

    evidence_profile[
        "telemetry_availability_limitations"
    ]["limitations"] = limitations

    return evidence_profile


# ---------------------------------------------------------------------------
# Latency anomaly analysis
# ---------------------------------------------------------------------------

def identify_latency_anomalies(rows):

    """
    Detect unusually slow spans relative to the same
    service + operation within the current incident.

    This avoids comparing fundamentally different operations
    such as frontend HTTP requests with database calls.

    This does NOT claim that a slow span is the root cause.
    It only identifies potentially useful latency evidence.
    """

    grouped = defaultdict(list)

    for row in rows:

        duration = row["_duration_numeric"]

        if duration is None:
            continue

        key = (
            row["service_name"],
            row["operation_name"],
        )

        grouped[key].append(duration)

    anomalies = []

    for row in rows:

        duration = row["_duration_numeric"]

        if duration is None:
            continue

        key = (
            row["service_name"],
            row["operation_name"],
        )

        values = grouped.get(key, [])

        if len(values) < 5:
            continue

        med = median(values)
        mad = median_absolute_deviation(values)

        z = robust_z_score(
            duration,
            med,
            mad,
        )

        if z < 3.5:
            continue

        anomalies.append({
            "trace_id": row["trace_id"],
            "span_id": row["span_id"],
            "parent_span": row["parent_span"],
            "service": row["service_name"],
            "operation": row["operation_name"],
            "duration": duration,
            "group_median": round(med, 3),
            "group_mad": round(mad, 3),
            "robust_z_score": round(z, 3),
            "has_error": row["_has_error_bool"],
            "response": row["response"],
            "start_time": row["_start_time_numeric"],
        })

    anomalies.sort(
        key=lambda x: x["robust_z_score"],
        reverse=True,
    )

    return anomalies[:TOP_EVIDENCE]


# ---------------------------------------------------------------------------
# Trace-level causal chains
# ---------------------------------------------------------------------------

def build_trace_causal_chains(rows, latency_anomalies=None):
    """
    Build compact, branch-aware causal chains from trace parent/child
    relationships.

    Unlike a leaf-path representation, this keeps shared prefixes only once
    and represents multiple downstream children as branches. This reduces
    duplicate context while preserving the causal structure needed by an SLM.
    """

    anomaly_lookup = {
        item.get("span_id")
        for item in (latency_anomalies or [])
        if item.get("span_id")
    }

    children_by_parent = defaultdict(list)
    traces = defaultdict(list)

    for row in rows:

        trace_id = row["trace_id"]

        if trace_id:
            traces[trace_id].append(row)

        parent = row["parent_span"]

        if parent:
            children_by_parent[parent].append(row)

    causal_trees = []

    for trace_id, trace_rows in traces.items():

        trace_span_ids = {
            row["span_id"]
            for row in trace_rows
            if row["span_id"]
        }

        roots = [
            row
            for row in trace_rows
            if not row["parent_span"]
            or row["parent_span"] not in trace_span_ids
        ]

        def build_node(row, depth=0):

            span_id = row["span_id"]

            node = {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span": row["parent_span"],
                "service": row["service_name"],
                "operation": row["operation_name"],
                "duration": row["_duration_numeric"],
                "response": row["response"],
                "has_error": row["_has_error_bool"],
                "depth": depth,
                "latency_anomaly": span_id in anomaly_lookup,
                "children": [],
            }

            children = children_by_parent.get(span_id, [])

            for child in children:
                node["children"].append(
                    build_node(child, depth + 1)
                )

            return node

        for root in roots:
            causal_trees.append(build_node(root))

    return causal_trees


# ---------------------------------------------------------------------------
# Explicit error evidence
# ---------------------------------------------------------------------------

def extract_error_evidence(rows):

    errors = []

    for row in rows:

        if row["_has_error_bool"] is True:

            errors.append({
                "trace_id": row["trace_id"],
                "span_id": row["span_id"],
                "parent_span": row["parent_span"],
                "service": row["service_name"],
                "operation": row["operation_name"],
                "duration": row["_duration_numeric"],
                "response": row["response"],
                "start_time": row["_start_time_numeric"],
            })

    return errors


# ---------------------------------------------------------------------------
# Status / response evidence
# ---------------------------------------------------------------------------

def build_response_summary(rows):

    service_responses = defaultdict(Counter)

    for row in rows:

        service = row["service_name"]
        response = row["response"]

        if service and response:
            service_responses[service][response] += 1

    return {
        service: dict(counter)
        for service, counter in service_responses.items()
    }



# ---------------------------------------------------------------------------
# Relevant evidence ranking
# ---------------------------------------------------------------------------

def rank_evidence(rows, latency_anomalies, error_evidence):

    """
    Rank potentially useful telemetry evidence.

    The ranking is evidence prioritization only.
    It is NOT an RCA prediction.

    Priority is given to:
        - explicit errors
        - operation-specific latency anomalies
        - explicit response/status information
        - causal proximity to an anomalous span

    Ordinary parent-child relationships are NOT rewarded by
    themselves because most distributed trace spans have parents.
    """

    anomaly_lookup = {
        x["span_id"]: x
        for x in latency_anomalies
    }

    error_lookup = {
        x["span_id"]
        for x in error_evidence
    }

    anomalous_parents = {
        x["parent_span"]
        for x in latency_anomalies
        if x["parent_span"]
    }

    evidence = []

    for row in rows:

        score = 0.0
        reasons = []

        span_id = row["span_id"]
        parent_span = row["parent_span"]

        if span_id in error_lookup:

            score += 10.0
            reasons.append("explicit_error")

        anomaly = anomaly_lookup.get(span_id)

        if anomaly:

            score += min(
                anomaly["robust_z_score"],
                10.0,
            )

            reasons.append(
                "operation_latency_anomaly"
            )

        if parent_span in anomaly_lookup:

            score += 2.0
            reasons.append(
                "child_of_anomalous_span"
            )

        if span_id in anomalous_parents:

            score += 1.5
            reasons.append(
                "parent_of_anomalous_span"
            )

        response = row["response"].lower()

        if response not in {
            "",
            "unknown",
            "none",
            "null",
        }:

            score += 0.5
            reasons.append(
                "explicit_response"
            )

        if score <= 0:
            continue

        evidence.append({
            "score": round(score, 3),
            "reasons": reasons,
            "trace_id": row["trace_id"],
            "span_id": span_id,
            "parent_span": parent_span,
            "service": row["service_name"],
            "operation": row["operation_name"],
            "duration": row["_duration_numeric"],
            "has_error": row["_has_error_bool"],
            "response": row["response"],
            "start_time": row["_start_time_numeric"],
        })

    evidence.sort(
        key=lambda x: (
            x["score"],
            x["trace_id"],
            x["start_time"] if x["start_time"] is not None else 0,
        ),
        reverse=True,
    )

    return evidence[:TOP_EVIDENCE]


# ---------------------------------------------------------------------------
# Compact SLM context
# ---------------------------------------------------------------------------

def build_slm_context(
    input_file,
    rows,
    graph,
    service_summary,
    operation_summary,
    latency_anomalies,
    error_evidence,
    ranked_evidence,
    trace_causal_chains,
    universal_evidence=None,
    status_error_evidence=None,
):

    lines = []

    lines.append(
        "DISTRIBUTED SYSTEM TELEMETRY RCA CONTEXT"
    )

    lines.append(
        f"Telemetry source: {os.path.basename(input_file)}"
    )

    lines.append(
        f"Total spans: {len(rows)}"
    )

    lines.append(
        f"Unique services: {len(graph['services'])}"
    )

    lines.append("")

    # ------------------------------------------------------------------
    # Dependency graph
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Universal evidence profile
    # ------------------------------------------------------------------

    lines.append(
        "UNIVERSAL EVIDENCE PROFILE"
    )

    if universal_evidence:

        overview = universal_evidence.get(
            "incident_overview",
            {}
        )

        lines.append(
            "- Incident scope: "
            f"spans={overview.get('spans', 0)}, "
            f"traces={overview.get('traces', 0)}, "
            f"services={overview.get('services', 0)}, "
            f"operations={overview.get('operations', 0)}"
        )

        behavioural = universal_evidence.get(
            "behavioural_evidence",
            {}
        )

        top_operations = behavioural.get(
            "top_operations",
            []
        )

        if top_operations:

            lines.append(
                "- Behavioural evidence: highest-volume operations"
            )

            for item in top_operations[:10]:

                lines.append(
                    f"  - {item['service']} | "
                    f"{item['operation']} | "
                    f"count={item['span_count']}"
                )

        dependency = universal_evidence.get(
            "dependency_causal_evidence",
            {}
        )

        edges = dependency.get(
            "edges",
            []
        )

        if edges:

            lines.append(
                "- Dependency evidence:"
            )

            for edge in edges[:20]:

                lines.append(
                    f"  - {edge['source']} -> "
                    f"{edge['target']} | "
                    f"observations="
                    f"{edge.get('observations', edge.get('span_relationship_count', 0))}"
                )

        error_status = universal_evidence.get(
            "error_status_evidence",
            {}
        )

        lines.append(
            "- Error/status evidence: "
            f"explicit_span_errors="
            f"{error_status.get('explicit_span_errors', 0)}, "
            f"status_or_error_records="
            f"{error_status.get('status_or_error_records', 0)}"
        )

        status_summary = error_status.get(
            "status_summary",
            {}
        )

        response_distribution = status_summary.get(
            "response_distribution",
            {}
        )

        if response_distribution:

            lines.append(
                "  - Response distribution:"
            )

            for value, count in list(
                response_distribution.items()
            )[:20]:

                lines.append(
                    f"    - {value}: {count}"
                )

        failure_distribution = status_summary.get(
            "failure_like_status_distribution",
            {}
        )

        if failure_distribution:

            lines.append(
                "  - Failure-like status values observed:"
            )

            for value, count in list(
                failure_distribution.items()
            )[:20]:

                lines.append(
                    f"    - {value}: {count}"
                )


        performance = universal_evidence.get(
            "performance_anomaly_evidence",
            {}
        )

        lines.append(
            "- Performance evidence: "
            f"latency_anomalies="
            f"{len(performance.get('latency_anomalies', []))}"
        )

        # Temporal ordering is evidence only. Preserve first/last observed
        # error times without treating the earliest event as the RCA.
        temporal_groups = defaultdict(list)
        for item in error_evidence or []:
            if item.get("start_time") is not None:
                temporal_groups[(item.get("service", "unknown"), item.get("operation", "unknown"))].append(item["start_time"])
        if temporal_groups:
            lines.append("- Temporal failure evidence:")
            for (service, operation), times in sorted(temporal_groups.items(), key=lambda pair: min(pair[1]))[:20]:
                lines.append(
                    f"  - service={service} | operation={operation} | "
                    f"first_error_seen={min(times)} | last_error_seen={max(times)} | "
                    f"error_observations={len(times)}"
                )
        else:
            lines.append("- Temporal failure evidence unavailable: no usable error timestamps.")

        # Comparative service evidence helps distinguish healthy services,
        # callers, and directly failing emitters without selecting an RCA.
        service_failures = defaultdict(list)
        for item in error_evidence or []:
            service_failures[item.get("service", "unknown")].append(item)
        lines.append("- Service failure comparison:")
        for service in sorted(set(row.get("service_name", "unknown") for row in rows)):
            failures = service_failures.get(service, [])
            operations = sorted(set(item.get("operation", "unknown") for item in failures))
            times = [item.get("start_time") for item in failures if item.get("start_time") is not None]
            lines.append(
                f"  - service={service} | explicit_errors={len(failures)} | "
                f"failed_operations={', '.join(operations) if operations else 'none'} | "
                f"first_error_seen={min(times) if times else 'unavailable'} | "
                f"last_error_seen={max(times) if times else 'unavailable'}"
            )

        limitations = universal_evidence.get(
            "telemetry_availability_limitations",
            {}
        ).get(
            "limitations",
            []
        )

        if limitations:

            lines.append(
                "- Evidence limitations / interpretation safeguards:"
            )

            for limitation in limitations:

                lines.append(
                    f"  - {limitation}"
                )

    else:

        lines.append(
            "- Universal evidence profile unavailable."
        )

    lines.append("")

    # ------------------------------------------------------------------
    # Status / error / message evidence
    # Aggregate repetitive records while retaining useful failure details.

    lines.append("STATUS AND ERROR EVIDENCE")

    if status_error_evidence:
        lines.append(
            f"- Extracted status/error/message records: {len(status_error_evidence)}"
        )

        service_counts = {}
        operation_counts = {}
        response_counts = {}
        error_flag_counts = {}
        detail_counts = {}
        trace_details = {}

        for item in status_error_evidence:
            service = str(item.get("service") or "Unknown")
            operation = str(item.get("operation") or "Unknown")
            trace_id = str(item.get("trace_id") or "Unknown")
            response = str(item.get("response") or "Unknown")
            has_error = item.get("has_error")
            status_field = item.get("status_field")
            status_value = item.get("status_value")
            error_field = item.get("error_field")
            error_value = item.get("error_value")

            service_counts[service] = service_counts.get(service, 0) + 1
            key = (service, operation)
            operation_counts[key] = operation_counts.get(key, 0) + 1
            response_counts[response] = response_counts.get(response, 0) + 1
            flag = str(has_error)
            error_flag_counts[flag] = error_flag_counts.get(flag, 0) + 1

            details = []
            if status_field and status_value not in (None, ""):
                details.append(f"{status_field}={status_value}")
            if response != "Unknown":
                details.append(f"response={response}")
            if error_field and error_value not in (None, ""):
                details.append(f"{error_field}={error_value}")
            if has_error is True:
                details.append("has_error=True")

            detail = " | ".join(details) or "No detailed status/error text available"
            detail_counts[detail] = detail_counts.get(detail, 0) + 1

            if trace_id != "Unknown":
                entry = trace_details.setdefault(
                    trace_id, {"services": set(), "details": set()}
                )
                entry["services"].add(service)
                if details:
                    entry["details"].update(details)

        lines.append("- Records by service:")
        for service, count in sorted(
            service_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )[:10]:
            lines.append(f"    {service}: {count}")

        lines.append("- Records by service and operation:")
        for (service, operation), count in sorted(
            operation_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )[:15]:
            lines.append(f"    {service} | {operation}: {count}")

        lines.append("- Response values in extracted records:")
        for value, count in sorted(
            response_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )[:10]:
            lines.append(f"    {value}: {count}")

        lines.append("- Explicit error flag values:")
        for value, count in sorted(error_flag_counts.items()):
            lines.append(f"    {value}: {count}")

        lines.append("- Repeated status/error details:")
        for detail, count in sorted(
            detail_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )[:10]:
            lines.append(f"    {detail}: {count}")

        if trace_details:
            lines.append("- Representative trace references (up to 5):")
            for trace_id in sorted(trace_details)[:5]:
                entry = trace_details[trace_id]
                services = " -> ".join(sorted(entry["services"]))
                details = "; ".join(sorted(entry["details"])) or (
                    "No detailed status/error text available"
                )
                lines.append(
                    f"    trace={trace_id} | observed_services={services} | "
                    f"evidence={details}"
                )

        lines.append(
            "- This section aggregates extracted records. Consult retained raw "
            "telemetry for individual spans and complete field values."
        )
    else:
        lines.append(
            "- No non-empty status/error/message evidence was detected "
            "in the supplied telemetry fields."
        )

    lines.append("")

    lines.append(
        "SERVICE DEPENDENCY GRAPH"
    )

    if graph["edges"]:

        for edge in graph["edges"]:

            lines.append(
                f"- {edge['source']} -> "
                f"{edge['target']} "
                f"(observed relationships: "
                f"{edge['span_relationship_count']})"
            )

    else:

        lines.append(
            "- No cross-service relationships "
            "were identified from the available "
            "parent-child span relationships."
        )

    lines.append("")

    # ------------------------------------------------------------------
    # Service-level telemetry
    # ------------------------------------------------------------------

    lines.append(
        "SERVICE TELEMETRY SUMMARY"
    )

    for service, summary in sorted(
        service_summary.items(),
        key=lambda x: x[1]["span_count"],
        reverse=True,
    ):

        duration = summary["duration"]

        lines.append(
            f"- {service}: "
            f"spans={summary['span_count']}, "
            f"mean_duration={duration['mean']}, "
            f"p95={duration['p95']}, "
            f"max={duration['max']}, "
            f"explicit_errors="
            f"{summary['explicit_error_count']}"
        )

    lines.append("")

    # ------------------------------------------------------------------
    # Operation-level evidence
    # ------------------------------------------------------------------

    lines.append(
        "TOP OPERATIONS BY TRACE VOLUME"
    )

    for operation in operation_summary[:30]:

        duration = operation["duration"]

        lines.append(
            f"- {operation['service']} | "
            f"{operation['operation']} | "
            f"count={operation['span_count']} | "
            f"mean={duration['mean']} | "
            f"p95={duration['p95']} | "
            f"max={duration['max']} | "
            f"errors="
            f"{operation['explicit_error_count']}"
        )

    lines.append("")

    # ------------------------------------------------------------------
    # Cross-trace causal evidence patterns
    # ------------------------------------------------------------------

    lines.append(
        "CAUSAL EVIDENCE PATTERNS"
    )

    # Aggregate repeated trace spans into compact service-level
    # causal evidence. Complete raw telemetry remains available
    # in raw_traces.json.
    #
    # Paths are reconstructed from parent_span relationships.
    # Consecutive spans belonging to the same service are collapsed.
    # No root-cause conclusion is inferred here.

    span_by_id = {
        row["span_id"]: row
        for row in rows
        if row.get("span_id")
    }

    latency_anomaly_span_ids = {
        item.get("span_id")
        for item in latency_anomalies
        if item.get("span_id")
    }

    causal_paths = {}

    for row in rows:

        current = row
        chain = []
        visited = set()

        while current:

            span_id = current.get("span_id")

            if span_id in visited:
                break

            if span_id:
                visited.add(span_id)

            chain.append(current)

            parent_id = current.get("parent_span")

            if not parent_id or parent_id == "ROOT":
                break

            current = span_by_id.get(parent_id)

            if current is None:
                break

        chain.reverse()

        services = []

        for span in chain:

            service = span.get("service_name")

            if not service:
                continue

            if not services or services[-1] != service:
                services.append(service)

        if len(services) < 2:
            continue

        path = tuple(services)

        entry = causal_paths.setdefault(
            path,
            {
                "span_observations": 0,
                "traces": set(),
                "anomalies": 0,
                "operations": {},
            },
        )

        entry["span_observations"] += 1

        trace_id = row.get("trace_id")

        if trace_id:
            entry["traces"].add(trace_id)

        if row.get("span_id") in latency_anomaly_span_ids:
            entry["anomalies"] += 1

        operation = row.get("operation_name")

        if operation:
            entry["operations"][operation] = (
                entry["operations"].get(operation, 0) + 1
            )

    if causal_paths:

        ranked_paths = sorted(
            causal_paths.items(),
            key=lambda item: (
                len(item[1]["traces"]),
                item[1]["span_observations"],
                item[1]["anomalies"],
            ),
            reverse=True,
        )

        lines.append(
            f"- Unique service-level path patterns: "
            f"{len(ranked_paths)}"
        )

        for index, (path, info) in enumerate(
            ranked_paths[:50],
            start=1,
        ):

            lines.append(
                f"  Path {index}: {' -> '.join(path)}"
            )

            lines.append(
                f"    affected traces: "
                f"{len(info['traces'])}"
            )

            lines.append(
                f"    span observations: "
                f"{info['span_observations']}"
            )

            lines.append(
                f"    latency-anomalous spans: "
                f"{info['anomalies']}"
            )

            top_operations = sorted(
                info["operations"].items(),
                key=lambda item: item[1],
                reverse=True,
            )[:5]

            if top_operations:

                lines.append(
                    "    key operations:"
                )

                for operation, count in top_operations:

                    lines.append(
                        f"      - {operation}: "
                        f"{count} observations"
                    )

    lines.append("")

    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Explicit errors
    # ------------------------------------------------------------------

    lines.append(
        "EXPLICIT ERROR EVIDENCE"
    )

    if error_evidence:

        for item in error_evidence[:30]:

            lines.append(
                f"- service={item['service']} | "
                f"operation={item['operation']} | "
                f"duration={item['duration']} | "
                f"response={item['response']} | "
                f"trace={item['trace_id']} | "
                f"span={item['span_id']}"
            )

    else:

        lines.append(
            "- No spans explicitly marked as errors."
        )

    lines.append("")

    # ------------------------------------------------------------------
    # Latency anomalies
    # ------------------------------------------------------------------

    lines.append(
        "UNUSUAL LATENCY EVIDENCE"
    )

    if latency_anomalies:

        for item in latency_anomalies[:30]:

            lines.append(
                f"- service={item['service']} | "
                f"operation={item['operation']} | "
                f"duration={item['duration']} | "
                f"robust_z={item['robust_z_score']} | "
                f"trace={item['trace_id']}"
            )

    else:

        lines.append(
            "- No statistically unusual latency "
            "was detected using the current threshold."
        )

    lines.append("")

    # ------------------------------------------------------------------
    # Ranked evidence
    # ------------------------------------------------------------------

    lines.append(
        "HIGH-VALUE TELEMETRY EVIDENCE"
    )

    for item in ranked_evidence[:40]:

        lines.append(
            f"- score={item['score']} | "
            f"service={item['service']} | "
            f"operation={item['operation']} | "
            f"duration={item['duration']} | "
            f"response={item['response']} | "
            f"reasons={','.join(item['reasons'])}"
        )

    lines.append("")

    lines.append(
        "IMPORTANT: Evidence ranking identifies "
        "potentially useful telemetry only. "
        "It does not identify the root cause."
    )

    lines.append(
        "IMPORTANT: 'Unknown' response values are "
        "not treated as errors."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process(input_file, output_directory):

    os.makedirs(
        output_directory,
        exist_ok=True,
    )

    columns, rows = load_csv(input_file)

    graph = build_dependency_graph(rows)

    service_summary = build_service_summary(rows)

    operation_summary = build_operation_summary(rows)

    trace_summary = build_trace_summary(rows)

    latency_anomalies = identify_latency_anomalies(rows)

    error_evidence = extract_error_evidence(rows)

    # Optional status/error/message evidence discovered dynamically
    # from the supplied telemetry schema.
    status_error_evidence = extract_status_error_evidence(rows)

    trace_causal_chains = build_trace_causal_chains(
        rows,
        latency_anomalies,
    )

    response_summary = build_response_summary(rows)

    ranked_evidence = rank_evidence(
        rows,
        latency_anomalies,
        error_evidence,
    )

    universal_evidence = build_universal_evidence_profile(
        rows=rows,
        fieldnames=columns,
        graph=graph,
        service_summary=service_summary,
        operation_summary=operation_summary,
        error_evidence=error_evidence,
        status_error_evidence=status_error_evidence,
        latency_anomalies=latency_anomalies,
    )

    universal_evidence = add_evidence_limitations(
        universal_evidence
    )

    slm_context = build_slm_context(
        input_file,
        rows,
        graph,
        service_summary,
        operation_summary,
        latency_anomalies,
        error_evidence,
        ranked_evidence,
        trace_causal_chains,
        universal_evidence,
        status_error_evidence,
    )

    # ---------------------------------------------------------------
    # Preserve raw telemetry
    # ---------------------------------------------------------------

    raw_output = os.path.join(
        output_directory,
        "raw_traces.json",
    )

    clean_rows = []

    for row in rows:

        clean = {
            key: value
            for key, value in row.items()
            if not key.startswith("_")
        }

        clean_rows.append(clean)

    with open(
        raw_output,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            clean_rows,
            f,
            indent=2,
        )

    # ---------------------------------------------------------------
    # Dependency graph
    # ---------------------------------------------------------------

    with open(
        os.path.join(
            output_directory,
            "dependency_graph.json",
        ),
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            graph,
            f,
            indent=2,
        )

    # ---------------------------------------------------------------
    # Structured telemetry summary
    # ---------------------------------------------------------------

    structured = {

        "metadata": {
            "input_file": os.path.basename(
                input_file
            ),
            "span_count": len(rows),
            "trace_count": len({
                r["trace_id"]
                for r in rows
                if r["trace_id"]
            }),
            "service_count": len(
                graph["services"]
            ),
            "columns": columns,
        },

        "services": service_summary,

        "operations": operation_summary,

        "traces": trace_summary,

        "dependency_graph": graph,

        "response_summary": response_summary,

        "latency_anomalies": latency_anomalies,

        "explicit_error_evidence": error_evidence,

        "status_error_evidence": status_error_evidence,

        "ranked_evidence": ranked_evidence,

        "universal_evidence": universal_evidence,

    }

    with open(
        os.path.join(
            output_directory,
            "telemetry_summary.json",
        ),
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            structured,
            f,
            indent=2,
        )

    # ---------------------------------------------------------------
    # SLM context
    # ---------------------------------------------------------------

    with open(
        os.path.join(
            output_directory,
            "slm_context.txt",
        ),
        "w",
        encoding="utf-8",
    ) as f:

        f.write(slm_context)

    # ---------------------------------------------------------------
    # Separate graph-augmented SLM context.
    # Keep slm_context.txt unchanged as the direct-baseline input.
    # ---------------------------------------------------------------

    graph_lines = [
        "GRAPH-AUGMENTED DISTRIBUTED SYSTEM RCA CONTEXT",
        "",
        "This file augments the baseline telemetry context with observed",
        "service dependencies and service-level evidence.",
        "Observed dependencies do not, by themselves, prove root cause.",
        "",
        "OBSERVED SERVICE DEPENDENCY GRAPH",
    ]

    for edge in graph.get("edges", []):
        graph_lines.append(
            f"- {edge['source']} -> {edge['target']} "
            f"| observed_relationships="
            f"{edge['span_relationship_count']}"
        )

    graph_lines.extend([
        "",
        "SERVICE-LEVEL DIAGNOSTIC EVIDENCE",
    ])

    for service_name, service_data in service_summary.items():
        duration = service_data.get("duration", {})
        graph_lines.append(
            f"- service={service_name}"
            f" | spans={service_data.get('span_count', 0)}"
            f" | explicit_errors={service_data.get('explicit_error_count', 0)}"
            f" | median_duration={duration.get('median', 'Unknown')}"
            f" | p95_duration={duration.get('p95', 'Unknown')}"
        )

    graph_lines.extend([
        "",
        "LATENCY ANOMALIES",
    ])

    # Select up to 10 anomalies while limiting repetition from one
    # service-operation pair. Input is already sorted by robust z-score.
    selected_anomalies = []
    selected_ids = set()
    pair_counts = defaultdict(int)

    for item in latency_anomalies:
        pair = (
            item.get("service", "Unknown"),
            item.get("operation", "Unknown"),
        )
        identity = item.get("span_id")

        if pair_counts[pair] >= 2:
            continue

        selected_anomalies.append(item)
        selected_ids.add(identity)
        pair_counts[pair] += 1

        if len(selected_anomalies) == 10:
            break

    # Fill unused slots with the highest-ranked remaining anomalies.
    if len(selected_anomalies) < 10:
        for item in latency_anomalies:
            identity = item.get("span_id")
            if identity in selected_ids:
                continue
            selected_anomalies.append(item)
            selected_ids.add(identity)
            if len(selected_anomalies) == 10:
                break

    for item in selected_anomalies:
        graph_lines.append(
            f"- service={item.get('service', 'Unknown')}"
            f" | operation={item.get('operation', 'Unknown')}"
            f" | duration={item.get('duration')}"
            f" | trace={item.get('trace_id', 'Unknown')}"
            f" | span={item.get('span_id', 'Unknown')}"
        )

    graph_lines.extend([
        "",
        "STATUS AND ERROR EVIDENCE",
    ])

    for item in status_error_evidence:
        graph_lines.append(
            f"- service={item.get('service', 'Unknown')}"
            f" | operation={item.get('operation', 'Unknown')}"
            f" | has_error={item.get('has_error')}"
            f" | response={item.get('response', 'Unknown')}"
            f" | status_field={item.get('status_field')}"
            f" | status_value={item.get('status_value')}"
            f" | trace={item.get('trace_id', 'Unknown')}"
            f" | span={item.get('span_id', 'Unknown')}"
        )

    graph_lines.extend([
        "",
        "INTERPRETATION SAFEGUARDS",
        "- A dependency edge indicates observed communication, not causality.",
        "- Latency anomalies are clues, not proof of root cause.",
        "- Missing explicit error flags do not prove that no failure occurred.",
        "- Diagnose using the evidence provided; do not assume a root cause.",
    ])

    baseline_context_path = os.path.join(
        output_directory,
        "slm_context.txt",
    )

    with open(
        baseline_context_path,
        "r",
        encoding="utf-8",
    ) as baseline_file:
        baseline_text = baseline_file.read()

    graph_addendum = "\n".join(graph_lines) + "\n"

    with open(
        os.path.join(
            output_directory,
            "slm_context_graph_augmented.txt",
        ),
        "w",
        encoding="utf-8",
    ) as f:
        f.write(
            baseline_text.rstrip()
            + "\n\n"
            + graph_addendum
        )

    # ---------------------------------------------------------------
    # Human-readable report
    # ---------------------------------------------------------------

    report = []

    report.append(
        "CLOUD-AGNOSTIC TRACE ANALYSIS"
    )

    report.append("=" * 70)

    report.append(
        f"Input: {input_file}"
    )

    report.append(
        f"Spans: {len(rows)}"
    )

    report.append(
        f"Traces: "
        f"{len({r['trace_id'] for r in rows})}"
    )

    report.append(
        f"Services: {len(graph['services'])}"
    )

    report.append("")

    report.append(
        "Services:"
    )

    for service in graph["services"]:
        report.append(
            f"  - {service}"
        )

    report.append("")

    report.append(
        "Dependency edges:"
    )

    for edge in graph["edges"]:
        report.append(
            f"  - {edge['source']} -> "
            f"{edge['target']} "
            f"({edge['span_relationship_count']})"
        )

    report.append("")

    report.append(
        f"Explicit errors: "
        f"{len(error_evidence)}"
    )

    report.append(
        f"Latency anomalies: "
        f"{len(latency_anomalies)}"
    )

    report.append("")

    report.append(
        "Output files:"
    )

    report.append(
        "  raw_traces.json"
    )

    report.append(
        "  dependency_graph.json"
    )

    report.append(
        "  telemetry_summary.json"
    )

    report.append(
        "  slm_context.txt"
    )

    with open(
        os.path.join(
            output_directory,
            "report.txt",
        ),
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "\n".join(report)
        )

    print("")
    print("=" * 70)
    print("TRACE PARSING COMPLETE")
    print("=" * 70)
    print(f"Input:       {input_file}")
    print(f"Spans:       {len(rows)}")
    print(
        f"Traces:      "
        f"{len({r['trace_id'] for r in rows})}"
    )
    print(
        f"Services:    {len(graph['services'])}"
    )
    print(
        f"Edges:       {len(graph['edges'])}"
    )
    print(
        f"Errors:      {len(error_evidence)}"
    )
    print(
        f"Latency anomalies: "
        f"{len(latency_anomalies)}"
    )
    print("")
    print(
        f"Output directory: "
        f"{output_directory}"
    )
    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():

    if len(sys.argv) != 3:

        print(
            "Usage:\n"
            "  python trace_parser.py "
            "<trace.csv> <output_directory>"
        )

        sys.exit(1)

    input_file = sys.argv[1]
    output_directory = sys.argv[2]

    if not os.path.exists(input_file):

        print(
            f"ERROR: Input file does not exist: "
            f"{input_file}"
        )

        sys.exit(1)

    try:

        process(
            input_file,
            output_directory,
        )

    except Exception as exc:

        print(
            f"ERROR: {type(exc).__name__}: {exc}"
        )

        raise


if __name__ == "__main__":
    main()
