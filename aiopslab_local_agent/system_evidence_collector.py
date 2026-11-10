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
import re
import subprocess
from pathlib import Path
from typing import Any, Callable


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
            except Exception:
                pass
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
        except Exception as e:
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
                name = line.strip()
                if name and not name.startswith("jaeger-out"):
                    services.add(name)
        return sorted(services)

    def collect_kubernetes_state(self) -> dict[str, Any]:
        """Collects pod statuses, restart counts, deployment replicas, endpoints, and recent events."""
        data: dict[str, Any] = {}

        # 1. Pod statuses, ready counts, restarts, and phase
        pod_out = self.run_manifest_command("pod_health")
        pods_info = []
        if "ERROR" not in pod_out and pod_out.strip():
            for line in pod_out.strip().splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    pod_name = parts[0]
                    # Strip Kubernetes deployment and replicaset hash suffixes to get clean service name
                    svc_name = re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{4,6}$", "", pod_name)
                    svc_name = re.sub(r"-[a-z0-9]{4,6}$", "", svc_name)
                    pods_info.append({
                        "name": pod_name,
                        "service": svc_name,
                        "ready": parts[1],
                        "status": parts[2],
                        "restarts": parts[3],
                    })
        data["pods"] = pods_info

        # 2. Deployment replica counts (detects scale_pod_zero or scaled-down workloads)
        deploy_out = self.run_manifest_command("deployment_replicas")
        scaling_anomalies = []
        if "ERROR" not in deploy_out and deploy_out.strip():
            for line in deploy_out.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    d_name = parts[0]
                    desired = parts[1]
                    avail = parts[2] if len(parts) >= 3 else "0"
                    if desired == "0" or (desired != "<none>" and avail in ("0", "<none>")):
                        scaling_anomalies.append({
                            "service": d_name,
                            "desired": desired,
                            "available": avail,
                        })
        data["scaling_anomalies"] = scaling_anomalies

        # 3. Endpoints (critical for scaled-to-zero or disconnected routing)
        ep_out = self.run_manifest_command("service_endpoints")
        endpoints_info = []
        if "ERROR" not in ep_out and ep_out.strip():
            for line in ep_out.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    if len(parts) == 2 or parts[1] in ("<none>", "") or ":" not in parts[1]:
                        endpoints_info.append({"service": parts[0], "endpoints": "<none>"})
                    else:
                        endpoints_info.append({"service": parts[0], "endpoints": parts[1]})
                elif len(parts) == 1:
                    endpoints_info.append({"service": parts[0], "endpoints": "<none>"})
        data["endpoints"] = endpoints_info

        # 4. Cluster Warning / Failure Events
        ev_out = self.run_manifest_command("cluster_events")
        events: list[str] = []
        if "ERROR" not in ev_out and ev_out.strip():
            # Extract keywords and limit from manifest
            sig_cfg = next(
                (s for s in self.manifest.get("kubernetes_signals", []) if s.get("id") == "cluster_events"),
                {}
            )
            keywords = sig_cfg.get(
                "filter_keywords",
                ["warn", "fail", "error", "killing", "backoff", "unhealthy", "failedmount"]
            )
            max_events = sig_cfg.get("max_events", 10)

            for line in ev_out.strip().splitlines():
                lower = line.lower()
                if any(w in lower for w in keywords):
                    events.append(line.strip())
            events = events[-max_events:]
        data["warning_events"] = events

        return data

    def collect_service_port_mismatches(self) -> list[str]:
        """Detects Kubernetes Service targetPort vs container port mismatches universally.

        Cross-references each Service's spec.ports[*].targetPort against the
        containerPorts declared in its matching pods. Any divergence is a
        structural misconfiguration signal regardless of pod health or trace errors.
        """
        findings: list[str] = []

        svc_out = self.run_manifest_command("services_spec")
        if "ERROR" in svc_out or not svc_out.strip():
            return findings

        pod_out = self.run_manifest_command("pods_spec")

        # Build map: app-label -> set of containerPorts actually declared in pods
        container_ports: dict[str, set[int]] = {}
        if "ERROR" not in pod_out and pod_out.strip():
            try:
                pod_items = json.loads(pod_out).get("items", [])
                for pod in pod_items:
                    labels = pod.get("metadata", {}).get("labels", {})
                    app_name = labels.get("app", "")
                    if not app_name:
                        continue
                    if app_name not in container_ports:
                        container_ports[app_name] = set()
                    for container in pod.get("spec", {}).get("containers", []):
                        for cp in container.get("ports", []):
                            port_num = cp.get("containerPort")
                            if port_num:
                                container_ports[app_name].add(int(port_num))
            except Exception:
                pass

        # Scan each service for targetPort vs containerPort divergence
        try:
            svc_items = json.loads(svc_out).get("items", [])
            for svc in svc_items:
                svc_name = svc.get("metadata", {}).get("name", "")
                selector = svc.get("spec", {}).get("selector", {})
                app_label = selector.get("app", svc_name)
                known_ports = container_ports.get(app_label, set())

                for port_spec in svc.get("spec", {}).get("ports", []):
                    target_port = port_spec.get("targetPort")
                    try:
                        target_port_int = int(target_port)
                    except (TypeError, ValueError):
                        continue

                    if known_ports and target_port_int not in known_ports:
                        findings.append(
                            f"  - SERVICE PORT MISMATCH: service '{svc_name}' "
                            f"targetPort {target_port_int} not found in pod containerPorts {sorted(known_ports)} "
                            f"[!] ATTENTION: SERVICE MISCONFIGURATION — traffic will be dropped"
                        )
        except Exception:
            pass

        return findings

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

                    target = None
                    rpc_match = re.match(r"^/([a-zA-Z0-9_-]+)\.[a-zA-Z0-9_-]+/", op)
                    if rpc_match:
                        target = rpc_match.group(1).lower()
                    elif op.startswith("HTTP POST /") or op.startswith("HTTP GET /"):
                        route_match = re.match(r"^HTTP (?:POST|GET) /([a-zA-Z0-9_-]+)", op)
                        if route_match:
                            target = route_match.group(1).lower()

                    if target and target != emitter:
                        if target not in rpc_summary:
                            rpc_summary[target] = {"calls": 0, "failures": 0, "emitters": set(), "responses": set()}
                        rpc_summary[target]["calls"] += 1
                        rpc_summary[target]["emitters"].add(emitter)
                        if has_err or resp.startswith("5") or "error" in resp.lower():
                            rpc_summary[target]["failures"] += 1
                            rpc_summary[target]["responses"].add(resp)

            lines = []
            for target, stats in sorted(rpc_summary.items()):
                calls = stats["calls"]
                fails = stats["failures"]
                rate = (fails / calls) * 100 if calls > 0 else 0
                emitters = ", ".join(sorted(stats["emitters"]))
                if fails > 0:
                    lines.append(
                        f"  - target service: {target} | calls: {calls} | failures: {fails} ({rate:.1f}%) | "
                        f"called by: [{emitters}] | responses: {sorted(stats['responses'])} [!] ATTENTION: RPC FAILURES DETECTED"
                    )
            return lines
        except Exception:
            return []

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
            # Silence internal AIOpsLab print(logs) to keep terminal output clean
            with contextlib.redirect_stdout(io.StringIO()):
                raw_log = str(self.run_safe_action("get_logs", self.namespace, svc))

            # Fallback: if AIOpsLab get_logs fails, use declarative fallback command
            if (
                not raw_log
                or ("ERROR" in raw_log and "executing" in raw_log)
                or "does not exist" in raw_log
                or "namespace does not exist" in raw_log
            ):
                cmd_str = fallback_cmd_tmpl.format(service=svc, namespace=self.namespace)
                try:
                    cmd_parts = cmd_str.split()
                    result = subprocess.run(
                        cmd_parts,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    raw_log = result.stdout or result.stderr
                except Exception:
                    continue

            if not raw_log or ("ERROR" in raw_log and "executing" in raw_log):
                continue

            matching_lines = []
            for line in raw_log.splitlines():
                line_lower = line.lower()
                if any(k in line_lower for k in error_keywords):
                    matching_lines.append(line.strip()[:max_len])
            if matching_lines:
                error_logs[svc] = matching_lines[:max_lines_per_svc]

        return error_logs

    def build_summary_context(self, services: list[str] | None = None, trace_csv_path: str | None = None) -> str:
        """Constructs a compact, factual text block for the SLM context."""
        k8s_data = self.collect_kubernetes_state()
        lines = [
            "============================================================",
            "SYSTEM & CLUSTER OBJECTIVE SIGNALS",
            "============================================================",
        ]

        # 1. Pod Status & Restarts
        pods = k8s_data.get("pods", [])
        if pods:
            lines.append("POD HEALTH & RESTART COUNTS (Mapped to Service Identifiers):")
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
            lines.append("")

        # 2. Scaling Anomalies (Scale pod to zero)
        scale_anoms = k8s_data.get("scaling_anomalies", [])
        if scale_anoms:
            lines.append("WORKLOAD SCALING ANOMALIES (Scale-to-zero or Unschedulable):")
            for sa in scale_anoms:
                lines.append(f"  - service: {sa['service']} | desired replicas: {sa['desired']} | available: {sa['available']} [!] ATTENTION: WORKLOAD SCALED TO ZERO")
            lines.append("")

        # 3. Missing/Empty Endpoints
        eps = k8s_data.get("endpoints", [])
        empty_eps = [e for e in eps if e.get("endpoints") in ("<none>", "")]
        if empty_eps:
            lines.append("ENDPOINT ANOMALIES (Disconnected or Port Misconfiguration):")
            for e in empty_eps:
                lines.append(f"  - service: {e['service']} has NO active endpoints (traffic cannot reach pods)")
            lines.append("")

        # 4. Service Port -> Container Port Mismatch (Catches targetPort misconfigs)
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

        # 6. RPC Remote Failure Targets (Causal Downstream Target Extraction)
        if trace_csv_path:
            rpc_failures = self.extract_rpc_targets(trace_csv_path)
            if rpc_failures:
                lines.append("REMOTE RPC TARGET FAILURE ANALYSIS (Callers failing when invoking target service):")
                lines.extend(rpc_failures)
                lines.append("")

        # 7. Service Log Error Highlights (Uniform across all cluster services)
        if services:
            svc_errors = self.collect_service_logs_summary(services)
            if svc_errors:
                lines.append("SERVICE LOG ERROR HIGHLIGHTS:")
                for svc, err_lines in svc_errors.items():
                    lines.append(f"  [Service: {svc}] ({len(err_lines)} error lines observed)")
                    for el in err_lines:
                        lines.append(f"    * {el}")
                lines.append("")

        lines.append("============================================================")
        return "\n".join(lines)
