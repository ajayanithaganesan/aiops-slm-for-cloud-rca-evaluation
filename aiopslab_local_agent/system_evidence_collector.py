"""
Universal System Evidence Collector

Queries Kubernetes cluster state and logs via telemetry_manifest.json to collect:
1. Pod health & restart counts
2. Workload scaling anomalies (e.g. deployments scaled to zero)
3. Disconnected service endpoints (<none>)
4. Service port misconfigurations (targetPort vs containerPort divergence)
5. Cluster warning events (CrashLoopBackOff, FailedMount, etc.)
6. Remote RPC failure rates from trace records
7. Service log error snippets

Provides objective, factual signals to the SLM without guessing ground truth.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import csv
import io
import json
import logging
from pathlib import Path
import re
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

NONE_STR = "<none>"
SECTION_DIVIDER = "=" * 60


class SystemEvidenceCollector:
    """Collects and organizes cluster, pod, log, and metric signals using a declarative manifest."""

    def __init__(
        self,
        action_runner: Callable[[str, Any], Any],
        namespace: str,
        manifest_path: str | Path | None = None,
    ):
        self.action_runner = action_runner
        self.namespace = namespace
        self.manifest_path = Path(manifest_path or Path(__file__).parent / "telemetry_manifest.json")
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        """Loads command templates from telemetry_manifest.json on disk."""
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError) as err:
                logger.debug("Failed to load telemetry manifest: %s", err)
        return {}

    def get_command(self, signal_id: str) -> str:
        """Retrieves and formats a manifest command for our active Kubernetes namespace."""
        for sig in self.manifest.get("kubernetes_signals", []):
            if sig.get("id") == signal_id:
                return str(sig.get("command", "")).format(namespace=self.namespace)
        return ""

    def run_safe_action(self, action_name: str, *args: Any) -> Any:
        """Executes an action safely, catching any runtime exceptions so the agent doesn't crash."""
        try:
            return self.action_runner(action_name, *args)
        except (RuntimeError, ValueError, OSError) as err:
            logger.warning("Error executing action %s: %s", action_name, err)
            return f"ERROR executing {action_name}: {err}"

    def run_manifest_command(self, signal_id: str) -> str:
        """Looks up a signal ID from the manifest and runs it via shell."""
        cmd = self.get_command(signal_id)
        return str(self.run_safe_action("exec_shell", cmd)) if cmd else ""

    def discover_cluster_services(self) -> list[str]:
        """Discovers all defined service names directly from the cluster."""
        out = self.run_manifest_command("discover_services")
        if "ERROR" in out or not out.strip():
            return []
        return sorted({line.strip().lower() for line in out.strip().splitlines() if line.strip()})

    
    # Kubernetes Signal Parsers


    def _parse_pods_info(self, pod_out: str) -> list[dict[str, str]]:
        """Parses 'kubectl get pods' output and removes replica hash suffixes to extract clean service names."""
        if "ERROR" in pod_out or not pod_out.strip():
            return []
        pods = []
        for line in pod_out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                # Remove random deployment/pod hashes (e.g. adservice-76b9b5f88b-x7z8p -> adservice)
                svc = re.sub(r"-[a-z0-9]{4,6}$", "", re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{4,6}$", "", parts[0]))
                pods.append({"name": parts[0], "service": svc, "ready": parts[1], "status": parts[2], "restarts": parts[3]})
        return pods

    def _parse_scaling_anomalies(self, deploy_out: str) -> list[dict[str, str]]:
        """Identifies deployments that have zero available replicas or were scaled to zero."""
        if "ERROR" in deploy_out or not deploy_out.strip():
            return []
        anomalies = []
        for line in deploy_out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                desired, avail = parts[1], parts[2] if len(parts) >= 3 else "0"
                if desired == "0" or (desired != NONE_STR and avail in ("0", NONE_STR)):
                    anomalies.append({"service": parts[0], "desired": desired, "available": avail})
        return anomalies

    def _parse_endpoints_info(self, ep_out: str) -> list[dict[str, str]]:
        """Parses service endpoints to detect disconnected services that cannot receive traffic."""
        if "ERROR" in ep_out or not ep_out.strip():
            return []
        endpoints = []
        for line in ep_out.strip().splitlines():
            parts = line.split()
            if parts:
                has_ep = len(parts) > 1 and parts[1] not in (NONE_STR, "") and ":" in parts[1]
                endpoints.append({"service": parts[0], "endpoints": parts[1] if has_ep else NONE_STR})
        return endpoints

    def _parse_warning_events(self, ev_out: str) -> list[str]:
        """Extracts recent warning and failure events from the cluster (CrashLoop, OOMKilled, Backoff)."""
        if "ERROR" in ev_out or not ev_out.strip():
            return []
        sig_cfg = next((s for s in self.manifest.get("kubernetes_signals", []) if s.get("id") == "cluster_events"), {})
        keywords = sig_cfg.get("filter_keywords", ["warn", "fail", "error", "killing", "backoff", "unhealthy", "failedmount"])
        max_ev = sig_cfg.get("max_events", 10)
        events = [line.strip() for line in ev_out.strip().splitlines() if any(kw in line.lower() for kw in keywords)]
        return events[-max_ev:]

    def collect_kubernetes_state(self) -> dict[str, Any]:
        """Gathers all live cluster state signals: pods, deployments, endpoints, and warning events."""
        return {
            "pods": self._parse_pods_info(self.run_manifest_command("pod_health")),
            "scaling_anomalies": self._parse_scaling_anomalies(self.run_manifest_command("deployment_replicas")),
            "endpoints": self._parse_endpoints_info(self.run_manifest_command("service_endpoints")),
            "warning_events": self._parse_warning_events(self.run_manifest_command("cluster_events")),
        }

    # Port Mismatch & RPC Failure Analysis
   

    def _extract_container_ports(self, pod_out: str) -> dict[str, set[int]]:
        """Inspects pod JSON specs to find all open container ports per application label."""
        ports_by_app: dict[str, set[int]] = {}
        if "ERROR" in pod_out or not pod_out.strip():
            return ports_by_app
        try:
            for pod in json.loads(pod_out).get("items", []):
                app = pod.get("metadata", {}).get("labels", {}).get("app", "")
                if app:
                    p_set = ports_by_app.setdefault(app, set())
                    for c in pod.get("spec", {}).get("containers", []):
                        for cp in c.get("ports", []):
                            if cp.get("containerPort"):
                                p_set.add(int(cp["containerPort"]))
        except (TypeError, ValueError) as err:
            logger.debug("Failed to extract container ports: %s", err)
        return ports_by_app

    def collect_service_port_mismatches(self) -> list[str]:
        """Detects if a Service targetPort points to a port number that no pod container is listening on."""
        svc_out = self.run_manifest_command("services_spec")
        if "ERROR" in svc_out or not svc_out.strip():
            return []
        container_ports = self._extract_container_ports(self.run_manifest_command("pods_spec"))
        findings = []
        try:
            for svc in json.loads(svc_out).get("items", []):
                svc_name = svc.get("metadata", {}).get("name", "")
                app_label = svc.get("spec", {}).get("selector", {}).get("app", svc_name)
                known = container_ports.get(app_label, set())
                for port_spec in svc.get("spec", {}).get("ports", []):
                    try:
                        target = int(port_spec.get("targetPort"))
                        if known and target not in known:
                            findings.append(
                                f"  - SERVICE PORT MISMATCH: service '{svc_name}' "
                                f"targetPort {target} not found in pod containerPorts {sorted(known)} "
                                f"[!] ATTENTION: SERVICE MISCONFIGURATION — traffic will be dropped"
                            )
                    except (TypeError, ValueError):
                        continue
        except (TypeError, ValueError) as err:
            logger.debug("Failed to scan service port mismatches: %s", err)
        return findings

    @staticmethod
    def _parse_operation_target(op: str) -> str | None:
        """Extracts downstream destination service name from gRPC or REST operation paths."""
        # 1. Standard gRPC convention: e.g. "CartService/AddItem" -> "cart"
        grpc_match = re.search(r"/?(?:[a-zA-Z0-9_]+\.)*([a-zA-Z0-9]+)Service/", op)
        if grpc_match:
            return re.sub(r"(?<!^)(?=[A-Z])", "-", grpc_match.group(1)).lower()
        # 2. Package-based gRPC fallback
        pkg_match = re.match(r"^/([\w-]+)\.[\w-]+/", op)
        if pkg_match:
            return pkg_match.group(1).lower()
        # 3. HTTP / REST route convention: e.g. "/api/v1/orders" -> "orders"
        if op.startswith(("HTTP POST /", "HTTP GET /", "/")):
            route_match = re.search(r"/(?:api(?:/v\d+)?)?/([\w-]+)", op)
            if route_match:
                return route_match.group(1).lower()
        return None

    @staticmethod
    def _format_rpc_summary(rpc_summary: dict[str, dict[str, Any]]) -> list[str]:
        """Formats aggregated RPC call counts and failure rates into simple text."""
        lines = []
        for target, stats in sorted(rpc_summary.items()):
            calls, fails = stats["calls"], stats["failures"]
            if fails > 0:
                rate = (fails / calls) * 100 if calls > 0 else 0
                emitters = ", ".join(sorted(stats["emitters"]))
                lines.append(
                    f"  - target service: {target} | calls: {calls} | failures: {fails} ({rate:.1f}%) | "
                    f"called by: [{emitters}] | responses: {sorted(stats['responses'])} [!] ATTENTION: RPC FAILURES DETECTED"
                )
        return lines

    def extract_rpc_targets(self, trace_csv_path: str) -> list[str]:
        """Calculates downstream RPC call and failure statistics from trace records."""
        if not trace_csv_path or not Path(trace_csv_path).exists():
            return []
        rpc_summary: dict[str, dict[str, Any]] = {}
        try:
            with open(trace_csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    op, emitter = row.get("operation_name", "").strip(), row.get("service_name", "").strip()
                    has_err = str(row.get("has_error", "")).lower() in ("true", "1")
                    resp = str(row.get("response", "")).strip()
                    target = self._parse_operation_target(op)
                    if not target or target == emitter:
                        continue
                    stats = rpc_summary.setdefault(target, {"calls": 0, "failures": 0, "emitters": set(), "responses": set()})
                    stats["calls"] += 1
                    stats["emitters"].add(emitter)
                    if has_err or resp.startswith("5") or "error" in resp.lower():
                        stats["failures"] += 1
                        stats["responses"].add(resp)
            return self._format_rpc_summary(rpc_summary)
        except (OSError, csv.Error) as err:
            logger.debug("Failed to extract RPC targets from trace CSV: %s", err)
            return []

    # Logs & Summary Construction

    def _fetch_fallback_log(self, svc: str, cmd_tmpl: str) -> str:
        """Runs fallback kubectl command to retrieve logs if the primary get_logs action fails."""
        cmd_str = cmd_tmpl.format(service=svc, namespace=self.namespace)
        try:
            res = subprocess.run(cmd_str.split(), capture_output=True, text=True, timeout=10, check=False)
            return res.stdout or res.stderr
        except (subprocess.SubprocessError, OSError) as err:
            logger.debug("Failed fallback log execution for %s: %s", svc, err)
            return ""

    def collect_service_logs_summary(self, services: list[str], max_lines_per_svc: int | None = None) -> dict[str, list[str]]:
        """Collects error log lines without overflowing the SLM context window."""
        log_cfg = self.manifest.get("log_inspection", {})
        error_keywords = log_cfg.get("error_keywords", ["error", "exception", "panic", "fatal", "unauthorized", "refused", "timeout", "failed"])
        max_lines = max_lines_per_svc or log_cfg.get("max_lines_per_service", 4)
        max_len = log_cfg.get("max_line_length", 150)
        fallback_cmd = log_cfg.get("fallback_command", "kubectl logs -l app={service} -n {namespace} --tail=50 --ignore-errors --prefix")
        error_logs: dict[str, list[str]] = {}

        for svc in services:
            with contextlib.redirect_stdout(io.StringIO()):
                raw = str(self.run_safe_action("get_logs", self.namespace, svc))
            if not raw or ("ERROR" in raw and "executing" in raw) or "does not exist" in raw or "namespace does not exist" in raw:
                raw = self._fetch_fallback_log(svc, fallback_cmd)
            if not raw or ("ERROR" in raw and "executing" in raw):
                continue
            matching = [line.strip()[:max_len] for line in raw.splitlines() if any(k in line.lower() for k in error_keywords)]
            if matching:
                error_logs[svc] = matching[:max_lines]
        return error_logs

    @staticmethod
    def _format_pod_lines(pods: list[dict[str, str]]) -> list[str]:
        """Formats pod health lines, highlighting crashing pods or non-zero restarts."""
        lines = []
        has_anom = False
        for p in pods:
            if p["name"].startswith("wrk2") or "job" in p["name"].lower() or p.get("status") in ("Completed", "Succeeded"):
                continue
            restarts, status, ready, svc = p.get("restarts", "0"), p.get("status", "Unknown"), p.get("ready", ""), p.get("service", "unknown")
            is_anom = restarts not in ("0", "") or status not in ("Running", "Completed") or "0/" in ready
            flag = " [!] ATTENTION: ANOMALOUS SERVICE (CRASH/RESTART)" if is_anom else ""
            lines.append(f"  - service: {svc} (pod: {p['name']}) | status: {status} | ready: {ready} | restarts: {restarts}{flag}")
            if is_anom:
                has_anom = True
        if not has_anom:
            lines.append("  (All observed application service pods currently Running with 0 restarts)")
        return lines

    @staticmethod
    def _format_scaling_lines(scale_anoms: list[dict[str, str]]) -> list[str]:
        """Formats scaling anomalies (e.g. deployments scaled to zero)."""
        if not scale_anoms:
            return []
        lines = ["WORKLOAD SCALING ANOMALIES (Scale-to-zero or Unschedulable):"]
        for sa in scale_anoms:
            lines.append(f"  - service: {sa['service']} | desired replicas: {sa['desired']} | available: {sa['available']} [!] ATTENTION: WORKLOAD SCALED TO ZERO")
        lines.append("")
        return lines

    @staticmethod
    def _format_endpoint_lines(eps: list[dict[str, str]]) -> list[str]:
        """Formats disconnected service endpoints where traffic has no destination pod."""
        empty = [e for e in eps if e.get("endpoints") in (NONE_STR, "")]
        if not empty:
            return []
        lines = ["ENDPOINT ANOMALIES (Disconnected or Port Misconfiguration):"]
        for e in empty:
            lines.append(f"  - service: {e['service']} has NO active endpoints (traffic cannot reach pods)")
        lines.append("")
        return lines

    @staticmethod
    def _format_log_highlights(svc_errors: dict[str, list[str]]) -> list[str]:
        """Formats the top error log lines per service."""
        if not svc_errors:
            return []
        lines = ["SERVICE LOG ERROR HIGHLIGHTS:"]
        for svc, errs in svc_errors.items():
            lines.append(f"  [Service: {svc}] ({len(errs)} error lines observed)")
            for el in errs:
                lines.append(f"    * {el}")
        lines.append("")
        return lines

    def build_summary_context(self, services: list[str] | None = None, trace_csv_path: str | None = None) -> str:
        """Prepares a clean, structured evidence summary to insert directly into the SLM prompt."""
        k8s_data = self.collect_kubernetes_state()
        lines = [SECTION_DIVIDER, "SYSTEM & CLUSTER OBJECTIVE SIGNALS", SECTION_DIVIDER]

        pods = k8s_data.get("pods", [])
        if pods:
            lines.append("POD HEALTH & RESTART COUNTS (Mapped to Service Identifiers):")
            lines.extend(self._format_pod_lines(pods))
            lines.append("")

        lines.extend(self._format_scaling_lines(k8s_data.get("scaling_anomalies", [])))
        lines.extend(self._format_endpoint_lines(k8s_data.get("endpoints", [])))

        port_mismatches = self.collect_service_port_mismatches()
        if port_mismatches:
            lines.append("KUBERNETES SERVICE PORT MISCONFIGURATIONS (targetPort vs container port divergence):")
            lines.extend(port_mismatches)
            lines.append("")

        events = k8s_data.get("warning_events", [])
        if events:
            lines.append("RECENT CLUSTER WARNING / FAILURE EVENTS:")
            for ev in events:
                lines.append(f"  - {ev}")
            lines.append("")

        if trace_csv_path:
            rpc_failures = self.extract_rpc_targets(trace_csv_path)
            if rpc_failures:
                lines.append("REMOTE RPC TARGET FAILURE ANALYSIS (Callers failing when invoking target service):")
                lines.extend(rpc_failures)
                lines.append("")

        if services:
            lines.extend(self._format_log_highlights(self.collect_service_logs_summary(services)))

        lines.append(SECTION_DIVIDER)
        return "\n".join(lines)
