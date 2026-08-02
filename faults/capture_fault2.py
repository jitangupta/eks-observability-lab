#!/usr/bin/env python3
"""Capture Phase 11 fault and recovery evidence for productcatalogservice."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from fault2_oom import (
    CONTAINER,
    DEPLOYMENT,
    FAULT_NAME,
    NAMESPACE,
    PROJECT,
    deployment_resources,
    deployment_strategy,
    get_deployment,
    kubectl,
    kubectl_json,
    load_c1_context,
    summarize_fault_pods,
    utc_now,
    utc_stamp,
    validate_deployment,
    validate_manifest_scope,
    write_json_atomic,
)


DEFAULT_GRAFANA_URL = "https://epicspider2262.grafana.net"
DEFAULT_DASHBOARD_UID = "eks-cross-region-incident-lab"
FAULT_ALERTS = {"Container OOMKilled", "Application container restarted"}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read JSON evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return value


def parse_utc(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"Invalid UTC timestamp in evidence: {value!r}") from exc
    if result.tzinfo is None:
        raise RuntimeError(f"Timestamp is missing a timezone: {value!r}")
    return result.astimezone(timezone.utc)


def read_grafana_service_account_token(path: Path) -> str:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        raise RuntimeError(f"Unable to read Grafana token file {path}") from exc
    for line in lines:
        label, separator, value = line.partition(":")
        if separator and label.strip().lower() == "grafana service account" and value.strip():
            return value.strip()
    if len(lines) == 1 and not any(marker in lines[0] for marker in ("=", ":")):
        return lines[0]
    raise RuntimeError("Grafana token file must contain a 'grafana service account:' line or one raw token")


def grafana_request(base_url: str, token: str, path: str, timeout: int, *, json_result: bool) -> Any:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json,image/png"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Grafana API returned HTTP {exc.code} for {path}") from None
    except Exception as exc:
        raise RuntimeError(f"Grafana request failed for {path}: {type(exc).__name__}") from None
    if json_result:
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Grafana API did not return JSON for {path} ({content_type})") from exc
    if content_type != "image/png" or len(body) < 10_000:
        raise RuntimeError(f"Grafana render was not a usable PNG ({content_type}, {len(body)} bytes)")
    return body


def alert_name(alert: dict[str, Any]) -> str:
    labels = alert.get("labels") or {}
    return str(labels.get("alertname") or labels.get("rulename") or "<unnamed>")


def alert_started_at(alert: dict[str, Any]) -> str | None:
    value = alert.get("startsAt")
    return str(value) if value else None


def capture_kubernetes(scope: dict[str, str], output: Path, label: str) -> tuple[dict[str, Any], dict[str, str]]:
    output.mkdir(parents=True, exist_ok=True)
    resources: dict[str, str] = {}
    objects = {
        "deployment": get_deployment(scope),
        "replicasets": kubectl_json(scope, ["--namespace", NAMESPACE, "get", "replicasets", "--selector", f"app={DEPLOYMENT}"]),
        "pods": kubectl_json(scope, ["--namespace", NAMESPACE, "get", "pods", "--selector", f"app={DEPLOYMENT}"]),
        "events": kubectl_json(scope, ["--namespace", NAMESPACE, "get", "events", "--sort-by=.metadata.creationTimestamp"]),
    }
    for name, value in objects.items():
        path = output / f"{label}-{name}.json"
        write_json_atomic(path, value)
        resources[name] = path.name
    describe = kubectl(
        scope,
        ["--namespace", NAMESPACE, "describe", "deployment", DEPLOYMENT],
        check=False,
    )
    describe_path = output / f"{label}-deployment-describe.txt"
    describe_path.write_text(describe.stdout + (f"\nSTDERR: {describe.stderr}" if describe.stderr else ""), encoding="utf-8")
    resources["describe"] = describe_path.name

    pod_summary = summarize_fault_pods(objects["pods"])
    log_files: list[str] = []
    for pod in pod_summary["pods"]:
        pod_name = pod.get("pod")
        if not pod_name:
            continue
        for previous in (False, True):
            result = kubectl(
                scope,
                [
                    "--namespace", NAMESPACE, "logs", pod_name, "--container", CONTAINER,
                    *( ["--previous"] if previous else [] ),
                    "--timestamps=true", "--tail=500",
                ],
                check=False,
            )
            path = output / f"{label}-{pod_name}-{'previous' if previous else 'current'}.log"
            path.write_text(result.stdout + (f"\nSTDERR: {result.stderr}" if result.stderr else ""), encoding="utf-8")
            log_files.append(path.name)
    resources["logs"] = ",".join(log_files)
    return {"deployment": objects["deployment"], "pods": pod_summary}, resources


def run_verifier(
    repository_root: Path,
    state: str,
    terraform_dir: Path,
    output_dir: Path,
    profile: str | None,
    timeout: int,
) -> tuple[Path, dict[str, Any]]:
    before = set(output_dir.glob(f"verification-{state}-*.json")) if output_dir.exists() else set()
    command = [
        sys.executable,
        str(repository_root / "verification" / "verify.py"),
        "--state", state,
        "--terraform-dir", str(terraform_dir),
        "--output-dir", str(output_dir),
        "--timeout", str(timeout),
    ]
    if profile:
        command.extend(["--profile", profile])
    try:
        result = subprocess.run(command, cwd=repository_root, capture_output=True, text=True, timeout=max(300, timeout * 5), check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Phase 8 verifier timed out for {state}") from exc
    candidates = sorted(set(output_dir.glob(f"verification-{state}-*.json")) - before)
    if len(candidates) != 1:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Verifier did not create exactly one new {state} report: {detail}")
    document = read_json(candidates[0])
    if result.returncode != 0 or document.get("outcome") != "PASS":
        raise RuntimeError(f"Verifier outcome for {state} was {document.get('outcome')!r}; report: {candidates[0]}")
    return candidates[0], document


def latest_passing_verifier(output_dir: Path, state: str) -> tuple[Path, dict[str, Any]] | None:
    for path in sorted(output_dir.glob(f"verification-{state}-*.json"), reverse=True):
        try:
            document = read_json(path)
        except RuntimeError:
            continue
        if document.get("state") == state and document.get("outcome") == "PASS":
            return path, document
    return None


def check_by_id(document: dict[str, Any], check_id: str) -> dict[str, Any]:
    matches = [item for item in document.get("checks", []) if item.get("id") == check_id]
    if len(matches) != 1:
        raise RuntimeError(f"Verification report does not contain exactly one {check_id!r} check")
    return matches[0]


def assert_cart_healthy(verification: dict[str, Any]) -> dict[str, Any]:
    probes = check_by_id(verification, "active-probes")
    outcomes = probes.get("evidence", {}).get("outcomes", {})
    authorized = outcomes.get("authorized", {})
    unauthorized = outcomes.get("unauthorized", {})
    if probes.get("status") != "PASS" or authorized.get("exit_code") != 0 or unauthorized.get("exit_code") == 0:
        raise RuntimeError("Fault 2 verifier did not prove authorized cart success and unauthorized denial")
    return {"detail": probes.get("detail"), "outcomes": outcomes}


def observe_frontend(verification: dict[str, Any], timeout: int) -> dict[str, Any]:
    exposure = check_by_id(verification, "kubernetes-exposure")
    hostname = exposure.get("evidence", {}).get("frontend_ingress_hostname")
    if not hostname:
        raise RuntimeError("Verifier did not report the frontend hostname")
    url = f"http://{hostname}/"
    request = urllib.request.Request(url, headers={"User-Agent": f"{PROJECT}-phase11/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return {"url": url, "status": response.status, "bytes": len(body)}
    except urllib.error.HTTPError as exc:
        return {"url": url, "status": exc.code, "bytes": len(exc.read()), "error": "HTTPError"}
    except Exception as exc:
        return {"url": url, "status": None, "error": type(exc).__name__}


def capture_grafana(
    args: argparse.Namespace,
    bundle: Path,
    started_at: datetime,
    label: str,
) -> tuple[list[dict[str, Any]], Path, Path]:
    token = read_grafana_service_account_token(args.grafana_token_file.resolve())
    alerts = grafana_request(
        args.grafana_url,
        token,
        "/api/alertmanager/grafana/api/v2/alerts",
        args.timeout,
        json_result=True,
    )
    if not isinstance(alerts, list):
        raise RuntimeError("Grafana active-alert response is not a list")
    captured_at = utc_now()
    from_ms = int((started_at - timedelta(minutes=5)).timestamp() * 1000)
    to_ms = int(parse_utc(captured_at).timestamp() * 1000)
    render_path = (
        f"/render/d/{urllib.parse.quote(args.grafana_dashboard_uid)}/phase11-{label}"
        f"?from={from_ms}&to={to_ms}&width=1800&height=1200&tz=UTC&kiosk"
    )
    dashboard = grafana_request(
        args.grafana_url, token, render_path, max(args.timeout, 180), json_result=False
    )
    grafana_dir = bundle / "grafana"
    grafana_dir.mkdir(parents=True, exist_ok=True)
    alerts_path = grafana_dir / f"{label}-active-alerts-{utc_stamp(captured_at)}.json"
    screenshot = grafana_dir / f"{label}-dashboard-{utc_stamp(captured_at)}.png"
    write_json_atomic(alerts_path, {"captured_at": captured_at, "alerts": alerts})
    screenshot.write_bytes(dashboard)
    return alerts, alerts_path, screenshot


def command_fault(args: argparse.Namespace) -> int:
    repository_root = Path(__file__).resolve().parent.parent
    injection_path = args.manifest.resolve()
    injection = read_json(injection_path)
    if injection.get("status") not in ("INJECTED", "INJECTED_EVIDENCE_INCOMPLETE"):
        raise RuntimeError("Fault capture requires an active injection manifest")
    scope = load_c1_context(args.terraform_dir.resolve())
    deployment = get_deployment(scope)
    validate_deployment(deployment)
    validate_manifest_scope(injection, scope, deployment)
    if (
        deployment_resources(deployment) != injection.get("fault_resources")
        or deployment_strategy(deployment) != injection.get("fault_strategy")
    ):
        raise RuntimeError("Journaled Fault 2 resources and rollout strategy are not currently active")
    bundle = injection_path.parent
    captured_at = utc_now()
    investigation_dir = bundle / "investigation"
    kube, files = capture_kubernetes(scope, investigation_dir, f"fault-{utc_stamp(captured_at)}")
    verification_dir = bundle / "verification"
    passing = latest_passing_verifier(verification_dir, "fault2")
    if passing is None:
        verification_path, verification = run_verifier(
            repository_root, "fault2", args.terraform_dir.resolve(), verification_dir, args.profile, args.timeout
        )
    else:
        verification_path, verification = passing
    cart = assert_cart_healthy(verification)
    frontend = observe_frontend(verification, args.timeout)
    alerts, alerts_path, screenshot = capture_grafana(
        args, bundle, parse_utc(str(injection.get("patched_at") or injection["started_at"])), "fault"
    )
    active_names = sorted(alert_name(item) for item in alerts)
    observed_fault_alerts = sorted(FAULT_ALERTS.intersection(active_names))
    pods = kube["pods"]
    missing: list[str] = []
    for key, label in (("oomkilled", "OOMKilled"), ("exit_137", "exit code 137"), ("crash_loop_backoff", "CrashLoopBackOff")):
        if not pods.get(key):
            missing.append(label)
    if int(pods.get("max_restarts", 0)) < 1:
        missing.append("restart count")
    if FAULT_ALERTS - set(active_names):
        missing.append(f"active Grafana alerts {sorted(FAULT_ALERTS - set(active_names))}")
    document = {
        "schema_version": 1,
        "project": PROJECT,
        "phase": 11,
        "fault": FAULT_NAME,
        "state": "fault",
        "captured_at": captured_at,
        "injected_at": injection.get("patched_at"),
        "outcome": "PASS" if not missing else "INCOMPLETE",
        "missing_evidence": missing,
        "configuration_cause": {
            "previous_resources": injection.get("previous_resources"),
            "fault_resources": injection.get("fault_resources"),
            "current_resources": deployment_resources(deployment),
            "previous_strategy": injection.get("previous_strategy"),
            "fault_strategy": injection.get("fault_strategy"),
            "current_strategy": deployment_strategy(deployment),
            "deployment_uid": deployment["metadata"]["uid"],
            "generation": deployment["metadata"].get("generation"),
        },
        "workload": pods,
        "kubernetes_files": files,
        "upstream_effect": frontend,
        "cart_path": cart,
        "verification_report": str(verification_path),
        "grafana": {
            "active_alert_names": active_names,
            "fault_alert_names": observed_fault_alerts,
            "alert_started_at": {alert_name(item): alert_started_at(item) for item in alerts},
            "alerts_file": str(alerts_path),
            "dashboard_file": str(screenshot),
        },
        "root_cause": (
            "An operator-applied 4Mi memory request and limit forced productcatalogservice to be "
            "OOM-killed at startup; it is a direct invalid workload configuration, not a memory leak or network fault."
        ),
    }
    output = investigation_dir / f"investigation-{utc_stamp(captured_at)}.json"
    write_json_atomic(output, document)
    print(f"Fault investigation: {output}")
    print(f"Outcome: {document['outcome']}")
    print(f"OOMKilled={pods['oomkilled']} exit137={pods['exit_137']} CrashLoopBackOff={pods['crash_loop_backoff']} restarts={pods['max_restarts']}")
    print(f"Cart path: {cart['detail']}")
    print(f"Active alerts: {', '.join(active_names) or 'none'}")
    if missing:
        print(f"Missing: {', '.join(missing)}")
    return 0 if not missing else 1


def command_recovery(args: argparse.Namespace) -> int:
    repository_root = Path(__file__).resolve().parent.parent
    injection_path = args.manifest.resolve()
    injection = read_json(injection_path)
    restoration = read_json(args.restoration.resolve())
    if restoration.get("status") not in ("RESTORED", "ALREADY_RESTORED") or restoration.get("outcome") != "PASS":
        raise RuntimeError("Recovery capture requires a passing restoration manifest")
    if Path(restoration.get("injection_manifest", "")).resolve() != injection_path:
        raise RuntimeError("Restoration manifest is not bound to this injection")
    scope = load_c1_context(args.terraform_dir.resolve())
    deployment = get_deployment(scope)
    validate_deployment(deployment)
    validate_manifest_scope(injection, scope, deployment)
    if (
        deployment_resources(deployment) != injection.get("previous_resources")
        or deployment_strategy(deployment) != injection.get("previous_strategy")
    ):
        raise RuntimeError("Deployment does not contain the exact pre-injection resources and rollout strategy")
    replicas = int(deployment.get("spec", {}).get("replicas", 0))
    rollout_ready = (
        deployment.get("metadata", {}).get("generation") == deployment.get("status", {}).get("observedGeneration")
        and int(deployment.get("status", {}).get("availableReplicas", 0)) == replicas
    )
    if not rollout_ready:
        raise RuntimeError("Restored product catalog rollout is not fully available")
    bundle = injection_path.parent
    captured_at = utc_now()
    recovery_dir = bundle / "recovery"
    kube, files = capture_kubernetes(scope, recovery_dir, f"recovery-{utc_stamp(captured_at)}")
    verification_dir = recovery_dir / "verification"
    passing = latest_passing_verifier(verification_dir, "restored")
    if passing is None:
        verification_path, verification = run_verifier(
            repository_root, "restored", args.terraform_dir.resolve(), verification_dir, args.profile, args.timeout
        )
    else:
        verification_path, verification = passing
    cart = assert_cart_healthy(verification)
    frontend = observe_frontend(verification, args.timeout)
    alerts, alerts_path, screenshot = capture_grafana(
        args, bundle, parse_utc(str(injection.get("patched_at") or injection["started_at"])), "recovery"
    )
    active_names = sorted(alert_name(item) for item in alerts)
    lingering = sorted(FAULT_ALERTS.intersection(active_names))
    pods = kube["pods"]
    unhealthy = [item["pod"] for item in pods["pods"] if item.get("ready") is not True or item.get("phase") != "Running"]
    missing: list[str] = []
    if unhealthy:
        missing.append(f"healthy restored pods (unhealthy: {unhealthy})")
    if lingering:
        missing.append(f"resolved Grafana alerts (still active: {lingering})")
    if frontend.get("status") != 200:
        missing.append(f"frontend HTTP 200 (observed {frontend.get('status')})")
    document = {
        "schema_version": 1,
        "project": PROJECT,
        "phase": 11,
        "fault": FAULT_NAME,
        "state": "recovered",
        "captured_at": captured_at,
        "restored_at": restoration.get("completed_at"),
        "outcome": "PASS" if not missing else "INCOMPLETE",
        "missing_evidence": missing,
        "resources": {
            "journaled_previous": injection.get("previous_resources"),
            "current": deployment_resources(deployment),
            "journaled_previous_strategy": injection.get("previous_strategy"),
            "current_strategy": deployment_strategy(deployment),
        },
        "rollout": {
            "ready": rollout_ready,
            "generation": deployment["metadata"].get("generation"),
            "observed_generation": deployment.get("status", {}).get("observedGeneration"),
            "available_replicas": deployment.get("status", {}).get("availableReplicas"),
        },
        "workload": pods,
        "kubernetes_files": files,
        "frontend": frontend,
        "cart_path": cart,
        "verification_report": str(verification_path),
        "grafana": {
            "active_alert_names": active_names,
            "lingering_fault_alert_names": lingering,
            "alerts_file": str(alerts_path),
            "dashboard_file": str(screenshot),
        },
    }
    output = recovery_dir / f"recovery-{utc_stamp(captured_at)}.json"
    write_json_atomic(output, document)
    print(f"Recovery evidence: {output}")
    print(f"Outcome: {document['outcome']}")
    print(f"Rollout ready: {rollout_ready}; frontend status: {frontend.get('status')}")
    print(f"Cart path: {cart['detail']}")
    print(f"Active alerts: {', '.join(active_names) or 'none'}")
    if missing:
        print(f"Missing: {', '.join(missing)}")
    return 0 if not missing else 1


def common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--terraform-dir", type=Path, default=Path("terraform"))
    parser.add_argument("--profile")
    parser.add_argument("--grafana-url", default=DEFAULT_GRAFANA_URL)
    parser.add_argument("--grafana-dashboard-uid", default=DEFAULT_DASHBOARD_UID)
    parser.add_argument("--grafana-token-file", type=Path, default=Path("secrets/grafana-cloud.txt"))
    parser.add_argument("--timeout", type=int, default=120)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fault = subparsers.add_parser("fault", help="Capture direct fault evidence, upstream effects, cart health, and firing alerts")
    common_arguments(fault)
    recovery = subparsers.add_parser("recovery", help="Capture exact resource, rollout, application, cart, and alert recovery")
    common_arguments(recovery)
    recovery.add_argument("--restoration", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "fault":
            return command_fault(args)
        if args.command == "recovery":
            return command_recovery(args)
        raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
