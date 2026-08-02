#!/usr/bin/env python3
"""Safely preflight, inject, and restore the Phase 11 product catalog OOM fault."""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence


PROJECT = "eks-observability-lab"
FAULT_NAME = "fault2-productcatalog-oom"
NAMESPACE = "online-boutique"
DEPLOYMENT = "productcatalogservice"
CONTAINER = "server"
FAULT_MEMORY = "4Mi"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_stamp(value: str) -> str:
    return value.replace("-", "").replace(":", "")


def write_json_atomic(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_command(
    arguments: Sequence[str | Path], *, timeout: int = 120, check: bool = True
) -> subprocess.CompletedProcess[str]:
    rendered = [str(item) for item in arguments]
    try:
        result = subprocess.run(rendered, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command is not installed or not on PATH: {rendered[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(rendered)}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no command output"
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(rendered)}\n{detail}")
    return result


def load_c1_context(terraform_dir: Path) -> dict[str, str]:
    result = run_command(["terraform", f"-chdir={terraform_dir}", "output", "-json"], timeout=60)
    try:
        raw = json.loads(result.stdout)
        values = {name: item["value"] for name, item in raw.items()}
        c1 = values["clusters"]["c1"]
        scope = {
            "account_id": str(values["account_id"]),
            "context": str(c1["name"]),
            "cluster_name": str(c1["name"]),
            "region": str(c1["region"]),
            "namespace": NAMESPACE,
            "deployment": DEPLOYMENT,
            "container": CONTAINER,
            "fault_memory": FAULT_MEMORY,
        }
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Terraform outputs do not match the Phase 11 C1 scope contract") from exc
    if not scope["account_id"].isdigit() or len(scope["account_id"]) != 12:
        raise RuntimeError("Terraform returned an invalid AWS account ID")
    if not scope["context"] or not scope["region"]:
        raise RuntimeError("Terraform returned an incomplete C1 cluster scope")
    return scope


def kubectl(scope: dict[str, str], arguments: Sequence[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_command(["kubectl", "--context", scope["context"], *arguments], timeout=timeout, check=check)


def kubectl_json(scope: dict[str, str], arguments: Sequence[str], *, timeout: int = 120) -> dict[str, Any]:
    result = kubectl(scope, [*arguments, "-o", "json"], timeout=timeout)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"kubectl returned invalid JSON for {' '.join(arguments)}") from exc


def get_deployment(scope: dict[str, str]) -> dict[str, Any]:
    return kubectl_json(scope, ["--namespace", NAMESPACE, "get", "deployment", DEPLOYMENT])


def container_index(deployment: dict[str, Any], name: str = CONTAINER) -> int:
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    matches = [index for index, item in enumerate(containers) if item.get("name") == name]
    if len(matches) != 1:
        raise RuntimeError(f"Deployment must contain exactly one {name!r} container; found {len(matches)}")
    return matches[0]


def deployment_resources(deployment: dict[str, Any]) -> dict[str, Any]:
    index = container_index(deployment)
    resources = deployment["spec"]["template"]["spec"]["containers"][index].get("resources")
    if not isinstance(resources, dict):
        raise RuntimeError("Product catalog container does not have a resources object to journal")
    return copy.deepcopy(resources)


def deployment_strategy(deployment: dict[str, Any]) -> dict[str, Any]:
    strategy = deployment.get("spec", {}).get("strategy")
    if not isinstance(strategy, dict) or not strategy.get("type"):
        raise RuntimeError("Product catalog Deployment does not have an explicit rollout strategy")
    return copy.deepcopy(strategy)


def memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([EPTGMK]i|[eE]|[EPTGMkK]|m|)?", value)
    if not match:
        raise ValueError(f"Unsupported Kubernetes memory quantity: {value!r}")
    try:
        number = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid Kubernetes memory quantity: {value!r}") from exc
    suffix = match.group(2) or ""
    binary = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
    decimal = {"K": 10**3, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18, "m": Decimal("0.001")}
    multiplier = binary.get(suffix, decimal.get(suffix, 1))
    return int(number * multiplier)


def fault_resources(previous: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(previous)
    requests = result.setdefault("requests", {})
    limits = result.setdefault("limits", {})
    if not isinstance(requests, dict) or not isinstance(limits, dict):
        raise RuntimeError("Product catalog requests and limits must be mappings")
    previous_request = requests.get("memory")
    previous_limit = limits.get("memory")
    if not isinstance(previous_request, str) or not isinstance(previous_limit, str):
        raise RuntimeError("Product catalog must have explicit memory request and limit values")
    fault_bytes = memory_bytes(FAULT_MEMORY)
    if memory_bytes(previous_request) <= fault_bytes or memory_bytes(previous_limit) <= fault_bytes:
        raise RuntimeError(f"Current memory configuration is already at or below the locked {FAULT_MEMORY} fault value")
    requests["memory"] = FAULT_MEMORY
    limits["memory"] = FAULT_MEMORY
    return result


def validate_deployment(deployment: dict[str, Any]) -> None:
    metadata = deployment.get("metadata", {})
    if metadata.get("namespace") != NAMESPACE or metadata.get("name") != DEPLOYMENT:
        raise RuntimeError("kubectl returned a deployment outside the locked Phase 11 scope")
    if not metadata.get("uid") or not metadata.get("resourceVersion"):
        raise RuntimeError("Deployment is missing UID or resourceVersion")
    labels = deployment.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
    if labels.get("app") != DEPLOYMENT:
        raise RuntimeError("Deployment Pod template lacks the expected app=productcatalogservice label")
    if int(deployment.get("spec", {}).get("replicas", 0)) != 1:
        raise RuntimeError("Phase 11 expects exactly one productcatalogservice replica")
    deployment_resources(deployment)
    deployment_strategy(deployment)


def fault_strategy(previous: dict[str, Any]) -> dict[str, Any]:
    if previous.get("type") != "RollingUpdate":
        raise RuntimeError(f"Phase 11 expects a RollingUpdate baseline, found {previous.get('type')!r}")
    return {"type": "Recreate"}


def workload_patch(
    deployment: dict[str, Any], replacement_resources: dict[str, Any], replacement_strategy: dict[str, Any]
) -> list[dict[str, Any]]:
    index = container_index(deployment)
    return [
        {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
        {
            "op": "test",
            "path": f"/spec/template/spec/containers/{index}/resources",
            "value": deployment_resources(deployment),
        },
        {
            "op": "replace",
            "path": f"/spec/template/spec/containers/{index}/resources",
            "value": replacement_resources,
        },
        {"op": "test", "path": "/spec/strategy", "value": deployment_strategy(deployment)},
        {"op": "replace", "path": "/spec/strategy", "value": replacement_strategy},
    ]


def apply_workload(
    scope: dict[str, str],
    deployment: dict[str, Any],
    replacement_resources: dict[str, Any],
    replacement_strategy: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    arguments = [
        "--namespace", NAMESPACE,
        "patch", "deployment", DEPLOYMENT,
        "--type=json",
        "--patch", json.dumps(
            workload_patch(deployment, replacement_resources, replacement_strategy), separators=(",", ":")
        ),
    ]
    if dry_run:
        arguments.append("--dry-run=server")
    return kubectl_json(scope, arguments)


def scope_document(scope: dict[str, str], deployment: dict[str, Any]) -> dict[str, str]:
    return {**scope, "deployment_uid": str(deployment["metadata"]["uid"])}


def validate_manifest_scope(document: dict[str, Any], scope: dict[str, str], deployment: dict[str, Any]) -> None:
    if document.get("project") != PROJECT or document.get("fault") != FAULT_NAME or document.get("action") != "inject":
        raise RuntimeError("Manifest does not belong to this project's Phase 11 injection tool")
    recorded = document.get("scope")
    current = scope_document(scope, deployment)
    if not isinstance(recorded, dict):
        raise RuntimeError("Injection manifest is missing its scope")
    mismatches = [key for key, value in current.items() if recorded.get(key) != value]
    if mismatches:
        raise RuntimeError(f"Current C1 deployment scope differs from the injection manifest: {mismatches}")


def common_document(action: str, scope: dict[str, str], deployment: dict[str, Any], started_at: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": PROJECT,
        "phase": 11,
        "fault": FAULT_NAME,
        "action": action,
        "started_at": started_at,
        "scope": scope_document(scope, deployment),
    }


def unique_bundle(root: Path, prefix: str, started_at: str) -> Path:
    base = root / f"{prefix}-{utc_stamp(started_at)}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = Path(f"{base}-{suffix}")
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def auth_can_patch(scope: dict[str, str]) -> str:
    result = kubectl(
        scope,
        ["auth", "can-i", "patch", "deployments.apps", "--namespace", NAMESPACE],
        timeout=60,
    )
    answer = result.stdout.strip().lower()
    if answer != "yes":
        raise RuntimeError(f"Kubernetes authorization denied deployment patch: {answer or 'empty response'}")
    return answer


def summarize_fault_pods(document: dict[str, Any]) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    for pod in document.get("items", []):
        metadata = pod.get("metadata", {})
        status = pod.get("status", {})
        spec_containers = status.get("containerStatuses", [])
        server = next((item for item in spec_containers if item.get("name") == CONTAINER), {})
        waiting = (server.get("state", {}).get("waiting") or {})
        terminated = (server.get("lastState", {}).get("terminated") or {})
        summaries.append(
            {
                "pod": metadata.get("name"),
                "created_at": metadata.get("creationTimestamp"),
                "phase": status.get("phase"),
                "ready": server.get("ready"),
                "restart_count": int(server.get("restartCount", 0)),
                "waiting_reason": waiting.get("reason"),
                "termination_reason": terminated.get("reason"),
                "exit_code": terminated.get("exitCode"),
                "started_at": terminated.get("startedAt"),
                "finished_at": terminated.get("finishedAt"),
            }
        )
    return {
        "pods": summaries,
        "oomkilled": any(item["termination_reason"] == "OOMKilled" for item in summaries),
        "exit_137": any(item["exit_code"] == 137 for item in summaries),
        "crash_loop_backoff": any(item["waiting_reason"] == "CrashLoopBackOff" for item in summaries),
        "max_restarts": max((item["restart_count"] for item in summaries), default=0),
    }


def capture_immediate_evidence(scope: dict[str, str], bundle: Path, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = kubectl_json(
            scope,
            ["--namespace", NAMESPACE, "get", "pods", "--selector", f"app={DEPLOYMENT}"],
            timeout=min(60, timeout),
        )
        summary = summarize_fault_pods(latest)
        if summary["oomkilled"] and summary["exit_137"] and summary["crash_loop_backoff"] and summary["max_restarts"] >= 1:
            break
        time.sleep(3)

    write_json_atomic(bundle / "fault-pods.json", latest)
    events = kubectl_json(
        scope,
        ["--namespace", NAMESPACE, "get", "events", "--sort-by=.metadata.creationTimestamp"],
    )
    write_json_atomic(bundle / "fault-events.json", events)
    deployment = get_deployment(scope)
    write_json_atomic(bundle / "fault-deployment.json", deployment)

    logs: list[dict[str, Any]] = []
    for pod in summary.get("pods", []):
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
            filename = f"{pod_name}-{'previous' if previous else 'current'}.log"
            (bundle / filename).write_text(result.stdout + (f"\nSTDERR: {result.stderr}" if result.stderr else ""), encoding="utf-8")
            logs.append({"pod": pod_name, "previous": previous, "exit_code": result.returncode, "file": filename})
    summary["files"] = {
        "pods": "fault-pods.json",
        "events": "fault-events.json",
        "deployment": "fault-deployment.json",
        "logs": logs,
    }
    return summary


def restore_command(script: Path, manifest: Path, terraform_dir: Path) -> str:
    terraform_arg = "" if str(terraform_dir) in ("terraform", ".\\terraform") else f' --terraform-dir "{terraform_dir}"'
    return f'"{sys.executable}" "{script}"{terraform_arg} restore --manifest "{manifest}" --execute'


def restoration_stage(
    current_resources: dict[str, Any],
    current_strategy: dict[str, Any],
    previous_resources: dict[str, Any],
    previous_strategy: dict[str, Any],
    injected_resources: dict[str, Any],
    injected_strategy: dict[str, Any],
) -> str:
    if current_resources == previous_resources and current_strategy == previous_strategy:
        return "complete"
    if current_resources == previous_resources and current_strategy == injected_strategy:
        return "strategy"
    if current_resources == injected_resources and current_strategy in (injected_strategy, previous_strategy):
        return "resources"
    raise RuntimeError(
        "Current resources/strategy match neither a journaled restoration stage nor the baseline; refusing to overwrite drift"
    )


def command_preflight(args: argparse.Namespace, script: Path) -> int:
    started_at = utc_now()
    scope = load_c1_context(args.terraform_dir.resolve())
    deployment = get_deployment(scope)
    validate_deployment(deployment)
    previous = deployment_resources(deployment)
    injected = fault_resources(previous)
    previous_strategy = deployment_strategy(deployment)
    injected_strategy = fault_strategy(previous_strategy)
    if previous == injected:
        raise RuntimeError("Fault 2 is already active")
    authorization = auth_can_patch(scope)
    preview = apply_workload(scope, deployment, injected, injected_strategy, dry_run=True)
    if deployment_resources(preview) != injected or deployment_strategy(preview) != injected_strategy:
        raise RuntimeError("Server-side dry-run did not return the exact locked fault resources and strategy")
    bundle = unique_bundle(args.output_root.resolve(), "preflight", started_at)
    document = common_document("preflight", scope, deployment, started_at)
    document.update(
        {
            "completed_at": utc_now(),
            "status": "REHEARSAL",
            "outcome": "PASS",
            "live_fault_injected": False,
            "authorization": {"patch_deployments": authorization},
            "previous_resources": previous,
            "fault_resources": injected,
            "previous_strategy": previous_strategy,
            "fault_strategy": injected_strategy,
            "server_dry_run_generation": preview.get("metadata", {}).get("generation"),
        }
    )
    manifest = bundle / "manifest.json"
    write_json_atomic(manifest, document)
    print(f"UTC: {document['completed_at']}")
    print(f"C1 context: {scope['context']}")
    print(f"Target: deployment/{DEPLOYMENT}, container/{CONTAINER}")
    print(f"Memory: {previous['requests']['memory']}/{previous['limits']['memory']} -> {FAULT_MEMORY}/{FAULT_MEMORY}")
    print("Preflight: PASS (server dry-run only; no live rollout was created)")
    print("Live fault injected: false")
    print(f"Evidence: {manifest}")
    print(f'Injection command: "{sys.executable}" "{script}" inject --execute')
    return 0


def find_active_injection(output_root: Path, scope: dict[str, str], deployment: dict[str, Any]) -> Path | None:
    current = deployment_resources(deployment)
    current_strategy = deployment_strategy(deployment)
    for path in sorted(output_root.glob("fault2-*/injection.json"), reverse=True):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            validate_manifest_scope(document, scope, deployment)
            if (
                document.get("fault_resources") == current
                and document.get("fault_strategy") == current_strategy
                and document.get("status") in (
                "INJECTED", "INJECTED_EVIDENCE_INCOMPLETE"
                )
            ):
                return path
        except (OSError, json.JSONDecodeError, RuntimeError):
            continue
    return None


def command_inject(args: argparse.Namespace, script: Path) -> int:
    if not args.execute:
        raise RuntimeError("Refusing live fault injection without --execute; run preflight first")
    started_at = utc_now()
    scope = load_c1_context(args.terraform_dir.resolve())
    deployment = get_deployment(scope)
    validate_deployment(deployment)
    previous = deployment_resources(deployment)
    previous_strategy = deployment_strategy(deployment)
    try:
        injected = fault_resources(previous)
    except RuntimeError:
        existing = find_active_injection(args.output_root.resolve(), scope, deployment)
        if existing:
            print("The exact journaled Fault 2 configuration is already active; no mutation was performed.")
            print(f"Restore with: {restore_command(script, existing, args.terraform_dir)}")
            return 0
        raise
    injected_strategy = fault_strategy(previous_strategy)
    auth_can_patch(scope)
    preview = apply_workload(scope, deployment, injected, injected_strategy, dry_run=True)
    if deployment_resources(preview) != injected or deployment_strategy(preview) != injected_strategy:
        raise RuntimeError("Server-side dry-run did not return the intended resources and rollout strategy")

    bundle = unique_bundle(args.output_root.resolve(), "fault2", started_at)
    manifest = bundle / "injection.json"
    document = common_document("inject", scope, deployment, started_at)
    document.update(
        {
            "status": "INJECTING",
            "previous_resources": previous,
            "fault_resources": injected,
            "previous_strategy": previous_strategy,
            "fault_strategy": injected_strategy,
            "pre_injection_generation": deployment["metadata"].get("generation"),
            "pre_injection_observed_generation": deployment.get("status", {}).get("observedGeneration"),
            "mutation_applied": False,
            "restore_command": restore_command(script, manifest, args.terraform_dir),
        }
    )
    write_json_atomic(manifest, document)
    try:
        patched = apply_workload(scope, deployment, injected, injected_strategy, dry_run=False)
        document.update(
            {
                "mutation_applied": True,
                "patched_at": utc_now(),
                "post_patch_generation": patched["metadata"].get("generation"),
                "post_patch_resources": deployment_resources(patched),
                "post_patch_strategy": deployment_strategy(patched),
            }
        )
        write_json_atomic(manifest, document)
        evidence = capture_immediate_evidence(scope, bundle, args.evidence_timeout)
        complete = all((evidence["oomkilled"], evidence["exit_137"], evidence["crash_loop_backoff"], evidence["max_restarts"] >= 1))
        document.update(
            {
                "completed_at": utc_now(),
                "status": "INJECTED" if complete else "INJECTED_EVIDENCE_INCOMPLETE",
                "outcome": "PASS" if complete else "INCOMPLETE",
                "immediate_evidence": evidence,
            }
        )
        write_json_atomic(manifest, document)
        if not complete:
            raise RuntimeError(f"Fault configuration is active but complete OOM evidence did not appear before timeout; restore with: {document['restore_command']}")
    except Exception as exc:
        if not document.get("mutation_applied"):
            document.update({"completed_at": utc_now(), "status": "FAILED_NO_MUTATION", "outcome": "FAIL", "error": str(exc)})
            write_json_atomic(manifest, document)
        raise

    print(f"Injection: PASS at {document['completed_at']}")
    print(f"Changed resource UID: {document['scope']['deployment_uid']}")
    print(f"Memory: {previous['requests']['memory']}/{previous['limits']['memory']} -> {FAULT_MEMORY}/{FAULT_MEMORY}")
    print(f"OOMKilled={evidence['oomkilled']} exit137={evidence['exit_137']} CrashLoopBackOff={evidence['crash_loop_backoff']} restarts={evidence['max_restarts']}")
    print(f"Evidence: {manifest}")
    print(f"Restore with: {document['restore_command']}")
    return 0


def command_restore(args: argparse.Namespace, script: Path) -> int:
    started_at = utc_now()
    try:
        injection = json.loads(args.manifest.resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read injection manifest {args.manifest}: {exc}") from exc
    scope = load_c1_context(args.terraform_dir.resolve())
    deployment = get_deployment(scope)
    validate_deployment(deployment)
    validate_manifest_scope(injection, scope, deployment)
    previous = injection.get("previous_resources")
    injected = injection.get("fault_resources")
    previous_strategy = injection.get("previous_strategy")
    injected_strategy = injection.get("fault_strategy")
    if not all(isinstance(item, dict) for item in (previous, injected, previous_strategy, injected_strategy)):
        raise RuntimeError("Injection manifest lacks the exact previous/fault resources or rollout strategies")
    current = deployment_resources(deployment)
    current_strategy = deployment_strategy(deployment)
    stage = restoration_stage(current, current_strategy, previous, previous_strategy, injected, injected_strategy)
    eligible = stage != "complete"
    if stage == "resources":
        preview_resources, preview_strategy = previous, injected_strategy
    else:
        preview_resources, preview_strategy = previous, previous_strategy
    auth_can_patch(scope)
    preview = apply_workload(scope, deployment, preview_resources, preview_strategy, dry_run=True)
    if deployment_resources(preview) != preview_resources or deployment_strategy(preview) != preview_strategy:
        raise RuntimeError("Server-side restore dry-run did not return the exact next restoration stage")
    if not args.execute:
        print(f"Restore preview: PASS; stage={stage}; eligible={str(eligible).lower()}")
        print(f"Exact resources to restore: {json.dumps(previous, sort_keys=True)}")
        print(f"Exact strategy to restore: {json.dumps(previous_strategy, sort_keys=True)}")
        print("No Kubernetes mutation was performed. Add --execute to restore.")
        return 0

    bundle = args.manifest.resolve().parent
    path = bundle / f"restoration-{utc_stamp(started_at)}.json"
    document = common_document("restore", scope, deployment, started_at)
    document.update(
        {
            "status": "RESTORING",
            "injection_manifest": str(args.manifest.resolve()),
            "pre_restore_resources": current,
            "pre_restore_strategy": current_strategy,
            "restored_resources": previous,
            "restored_strategy": previous_strategy,
            "eligible": eligible,
            "initial_stage": stage,
            "stages": [],
        }
    )
    write_json_atomic(path, document)

    if stage == "resources":
        apply_workload(scope, deployment, previous, injected_strategy, dry_run=False)
        resource_rollout = kubectl(
            scope,
            ["--namespace", NAMESPACE, "rollout", "status", f"deployment/{DEPLOYMENT}", f"--timeout={args.rollout_timeout}s"],
            timeout=args.rollout_timeout + 30,
            check=False,
        )
        deployment = get_deployment(scope)
        resource_ready = (
            deployment_resources(deployment) == previous
            and deployment_strategy(deployment) == injected_strategy
            and resource_rollout.returncode == 0
            and deployment.get("status", {}).get("observedGeneration") == deployment.get("metadata", {}).get("generation")
            and int(deployment.get("status", {}).get("availableReplicas", 0)) == int(deployment.get("spec", {}).get("replicas", 0))
        )
        document["stages"].append(
            {
                "stage": "resources-under-recreate",
                "completed_at": utc_now(),
                "ready": resource_ready,
                "rollout": {
                    "exit_code": resource_rollout.returncode,
                    "stdout": resource_rollout.stdout.strip(),
                    "stderr": resource_rollout.stderr.strip(),
                },
                "resources": deployment_resources(deployment),
                "strategy": deployment_strategy(deployment),
            }
        )
        write_json_atomic(path, document)
        if not resource_ready:
            document.update({"completed_at": utc_now(), "status": "RESTORE_INCOMPLETE", "outcome": "FAIL"})
            write_json_atomic(path, document)
            raise RuntimeError(f"Resource restoration under Recreate did not recover; evidence: {path}")
        stage = "strategy"

    if stage == "strategy":
        apply_workload(scope, deployment, previous, previous_strategy, dry_run=False)
        document["stages"].append(
            {
                "stage": "rollout-strategy",
                "completed_at": utc_now(),
                "resources": previous,
                "strategy": previous_strategy,
            }
        )
        write_json_atomic(path, document)

    rollout = kubectl(
        scope,
        ["--namespace", NAMESPACE, "rollout", "status", f"deployment/{DEPLOYMENT}", f"--timeout={args.rollout_timeout}s"],
        timeout=args.rollout_timeout + 30,
        check=False,
    )
    post = get_deployment(scope)
    ready = (
        deployment_resources(post) == previous
        and deployment_strategy(post) == previous_strategy
        and rollout.returncode == 0
        and post.get("status", {}).get("observedGeneration") == post.get("metadata", {}).get("generation")
        and int(post.get("status", {}).get("availableReplicas", 0)) == int(post.get("spec", {}).get("replicas", 0))
    )
    document.update(
        {
            "completed_at": utc_now(),
            "status": ("RESTORED" if eligible else "ALREADY_RESTORED") if ready else "RESTORE_INCOMPLETE",
            "outcome": "PASS" if ready else "FAIL",
            "rollout": {"exit_code": rollout.returncode, "stdout": rollout.stdout.strip(), "stderr": rollout.stderr.strip()},
            "post_restore_generation": post.get("metadata", {}).get("generation"),
            "post_restore_observed_generation": post.get("status", {}).get("observedGeneration"),
            "post_restore_available_replicas": post.get("status", {}).get("availableReplicas"),
            "post_restore_resources": deployment_resources(post),
            "post_restore_strategy": deployment_strategy(post),
        }
    )
    write_json_atomic(path, document)
    if not ready:
        raise RuntimeError(f"Restoration rollout did not recover; evidence: {path}")
    print(f"Restoration: PASS at {document['completed_at']}")
    print(f"Changed resource UID: {document['scope']['deployment_uid'] if eligible else 'none (already restored)'}")
    print(f"Exact previous resources restored: {json.dumps(previous, sort_keys=True)}")
    print(f"Evidence: {path}")
    print(f'Capture recovery with: "{sys.executable}" "{Path("faults/capture_fault2.py")}" recovery --manifest "{args.manifest.resolve()}" --restoration "{path}"')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform-dir", type=Path, default=Path("terraform"))
    parser.add_argument("--output-root", type=Path, default=Path("evidence/generated/phase11"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="Validate scope, authorization, and the server dry-run without a rollout")
    inject = subparsers.add_parser("inject", help=f"Journal and apply the locked {FAULT_MEMORY} request/limit")
    inject.add_argument("--execute", action="store_true", help="Required acknowledgement for the live fault")
    inject.add_argument("--evidence-timeout", type=int, default=180, help="Seconds to wait for OOM/137/CrashLoop evidence")
    restore = subparsers.add_parser("restore", help="Restore only the exact resources journaled by injection")
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--execute", action="store_true", help="Perform the manifest-bound restoration")
    restore.add_argument("--rollout-timeout", type=int, default=300)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    script = Path(__file__).resolve()
    try:
        if args.command == "preflight":
            return command_preflight(args, script)
        if args.command == "inject":
            return command_inject(args, script)
        if args.command == "restore":
            return command_restore(args, script)
        parser.error(f"Unsupported command: {args.command}")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
