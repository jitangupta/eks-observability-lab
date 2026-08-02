#!/usr/bin/env python3
"""Capture the ordered Phase 10 Fault 1 investigation evidence."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from fault1_nacl import (
    PROJECT,
    create_boto_clients,
    describe_topology,
    load_scope,
    require_identity,
    run_command,
    utc_now,
    utc_stamp,
    validate_manifest_scope,
    write_json_atomic,
)


DEFAULT_GRAFANA_URL = "https://epicspider2262.grafana.net"
DEFAULT_DASHBOARD_UID = "eks-cross-region-incident-lab"


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return value


def terraform_values(terraform_dir: Path) -> dict[str, Any]:
    result = run_command(["terraform", f"-chdir={terraform_dir}", "output", "-json"])
    try:
        raw = json.loads(result.stdout)
        return {name: item["value"] for name, item in raw.items()}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Terraform outputs do not match the investigation contract") from exc


def latest_passing_fault_verification(bundle: Path) -> tuple[Path, dict[str, Any]]:
    candidates = sorted((bundle / "verification").glob("verification-fault1-*.json"), reverse=True)
    for path in candidates:
        document = read_json(path)
        if document.get("outcome") == "PASS":
            return path, document
    raise RuntimeError("No PASS during-fault verification report exists in the injection bundle")


def check_by_id(document: dict[str, Any], check_id: str) -> dict[str, Any]:
    matches = [item for item in document.get("checks", []) if item.get("id") == check_id]
    if len(matches) != 1:
        raise RuntimeError(f"Verification report does not contain exactly one {check_id!r} check")
    return matches[0]


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
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Grafana API returned HTTP {exc.code} for {path}: {detail[:300]}") from exc
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


def read_grafana_service_account_token(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to read Grafana token file {path}") from exc
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    for line in lines:
        label, separator, value = line.partition(":")
        if separator and label.strip().lower() == "grafana service account" and value.strip():
            return value.strip()
    if len(lines) == 1 and not any(separator in lines[0] for separator in ("=", ":")):
        return lines[0]
    raise RuntimeError("Grafana token file must contain a 'grafana service account:' line or one raw token")


def describe_flow_rejects(logs: Any, log_group: str, start: datetime) -> dict[str, Any]:
    response = logs.filter_log_events(
        logGroupName=log_group,
        startTime=int(start.timestamp() * 1000),
        endTime=int(datetime.now(timezone.utc).timestamp() * 1000),
        filterPattern='"REJECT"',
        limit=1000,
    )
    events = []
    matching = []
    for event in response.get("events", []):
        item = {
            "timestamp": event.get("timestamp"),
            "ingestion_time": event.get("ingestionTime"),
            "log_stream_name": event.get("logStreamName"),
            "message": event.get("message", ""),
        }
        events.append(item)
        fields = str(item["message"]).split()
        if len(fields) >= 14:
            try:
                protocol = int(fields[7])
                destination_port = int(fields[6])
            except ValueError:
                continue
            if protocol == 6 and destination_port == 7070 and fields[12] == "REJECT":
                matching.append(item)
    return {"log_group": log_group, "events": events, "tcp_7070_rejects": matching}


def describe_cloudtrail(
    cloudtrail: Any, start: datetime, nacl_ids: Sequence[str], rule_number: int
) -> dict[str, Any]:
    response = cloudtrail.lookup_events(
        LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "CreateNetworkAclEntry"}],
        StartTime=start,
        EndTime=datetime.now(timezone.utc),
        MaxResults=50,
    )
    matches = []
    for event in response.get("Events", []):
        try:
            detail = json.loads(event.get("CloudTrailEvent", "{}"))
        except json.JSONDecodeError:
            continue
        request = detail.get("requestParameters") or {}
        if request.get("networkAclId") in set(nacl_ids) and int(request.get("ruleNumber", -1)) == rule_number:
            matches.append(
                {
                    "event_id": event.get("EventId"),
                    "event_time": (
                        event["EventTime"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                        if event.get("EventTime")
                        else None
                    ),
                    "event_name": event.get("EventName"),
                    "username": event.get("Username"),
                    "source_ip": detail.get("sourceIPAddress"),
                    "user_agent": detail.get("userAgent"),
                    "error_code": detail.get("errorCode"),
                    "error_message": detail.get("errorMessage"),
                    "request_parameters": request,
                }
            )
    return {
        "events": matches,
        "successful_events": [item for item in matches if not item["error_code"]],
        "dry_run_events": [item for item in matches if item["error_code"] == "Client.DryRunOperation"],
    }


def alert_name(alert: dict[str, Any]) -> str:
    labels = alert.get("labels") or {}
    return str(labels.get("alertname") or labels.get("rulename") or "<unnamed>")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture ordered evidence while Fault 1 is active")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--terraform-dir", type=Path, default=Path("terraform"))
    parser.add_argument("--profile")
    parser.add_argument("--grafana-url", default=DEFAULT_GRAFANA_URL)
    parser.add_argument("--grafana-dashboard-uid", default=DEFAULT_DASHBOARD_UID)
    parser.add_argument("--grafana-token-file", type=Path, default=Path("secrets/grafana-cloud.txt"))
    parser.add_argument("--timeout", type=int, default=120)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        injection_path = args.manifest.resolve()
        injection = read_json(injection_path)
        if injection.get("status") != "INJECTED":
            raise RuntimeError("Investigation capture requires a completed INJECTED manifest")
        scope = load_scope(args.terraform_dir.resolve())
        validate_manifest_scope(injection, scope)
        bundle = injection_path.parent
        verification_path, verification = latest_passing_fault_verification(bundle)
        values = terraform_values(args.terraform_dir.resolve())
        injection_time = parse_utc(str(injection["completed_at"]))
        query_start = injection_time - timedelta(minutes=5)

        sts, ec2 = create_boto_clients(args.profile, scope)
        identity = require_identity(sts, scope)
        topology = describe_topology(ec2, scope)
        if topology["state"] != "fault1":
            raise RuntimeError(f"Fault 1 is not active; observed NACL state: {topology['state']}")

        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("boto3 is required") from exc
        session = boto3.Session(**({"profile_name": args.profile} if args.profile else {}))
        config = Config(retries={"max_attempts": 4, "mode": "standard"})
        logs = session.client("logs", region_name=scope.region, config=config)
        cloudtrail = session.client("cloudtrail", region_name=scope.region, config=config)
        flow = describe_flow_rejects(logs, values["log_groups"]["c2_vpc_rejects"], query_start)
        trail = describe_cloudtrail(cloudtrail, query_start, scope.nacl_ids, scope.rule_number)

        token = read_grafana_service_account_token(args.grafana_token_file.resolve())
        active_alerts = grafana_request(
            args.grafana_url,
            token,
            "/api/alertmanager/grafana/api/v2/alerts",
            args.timeout,
            json_result=True,
        )
        if not isinstance(active_alerts, list):
            raise RuntimeError("Grafana active-alert response is not a list")

        captured_at = utc_now()
        to_ms = int(parse_utc(captured_at).timestamp() * 1000)
        from_ms = int((injection_time - timedelta(minutes=5)).timestamp() * 1000)
        render_path = (
            f"/render/d/{urllib.parse.quote(args.grafana_dashboard_uid)}/phase10-fault1"
            f"?from={from_ms}&to={to_ms}&width=1800&height=1200&tz=UTC&kiosk"
        )
        dashboard = grafana_request(
            args.grafana_url, token, render_path, max(args.timeout, 180), json_result=False
        )

        investigation_dir = bundle / "investigation"
        investigation_dir.mkdir(parents=True, exist_ok=True)
        screenshot = investigation_dir / f"grafana-fault1-{utc_stamp(captured_at)}.png"
        screenshot.write_bytes(dashboard)
        alerts_path = investigation_dir / f"grafana-active-alerts-{utc_stamp(captured_at)}.json"
        write_json_atomic(alerts_path, {"captured_at": captured_at, "alerts": active_alerts})

        dead_end_ids = ("routes", "security-groups", "target-health", "cni-runtime", "kubernetes-exposure")
        dead_ends = [
            {
                "hypothesis": check_by_id(verification, check_id)["name"],
                "status": "EXONERATED",
                "evidence": check_by_id(verification, check_id)["detail"],
            }
            for check_id in dead_end_ids
        ]
        probe = check_by_id(verification, "active-probes")
        document = {
            "schema_version": 1,
            "project": PROJECT,
            "phase": 10,
            "fault": "fault1-nacl",
            "captured_at": captured_at,
            "injected_at": injection["completed_at"],
            "identity": identity,
            "verification_report": str(verification_path),
            "symptom": {"status": probe["status"], "detail": probe["detail"], "evidence": probe.get("evidence")},
            "dead_ends": dead_ends,
            "nacl": topology,
            "flow_logs": flow,
            "cloudtrail": trail,
            "grafana": {
                "active_alert_count": len(active_alerts),
                "active_alert_names": sorted(alert_name(item) for item in active_alerts),
                "alerts_file": str(alerts_path),
                "dashboard_file": str(screenshot),
            },
            "root_cause": (
                "High-precedence C2 private-subnet NACL ingress deny for C1 CIDR on TCP/7070, "
                "corroborated by the exact live entry, Flow Log rejects, and CloudTrail create event."
            ),
        }
        missing = []
        if not flow["tcp_7070_rejects"]:
            missing.append("C2 Flow Log TCP/7070 REJECT")
        successful_nacl_ids = {
            item["request_parameters"].get("networkAclId") for item in trail["successful_events"]
        }
        missing_cloudtrail_ids = sorted(set(scope.nacl_ids) - successful_nacl_ids)
        if missing_cloudtrail_ids:
            missing.append(f"successful CloudTrail create for NACLs {missing_cloudtrail_ids}")
        if not active_alerts:
            missing.append("Grafana active alert")
        document["outcome"] = "PASS" if not missing else "INCOMPLETE"
        document["missing_evidence"] = missing
        output = investigation_dir / f"investigation-{utc_stamp(captured_at)}.json"
        write_json_atomic(output, document)
        print(f"Investigation evidence: {output}")
        print(f"Outcome: {document['outcome']}")
        print(f"Active alerts: {', '.join(document['grafana']['active_alert_names']) or 'none'}")
        print(f"TCP/7070 Flow Log rejects: {len(flow['tcp_7070_rejects'])}")
        print(f"CloudTrail successful creates: {len(trail['successful_events'])}")
        print(f"CloudTrail validation DryRuns: {len(trail['dry_run_events'])}")
        if missing:
            print(f"Missing: {', '.join(missing)}")
        return 0 if not missing else 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
