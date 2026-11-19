"""Universal collector for system, kubernetes, log, and metric telemetry.

Operates declaratively via telemetry_manifest.json across all incident categories
to extract objective signals (restarts, errors, endpoints, events, port mismatches,
scaling anomalies) without hardcoding problem-specific ground truths.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

NONE_STR = "<none>"
SECTION_DIVIDER = "=" * 60


class SystemEvidenceCollector:
    """Collects and compacts cluster, pod, log, and metric signals universally using a declarative manifest."""

    def __init__(
        self,
        action_runner: Callable[[str, Any], Any],
        namespace: str,
        manifest_path: str | Path | None = None,
    ):
        """action_runner is typically `orchestrator.session.problem.perform_action`."""
        self.action_runner = action_runner
        self.namespace = namespace

        if manifest_path is None:
            manifest_path = Path(__file__).parent / "telemetry_manifest.json"
        self.manifest_path = Path(manifest_path)
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        """Loads the declarative commandlet manifest from disk with built-in fallbacks."""
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.debug("Failed to load telemetry manifest: %s", e)
        return {}

    def get_command(self, signal_id: str) -> str:
        """Retrieves and interpolates a command for a given signal identifier."""
        signals = self.manifest.get("kubernetes_signals", [])
        for sig in signals:
            if sig.get("id") == signal_id:
                return str(sig.get("command", "")).format(namespace=self.namespace)
        return ""

    def run_safe_action(self, action_name: str, *args: Any) -> Any:
        try:
            return self.action_runner(action_name, *args)
        except (RuntimeError, ValueError, OSError) as e:
            logger.warning("Error executing action %s: %s", action_name, e)
            return f"ERROR executing {action_name}: {e}"

    def run_manifest_command(self, signal_id: str) -> str:
        """Executes a manifest command safely inside the cluster environment."""
        cmd = self.get_command(signal_id)
        if not cmd:
            return ""
        return str(self.run_safe_action("exec_shell", cmd))

    def discover_cluster_services(self) -> list[str]:
        """Discovers all defined services directly from the cluster, including dead or inactive ones."""
        services: set[str] = set()
        svc_out = self.run_manifest_command("discover_services")
        if "ERROR" not in svc_out and svc_out.strip():
            for line in svc_out.strip().splitlines():
                name = line.strip().lower()
                if name:
                    services.add(name)
        return sorted(services)

    def _parse_pods_info(self, pod_out: str) -> list[dict[str, str]]:
        pods_info = []
        if "ERROR" in pod_out or not pod_out.strip():
            return pods_info
        for line in pod_out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                pod_name = parts[0]
                svc_name = re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{4,6}$", "", pod_name)
                svc_name = re.sub(r"-[a-z0-9]{4,6}$", "", svc_name)
                pods_info.append({
                    "name": pod_name,
                    "service": svc_name,
                    "ready": parts[1],
                    "status": parts[2],
                    "restarts": parts[3],
                })
        return pods_info

    def _parse_scaling_anomalies(self, deploy_out: str) -> list[dict[str, str]]:
        scaling_anomalies = []
        if "ERROR" in deploy_out or not deploy_out.strip():
            return scaling_anomalies
        for line in deploy_out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                d_name, desired = parts[0], parts[1]
                avail = parts[2] if len(parts) >= 3 else "0"
                if desired == "0" or (desired != NONE_STR and avail in ("0", NONE_STR)):
                    scaling_anomalies.append({
                        "service": d_name,
                        "desired": desired,
                        "available": avail,
                    })
        return scaling_anomalies

    def _parse_endpoints_info(self, ep_out: str) -> list[dict[str, str]]:
        endpoints_info = []
        if "ERROR" in ep_out or not ep_out.strip():
            return endpoints_info
        for line in ep_out.strip().splitlines():
            parts = line.split()
            if not parts:
                continue
            svc = parts[0]
            has_valid_ep = len(parts) > 1 and parts[1] not in (NONE_STR, "") and ":" in parts[1]
            ep_val = parts[1] if has_valid_ep else NONE_STR
            endpoints_info.append({"service": svc, "endpoints": ep_val})
        return endpoints_info

    def _parse_warning_events(self, ev_out: str) -> list[str]:
        if "ERROR" in ev_out or not ev_out.strip():
            return []
        sig_cfg = next(
            (s for s in self.manifest.get("kubernetes_signals", []) if s.get("id") == "cluster_events"),
            {}
        )
        keywords = sig_cfg.get(
            "filter_keywords",
            ["warn", "fail", "error", "killing", "backoff", "unhealthy", "failedmount"]
        )
        max_events = sig_cfg.get("max_events", 10)
        events = [
            line.strip()
            for line in ev_out.strip().splitlines()
            if any(w in line.lower() for w in keywords)
        ]
        return events[-max_events:]

    def collect_kubernetes_state(self) -> dict[str, Any]:
        """Collects pod statuses, restart counts, deployment replicas, endpoints, and recent events."""
        return {
            "pods": self._parse_pods_info(self.run_manifest_command("pod_health")),
            "scaling_anomalies": self._parse_scaling_anomalies(self.run_manifest_command("deployment_replicas")),
            "endpoints": self._parse_endpoints_info(self.run_manifest_command("service_endpoints")),
            "warning_events": self._parse_warning_events(self.run_manifest_command("cluster_events")),
        }

    def _extract_container_ports(self, pod_out: str) -> dict[str, set[int]]:
        container_ports: dict[str, set[int]] = {}
        if "ERROR" in pod_out or not pod_out.strip():
            return container_ports
        try:
            pod_items = json.loads(pod_out).get("items", [])
            for pod in pod_items:
                app_name = pod.get("metadata", {}).get("labels", {}).get("app", "")
                if not app_name:
                    continue
                ports = container_ports.setdefault(app_name, set())
                for container in pod.get("spec", {}).get("containers", []):
                    for cp in container.get("ports", []):
                        port_num = cp.get("containerPort")
                        if port_num:
                            ports.add(int(port_num))
        except (TypeError, ValueError) as e:
            logger.debug("Failed to extract container ports: %s", e)
        return container_ports

    def collect_service_port_mismatches(self) -> list[str]:
        """Detects Kubernetes Service targetPort vs container port mismatches universally."""
        findings: list[str] = []
        svc_out = self.run_manifest_command("services_spec")
        if "ERROR" in svc_out or not svc_out.strip():
            return findings

        container_ports = self._extract_container_ports(self.run_manifest_command("pods_spec"))

        try:
            svc_items = json.loads(svc_out).get("items", [])
            for svc in svc_items:
                svc_name = svc.get("metadata", {}).get("name", "")
                selector = svc.get("spec", {}).get("selector", {})
                app_label = selector.get("app", svc_name)
                known_ports = container_ports.get(app_label, set())

                for port_spec in svc.get("spec", {}).get("ports", []):
                    try:
                        target_port_int = int(port_spec.get("targetPort"))
                    except (TypeError, ValueError):
                        continue

                    if known_ports and target_port_int not in known_ports:
                        findings.append(
                            f"  - SERVICE PORT MISMATCH: service '{svc_name}' "
                            f"targetPort {target_port_int} not found in pod containerPorts {sorted(known_ports)} "
                            f"[!] ATTENTION: SERVICE MISCONFIGURATION — traffic will be dropped"
                        )
        except (TypeError, ValueError) as e:
            logger.debug("Failed to scan service port mismatches: %s", e)

        return findings

    @staticmethod
    def _parse_operation_target(op: str) -> str | None:
        """Extract downstream target service name from operation string using universal patterns."""
        # 1. Standard gRPC Service convention: e.g. "com.example.BillingService/Process" -> "billing"
        grpc_match = re.search(r"/?(?:[a-zA-Z0-9_]+\.)*([a-zA-Z0-9]+)Service/", op)
        if grpc_match:
            name = grpc_match.group(1)
            return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()
        # 2. Legacy / package gRPC match fallback
        pkg_match = re.match(r"^/([\w-]+)\.[\w-]+/", op)
        if pkg_match:
            return pkg_match.group(1).lower()
        # 3. Standard HTTP/REST route convention
        if op.startswith(("HTTP POST /", "HTTP GET /", "/")):
            route_match = re.search(r"/(?:api(?:/v\d+)?)?/([\w-]+)", op)
            if route_match:
                return route_match.group(1).lower()
        return None

    @staticmethod
    def _format_rpc_summary(rpc_summary: dict[str, dict[str, Any]]) -> list[str]:
        lines = []
        for target, stats in sorted(rpc_summary.items()):
            calls = stats["calls"]
            fails = stats["failures"]
            if fails > 0:
                rate = (fails / calls) * 100 if calls > 0 else 0
                emitters = ", ".join(sorted(stats["emitters"]))
                lines.append(
                    f"  - target service: {target} | calls: {calls} | failures: {fails} ({rate:.1f}%) | "
                    f"called by: [{emitters}] | responses: {sorted(stats['responses'])} [!] ATTENTION: RPC FAILURES DETECTED"
                )
        return lines

    def extract_rpc_targets(self, trace_csv_path: str) -> list[str]:
        """Mechanically extracts downstream RPC target services and their error rates from traces."""
        if not trace_csv_path or not Path(trace_csv_path).exists():
            return []

        rpc_summary: dict[str, dict[str, Any]] = {}
        try:
            with open(trace_csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    op = row.get("operation_name", "").strip()
                    emitter = row.get("service_name", "").strip()
                    has_err = str(row.get("has_error", "")).lower() in ("true", "1")
                    resp = str(row.get("response", "")).strip()

                    target = self._parse_operation_target(op)
                    if not target or target == emitter:
                        continue

                    stats = rpc_summary.setdefault(
                        target,
                        {"calls": 0, "failures": 0, "emitters": set(), "responses": set()}
                    )
                    stats["calls"] += 1
                    stats["emitters"].add(emitter)
                    if has_err or resp.startswith("5") or "error" in resp.lower():
                        stats["failures"] += 1
                        stats["responses"].add(resp)

            return self._format_rpc_summary(rpc_summary)
        except (OSError, csv.Error) as e:
            logger.debug("Failed to extract RPC targets from trace CSV: %s", e)
            return []

    def _fetch_fallback_log(self, svc: str, cmd_tmpl: str) -> str:
        cmd_str = cmd_tmpl.format(service=svc, namespace=self.namespace)
        try:
            result = subprocess.run(
                cmd_str.split(),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return result.stdout or result.stderr
        except (subprocess.SubprocessError, OSError) as e:
            logger.debug("Failed fallback log execution for %s: %s", svc, e)
            return ""

    def collect_service_logs_summary(
        self,
        services: list[str],
        max_lines_per_svc: int | None = None,
    ) -> dict[str, list[str]]:
        """Collects and compacts log error signals universally across all services without terminal spam."""
        log_cfg = self.manifest.get("log_inspection", {})
        error_keywords = log_cfg.get(
            "error_keywords",
            ["error", "exception", "panic", "fatal", "unauthorized", "refused", "timeout", "failed"]
        )
        if max_lines_per_svc is None:
            max_lines_per_svc = log_cfg.get("max_lines_per_service", 4)
        max_len = log_cfg.get("max_line_length", 150)
        fallback_cmd_tmpl = log_cfg.get(
            "fallback_command",
            "kubectl logs -l app={service} -n {namespace} --tail=50 --ignore-errors --prefix"
        )

        error_logs: dict[str, list[str]] = {}

        for svc in services:
            with contextlib.redirect_stdout(io.StringIO()):
                raw_log = str(self.run_safe_action("get_logs", self.namespace, svc))

            is_err_log = (
                not raw_log
                or ("ERROR" in raw_log and "executing" in raw_log)
                or "does not exist" in raw_log
                or "namespace does not exist" in raw_log
            )
            if is_err_log:
                raw_log = self._fetch_fallback_log(svc, fallback_cmd_tmpl)

            if not raw_log or ("ERROR" in raw_log and "executing" in raw_log):
                continue

            matching = [
                line.strip()[:max_len]
                for line in raw_log.splitlines()
                if any(k in line.lower() for k in error_keywords)
            ]
            if matching:
                error_logs[svc] = matching[:max_lines_per_svc]

        return error_logs

    @staticmethod
    def _format_pod_lines(pods: list[dict[str, str]]) -> list[str]:
        lines = []
        has_anomalous_pod = False
        for p in pods:
            if p["name"].startswith("wrk2") or "job" in p["name"].lower() or p.get("status") in ("Completed", "Succeeded"):
                continue
            restarts = p.get("restarts", "0")
            status = p.get("status", "Unknown")
            ready = p.get("ready", "")
            svc = p.get("service", "unknown")
            is_anom = (restarts not in ("0", "") or status not in ("Running", "Completed") or "0/" in ready)
            flag = " [!] ATTENTION: ANOMALOUS SERVICE (CRASH/RESTART)" if is_anom else ""
            lines.append(f"  - service: {svc} (pod: {p['name']}) | status: {status} | ready: {ready} | restarts: {restarts}{flag}")
            if is_anom:
                has_anomalous_pod = True
        if not has_anomalous_pod:
            lines.append("  (All observed application service pods currently Running with 0 restarts)")
        return lines

    @staticmethod
    def _format_scaling_lines(scale_anoms: list[dict[str, str]]) -> list[str]:
        if not scale_anoms:
            return []
        lines = ["WORKLOAD SCALING ANOMALIES (Scale-to-zero or Unschedulable):"]
        for sa in scale_anoms:
            lines.append(
                f"  - service: {sa['service']} | desired replicas: {sa['desired']} | "
                f"available: {sa['available']} [!] ATTENTION: WORKLOAD SCALED TO ZERO"
            )
        lines.append("")
        return lines

    @staticmethod
    def _format_endpoint_lines(eps: list[dict[str, str]]) -> list[str]:
        empty_eps = [e for e in eps if e.get("endpoints") in (NONE_STR, "")]
        if not empty_eps:
            return []
        lines = ["ENDPOINT ANOMALIES (Disconnected or Port Misconfiguration):"]
        for e in empty_eps:
            lines.append(f"  - service: {e['service']} has NO active endpoints (traffic cannot reach pods)")
        lines.append("")
        return lines

    @staticmethod
    def _format_log_highlights(svc_errors: dict[str, list[str]]) -> list[str]:
        if not svc_errors:
            return []
        lines = ["SERVICE LOG ERROR HIGHLIGHTS:"]
        for svc, err_lines in svc_errors.items():
            lines.append(f"  [Service: {svc}] ({len(err_lines)} error lines observed)")
            for el in err_lines:
                lines.append(f"    * {el}")
        lines.append("")
        return lines

    def build_summary_context(self, services: list[str] | None = None, trace_csv_path: str | None = None) -> str:
        """Constructs a compact, factual text block for the SLM context."""
        k8s_data = self.collect_kubernetes_state()
        lines = [
            SECTION_DIVIDER,
            "SYSTEM & CLUSTER OBJECTIVE SIGNALS",
            SECTION_DIVIDER,
        ]

        # 1. Pod Status & Restarts
        pods = k8s_data.get("pods", [])
        if pods:
            lines.append("POD HEALTH & RESTART COUNTS (Mapped to Service Identifiers):")
            lines.extend(self._format_pod_lines(pods))
            lines.append("")

        # 2. Scaling Anomalies & 3. Missing/Empty Endpoints
        lines.extend(self._format_scaling_lines(k8s_data.get("scaling_anomalies", [])))
        lines.extend(self._format_endpoint_lines(k8s_data.get("endpoints", [])))

        # 4. Service Port -> Container Port Mismatch
        port_mismatches = self.collect_service_port_mismatches()
        if port_mismatches:
            lines.append("KUBERNETES SERVICE PORT MISCONFIGURATIONS (targetPort vs container port divergence):")
            lines.extend(port_mismatches)
            lines.append("")

        # 5. Warning Events
        events = k8s_data.get("warning_events", [])
        if events:
            lines.append("RECENT CLUSTER WARNING / FAILURE EVENTS:")
            for ev in events:
                lines.append(f"  - {ev}")
            lines.append("")

        # 6. RPC Remote Failure Targets
        if trace_csv_path:
            rpc_failures = self.extract_rpc_targets(trace_csv_path)
            if rpc_failures:
                lines.append("REMOTE RPC TARGET FAILURE ANALYSIS (Callers failing when invoking target service):")
                lines.extend(rpc_failures)
                lines.append("")

        # 7. Service Log Error Highlights
        if services:
            lines.extend(self._format_log_highlights(self.collect_service_logs_summary(services)))

        lines.append(SECTION_DIVIDER)
        return "\n".join(lines)
