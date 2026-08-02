#!/usr/bin/env python3
"""Capture the complete UTC-stamped Phase 9 healthy evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


PROJECT = "eks-observability-lab"
NAMESPACE = "online-boutique"
DEFAULT_GRAFANA_URL = "https://epicspider2262.grafana.net"
DEFAULT_DASHBOARD_UID = "eks-cross-region-incident-lab"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stamp_from_utc(value: str) -> str:
    return value.replace("-", "").replace(":", "")


@dataclass
class Check:
    id: str
    name: str
    status: str
    detail: str
    evidence_files: list[str]


class Capture:
    def __init__(self, bundle_dir: Path, started_at: str) -> None:
        self.bundle_dir = bundle_dir
        self.started_at = started_at
        self.checks: list[Check] = []

    def run(self, check_id: str, name: str, action: Callable[[], tuple[str, list[str]]]) -> None:
        print(f"Capturing {name}...")
        try:
            detail, files = action()
            self.checks.append(Check(check_id, name, "PASS", detail, files))
            print(f"PASS: {detail}")
        except Exception as exc:  # Continue so a failed run still preserves useful evidence.
            self.checks.append(Check(check_id, name, "FAIL", str(exc), []))
            print(f"FAIL: {exc}", file=sys.stderr)

    def write_report(self) -> tuple[Path, Path, dict[str, Any]]:
        completed_at = utc_now()
        failed = sum(check.status == "FAIL" for check in self.checks)
        document = {
            "schema_version": 1,
            "project": PROJECT,
            "phase": 9,
            "state": "healthy",
            "outcome": "PASS" if failed == 0 else "FAIL",
            "started_at": self.started_at,
            "completed_at": completed_at,
            "summary": {"PASS": len(self.checks) - failed, "FAIL": failed},
            "checks": [asdict(check) for check in self.checks],
        }
        manifest_path = self.bundle_dir / "manifest.json"
        manifest_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        lines = [
            f"EKS Observability Lab Phase 9 healthy baseline: {document['outcome']}",
            f"Started (UTC): {self.started_at}",
            f"Completed (UTC): {completed_at}",
            f"Summary: PASS={document['summary']['PASS']} FAIL={failed}",
            "",
        ]
        for check in self.checks:
            lines.extend([f"[{check.status}] {check.id} - {check.name}", f"  {check.detail}"])
            if check.evidence_files:
                lines.append(f"  Evidence: {', '.join(check.evidence_files)}")
            lines.append("")
        summary_path = self.bundle_dir / "SUMMARY.txt"
        summary_path.write_text("\n".join(lines), encoding="utf-8")
        return summary_path, manifest_path, document


def run_command(arguments: Sequence[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(arguments),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        output = result.stderr.strip() or result.stdout.strip() or "no command output"
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(arguments)}: {output}")
    return result


def write_json(path: Path, value: Any) -> str:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path.name


def load_terraform_outputs(terraform_dir: Path, timeout: int) -> dict[str, Any]:
    result = run_command(["terraform", f"-chdir={terraform_dir}", "output", "-json"], timeout=timeout)
    raw = json.loads(result.stdout)
    try:
        return {key: item["value"] for key, item in raw.items()}
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Terraform outputs do not match the expected root-output format") from exc


def find_verification_check(document: dict[str, Any], check_id: str) -> dict[str, Any]:
    matches = [item for item in document.get("checks", []) if item.get("id") == check_id]
    if len(matches) != 1:
        raise RuntimeError(f"Verification report does not contain exactly one {check_id!r} check")
    return matches[0]


def parse_promtool_scalar(output: str, *, required_label: str | None = None) -> float:
    candidate = output
    if required_label is not None:
        matching_lines = [line for line in output.splitlines() if required_label in line]
        if len(matching_lines) != 1:
            raise ValueError(
                f"Expected one Prometheus series containing {required_label!r}, found {len(matching_lines)}"
            )
        candidate = matching_lines[0]
    match = re.search(r"=>\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s+@", candidate)
    if not match:
        raise ValueError(f"Unable to parse Prometheus scalar from: {output.strip()}")
    return float(match.group(1))


def summarize_pods(document: dict[str, Any]) -> dict[str, Any]:
    pods: list[dict[str, Any]] = []
    unhealthy: list[str] = []
    for item in document.get("items", []):
        name = item.get("metadata", {}).get("name", "<unknown>")
        status = item.get("status", {})
        container_statuses = status.get("containerStatuses", [])
        ready = bool(container_statuses) and all(container.get("ready") is True for container in container_statuses)
        phase = status.get("phase")
        restarts = sum(int(container.get("restartCount", 0)) for container in container_statuses)
        pods.append({"name": name, "phase": phase, "ready": ready, "restarts": restarts})
        if phase != "Running" or not ready:
            unhealthy.append(name)
    if not pods:
        raise RuntimeError("No application Pods were returned")
    return {"count": len(pods), "unhealthy": unhealthy, "pods": pods}


def timed_http(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    data: dict[str, str] | None = None,
    timeout: int,
) -> tuple[dict[str, Any], bytes]:
    encoded = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=encoded, headers={"User-Agent": f"{PROJECT}-phase9/1.0"})
    started = time.perf_counter()
    with opener.open(request, timeout=timeout) as response:
        body = response.read()
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        result = {
            "method": "POST" if data is not None else "GET",
            "url": url,
            "final_url": response.geturl(),
            "status": response.status,
            "elapsed_ms": elapsed_ms,
            "bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        if response.status != 200:
            raise RuntimeError(f"{result['method']} {url} returned HTTP {response.status}")
        return result, body


def capture_application_journey(base_url: str, timeout: int) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    steps: list[dict[str, Any]] = []

    for path in ("/", "/product/OLJCESPC7Z"):
        result, _ = timed_http(opener, f"{base_url}{path}", timeout=timeout)
        steps.append(result)

    result, _ = timed_http(
        opener,
        f"{base_url}/cart",
        data={"product_id": "OLJCESPC7Z", "quantity": "1"},
        timeout=timeout,
    )
    steps.append(result)
    result, cart_body = timed_http(opener, f"{base_url}/cart", timeout=timeout)
    steps.append(result)
    if b"OLJCESPC7Z" not in cart_body:
        raise RuntimeError("Cart page did not contain the selected product ID")

    checkout_data = {
        "email": "phase9@example.com",
        "street_address": "1 Baseline Way",
        "zip_code": "10001",
        "city": "New York",
        "state": "NY",
        "country": "United States",
        "credit_card_number": "4111111111111111",
        "credit_card_expiration_month": "12",
        "credit_card_expiration_year": str(datetime.now(timezone.utc).year + 5),
        "credit_card_cvv": "123",
    }
    result, checkout_body = timed_http(
        opener,
        f"{base_url}/cart/checkout",
        data=checkout_data,
        timeout=timeout,
    )
    steps.append(result)
    text = checkout_body.decode("utf-8", errors="replace").lower()
    if "order confirmation" not in text and "order is complete" not in text:
        raise RuntimeError("Checkout returned HTTP 200 but no order-confirmation marker")
    return {"base_url": base_url, "checkout_confirmed": True, "steps": steps}


def grafana_request(
    base_url: str,
    token: str,
    path: str,
    *,
    timeout: int,
    expect_json: bool,
) -> tuple[Any, str]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json,image/png"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Grafana API {path} returned HTTP {exc.code}: {body[:300]}") from exc
    if expect_json:
        try:
            return json.loads(body), content_type
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Grafana API {path} did not return JSON ({content_type})") from exc
    return body, content_type


def alert_state(value: Any) -> str:
    if isinstance(value, dict):
        labels = value.get("labels") or {}
        return str(value.get("status", {}).get("state") or labels.get("alertstate") or "unknown").lower()
    return "unknown"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    repository_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Optional AWS shared-config profile passed to the Phase 8 verifier")
    parser.add_argument("--terraform-dir", type=Path, default=repository_root / "terraform")
    parser.add_argument("--output-root", type=Path, default=repository_root / "evidence" / "generated" / "phase9")
    parser.add_argument("--application-url", help="Override the Terraform/Kubernetes-discovered frontend URL")
    parser.add_argument("--grafana-url", default=DEFAULT_GRAFANA_URL)
    parser.add_argument("--grafana-dashboard-uid", default=DEFAULT_DASHBOARD_UID)
    parser.add_argument(
        "--grafana-token-env",
        default="GRAFANA_SERVICE_ACCOUNT_TOKEN",
        help="Environment variable containing a read-only Grafana service-account token",
    )
    parser.add_argument("--timeout", type=int, default=120, help="Per-command/request timeout in seconds")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repository_root = Path(__file__).resolve().parent.parent
    started_at = utc_now()
    bundle_dir = args.output_root.resolve() / f"healthy-{stamp_from_utc(started_at)}"
    try:
        bundle_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print(f"Unable to create evidence bundle {bundle_dir}: {exc}", file=sys.stderr)
        return 2

    capture = Capture(bundle_dir, started_at)
    state: dict[str, Any] = {}

    def terraform_action() -> tuple[str, list[str]]:
        outputs = load_terraform_outputs(args.terraform_dir.resolve(), args.timeout)
        state["terraform"] = outputs
        evidence = {
            "account_id": outputs["account_id"],
            "clusters": outputs["clusters"],
            "networking": outputs["networking"],
            "security": outputs["security"],
        }
        name = write_json(bundle_dir / "deployment-scope.json", evidence)
        return "Terraform deployment scope loaded for both Regions", [name]

    capture.run("deployment-scope", "Terraform deployment scope", terraform_action)

    def verification_action() -> tuple[str, list[str]]:
        verification_dir = bundle_dir / "verification"
        command = [
            sys.executable,
            str(repository_root / "verification" / "verify.py"),
            "--state",
            "healthy",
            "--terraform-dir",
            str(args.terraform_dir.resolve()),
            "--output-dir",
            str(verification_dir),
            "--timeout",
            str(args.timeout),
        ]
        if args.profile:
            command.extend(["--profile", args.profile])
        run_command(command, timeout=max(args.timeout * 4, 300), cwd=repository_root)
        reports = list(verification_dir.glob("verification-healthy-*.json"))
        if len(reports) != 1:
            raise RuntimeError("Phase 8 verifier did not create exactly one JSON report in the bundle")
        document = json.loads(reports[0].read_text(encoding="utf-8"))
        if document.get("outcome") != "PASS":
            raise RuntimeError(f"Phase 8 verifier outcome is {document.get('outcome')!r}, not PASS")
        state["verification"] = document
        relative = [str(path.relative_to(bundle_dir)) for path in sorted(verification_dir.iterdir())]
        return "Full healthy verifier passed, including authorized success and unauthorized denial", relative

    capture.run("verification", "Full healthy verification", verification_action)

    def application_action() -> tuple[str, list[str]]:
        base_url = args.application_url
        if not base_url:
            verification = state.get("verification")
            if verification is None:
                raise RuntimeError("Cannot discover the frontend because verification did not pass")
            exposure = find_verification_check(verification, "kubernetes-exposure")
            hostname = exposure.get("evidence", {}).get("frontend_ingress_hostname")
            if not hostname:
                raise RuntimeError("Verifier did not report a frontend Ingress hostname")
            base_url = f"http://{hostname}"
        journey = capture_application_journey(base_url, args.timeout)
        name = write_json(bundle_dir / "application-journey.json", journey)
        return "Home, product, add-to-cart, cart view, and checkout all succeeded", [name]

    capture.run("application-checkout", "Application and checkout journey", application_action)

    def cart_probe_action() -> tuple[str, list[str]]:
        outputs = state.get("terraform")
        if outputs is None:
            raise RuntimeError("Cannot query the C1 probe without Terraform cluster outputs")
        context = outputs["clusters"]["c1"]["name"]
        result: dict[str, Any] = {"context": context, "captured_at": utc_now(), "queries": {}}
        expressions = {
            # The cluster label is attached by Prometheus remote_write and is not
            # present when querying the C1 server's local storage directly.
            "success": "probe_success",
            "duration_seconds": "probe_duration_seconds",
        }
        for key, expression in expressions.items():
            command = [
                "kubectl", "--context", context, "--namespace", "observability", "exec",
                "deployment/prometheus-server", "-c", "prometheus-server", "--",
                "/bin/promtool", "query", "instant", "http://127.0.0.1:9090", expression,
            ]
            command_result = run_command(command, timeout=args.timeout)
            result["queries"][key] = {
                "expression": expression,
                "value": parse_promtool_scalar(
                    command_result.stdout, required_label='job="cross-region-cart"'
                ),
                "raw": command_result.stdout.strip(),
            }
        if result["queries"]["success"]["value"] != 1.0:
            raise RuntimeError("Cross-region cart probe is not successful")
        duration = result["queries"]["duration_seconds"]["value"]
        if duration < 0:
            raise RuntimeError("Cross-region cart probe returned a negative duration")
        name = write_json(bundle_dir / "cross-region-cart-probe.json", result)
        return f"C1-to-C2 cart probe succeeded in {duration:.6f} seconds", [name]

    capture.run("cross-region-cart", "C1-to-C2 cart success and latency", cart_probe_action)

    def pod_health_action() -> tuple[str, list[str]]:
        outputs = state.get("terraform")
        if outputs is None:
            raise RuntimeError("Cannot query Pods without Terraform cluster outputs")
        files: list[str] = []
        counts: list[str] = []
        for side in ("c1", "c2"):
            context = outputs["clusters"][side]["name"]
            command_result = run_command(
                ["kubectl", "--context", context, "--namespace", NAMESPACE, "get", "pods", "-o", "json"],
                timeout=args.timeout,
            )
            document = json.loads(command_result.stdout)
            summary = summarize_pods(document)
            if summary["unhealthy"]:
                raise RuntimeError(f"{side.upper()} has unhealthy Pods: {', '.join(summary['unhealthy'])}")
            filename = f"{side}-pods.json"
            write_json(bundle_dir / filename, {"context": context, "captured_at": utc_now(), **summary})
            files.append(filename)
            counts.append(f"{side.upper()}={summary['count']}")
        return f"All application Pods are Running and Ready ({', '.join(counts)})", files

    capture.run("pod-health", "C1 and C2 Pod health", pod_health_action)

    def redis_action() -> tuple[str, list[str]]:
        outputs = state.get("terraform")
        if outputs is None:
            raise RuntimeError("Cannot query Redis without Terraform cluster outputs")
        context = outputs["clusters"]["c2"]["name"]
        result = run_command(
            [
                "kubectl", "--context", context, "--namespace", NAMESPACE, "exec",
                "deployment/redis-cart", "--", "redis-cli", "ping",
            ],
            timeout=args.timeout,
        )
        response = result.stdout.strip()
        if response != "PONG":
            raise RuntimeError(f"Redis health response was {response!r}, not 'PONG'")
        filename = "redis-health.txt"
        (bundle_dir / filename).write_text(f"Captured (UTC): {utc_now()}\nContext: {context}\nResponse: PONG\n", encoding="utf-8")
        return "C2 Redis returned PONG", [filename]

    capture.run("redis-health", "C2 Redis health", redis_action)

    def aws_edge_action() -> tuple[str, list[str]]:
        verification = state.get("verification")
        if verification is None:
            raise RuntimeError("Cannot extract AWS edge evidence because verification did not pass")
        checks = {
            check_id: find_verification_check(verification, check_id)
            for check_id in ("load-balancers", "target-health", "waf")
        }
        if any(check.get("status") != "PASS" for check in checks.values()):
            raise RuntimeError("One or more ALB/NLB/WAF verifier checks did not pass")
        filename = write_json(bundle_dir / "aws-edge-health.json", {"captured_at": utc_now(), "checks": checks})
        return "ALB/NLB inventory and targets are healthy and the WAF is associated", [filename]

    capture.run("aws-edge", "ALB/NLB target health and WAF association", aws_edge_action)

    def unauthorized_action() -> tuple[str, list[str]]:
        verification = state.get("verification")
        if verification is None:
            raise RuntimeError("Cannot extract probe evidence because verification did not pass")
        check = find_verification_check(verification, "active-probes")
        outcomes = check.get("evidence", {}).get("outcomes", {})
        authorized = outcomes.get("authorized", {}).get("exit_code")
        unauthorized = outcomes.get("unauthorized", {}).get("exit_code")
        if check.get("status") != "PASS" or authorized != 0 or unauthorized == 0:
            raise RuntimeError("Active-probe evidence does not prove authorized success and unauthorized denial")
        filename = write_json(bundle_dir / "network-policy-probes.json", {"captured_at": utc_now(), "check": check})
        return "Authorized request connected and unauthorized request was denied", [filename]

    capture.run("unauthorized-denial", "Unauthorized request failure", unauthorized_action)

    def grafana_action() -> tuple[str, list[str]]:
        token = os.environ.get(args.grafana_token_env)
        if not token:
            raise RuntimeError(
                f"{args.grafana_token_env} is not set; a read-only Grafana service-account token is required"
            )
        files: list[str] = []
        dashboard, _ = grafana_request(
            args.grafana_url,
            token,
            f"/api/dashboards/uid/{urllib.parse.quote(args.grafana_dashboard_uid)}",
            timeout=args.timeout,
            expect_json=True,
        )
        files.append(write_json(bundle_dir / "grafana-dashboard.json", dashboard))

        rules, _ = grafana_request(
            args.grafana_url, token, "/api/v1/provisioning/alert-rules", timeout=args.timeout, expect_json=True
        )
        if not isinstance(rules, list) or not rules:
            raise RuntimeError("Grafana returned no provisioned alert rules")
        files.append(write_json(bundle_dir / "grafana-alert-rules.json", rules))

        active, _ = grafana_request(
            args.grafana_url,
            token,
            "/api/alertmanager/grafana/api/v2/alerts",
            timeout=args.timeout,
            expect_json=True,
        )
        if not isinstance(active, list):
            raise RuntimeError("Grafana active-alert response is not a list")
        non_quiet = [item for item in active if alert_state(item) not in ("normal", "resolved")]
        files.append(write_json(bundle_dir / "grafana-active-alerts.json", active))
        if non_quiet:
            raise RuntimeError(f"Grafana has {len(non_quiet)} active or unknown-state alerts")

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        from_ms = now_ms - (60 * 60 * 1000)
        render_path = (
            f"/render/d/{urllib.parse.quote(args.grafana_dashboard_uid)}/phase9-baseline"
            f"?from={from_ms}&to={now_ms}&width=1800&height=1200&tz=UTC&kiosk"
        )
        image, content_type = grafana_request(
            args.grafana_url, token, render_path, timeout=max(args.timeout, 180), expect_json=False
        )
        if content_type != "image/png" or len(image) < 10_000:
            raise RuntimeError(
                f"Grafana dashboard render was not a usable PNG (type={content_type}, bytes={len(image)})"
            )
        screenshot = bundle_dir / "grafana-dashboard-baseline.png"
        screenshot.write_bytes(image)
        files.append(screenshot.name)
        return f"Grafana has {len(rules)} alert rules, no active alerts, and a rendered one-hour UTC baseline", files

    capture.run("grafana-baseline", "Quiet alert state and dashboard baseline", grafana_action)

    try:
        summary_path, manifest_path, document = capture.write_report()
    except OSError as exc:
        print(f"Unable to write final evidence reports: {exc}", file=sys.stderr)
        return 2

    print(f"Summary:  {summary_path}")
    print(f"Manifest: {manifest_path}")
    print(f"Outcome:  {document['outcome']}")
    return 0 if document["outcome"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
