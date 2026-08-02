#!/usr/bin/env python3
"""Safely preflight, inject, and restore the Phase 10 Fault 1 NACL deny."""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


PROJECT = "eks-observability-lab"
FAULT_NAME = "fault1-nacl"
FAULT_PORT = 7070
PROTOCOL_NUMBER = "6"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_stamp(value: str) -> str:
    return value.replace("-", "").replace(":", "")


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_command(arguments: Sequence[str | Path], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    rendered = [str(item) for item in arguments]
    try:
        result = subprocess.run(rendered, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command is not installed or not on PATH: {rendered[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(rendered)}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no command output"
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(rendered)}\n{detail}")
    return result


@dataclass(frozen=True)
class FaultScope:
    account_id: str
    region: str
    vpc_id: str
    source_cidr: str
    private_subnet_ids: tuple[str, ...]
    nacl_ids: tuple[str, ...]
    rule_number: int
    port: int = FAULT_PORT

    def validate(self) -> None:
        if not (self.account_id.isdigit() and len(self.account_id) == 12):
            raise RuntimeError(f"Invalid Terraform AWS account ID: {self.account_id!r}")
        if not self.region or not self.vpc_id:
            raise RuntimeError("Terraform scope is missing the C2 region or VPC ID")
        try:
            network = ipaddress.ip_network(self.source_cidr, strict=True)
        except ValueError as exc:
            raise RuntimeError(f"Invalid C1 source CIDR: {self.source_cidr!r}") from exc
        if network.version != 4:
            raise RuntimeError("Fault 1 supports only the Terraform IPv4 C1 CIDR")
        if not self.private_subnet_ids or not self.nacl_ids:
            raise RuntimeError("Terraform scope contains no C2 private subnets or NACLs")
        if len(set(self.private_subnet_ids)) != len(self.private_subnet_ids):
            raise RuntimeError("Terraform C2 private subnet IDs contain duplicates")
        if len(set(self.nacl_ids)) != len(self.nacl_ids):
            raise RuntimeError("Terraform C2 private NACL IDs contain duplicates")
        if not 1 <= self.rule_number <= 32766:
            raise RuntimeError(f"Reserved NACL rule number is outside the usable range: {self.rule_number}")
        if self.rule_number >= 100:
            raise RuntimeError("Fault rule must have higher precedence than the rule-100 baseline allow")
        if self.port != FAULT_PORT:
            raise RuntimeError(f"Fault 1 must target TCP/{FAULT_PORT}, not TCP/{self.port}")


def load_scope(terraform_dir: Path) -> FaultScope:
    result = run_command(["terraform", f"-chdir={terraform_dir}", "output", "-json"])
    try:
        raw = json.loads(result.stdout)
        values = {name: item["value"] for name, item in raw.items()}
        scope = FaultScope(
            account_id=str(values["account_id"]),
            region=str(values["clusters"]["c2"]["region"]),
            vpc_id=str(values["networking"]["c2"]["vpc_id"]),
            source_cidr=str(values["networking"]["c1"]["cidr"]),
            private_subnet_ids=tuple(sorted(str(value) for value in values["networking"]["c2"]["private_subnet_ids"])),
            nacl_ids=tuple(sorted(str(value) for value in values["networking"]["c2"]["private_nacl_ids"])),
            rule_number=int(values["security"]["fault_nacl_rule_number"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Terraform outputs do not match the Phase 10 fault-scope contract") from exc
    scope.validate()
    return scope


def create_boto_clients(profile: str | None, scope: FaultScope) -> tuple[Any, Any]:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is not installed; run: python -m pip install -r verification/requirements.txt"
        ) from exc
    config = Config(
        connect_timeout=10,
        read_timeout=30,
        retries={"max_attempts": 4, "mode": "standard"},
        user_agent_extra="eks-observability-lab-phase10-fault1",
    )
    session = boto3.Session(**({"profile_name": profile} if profile else {}))
    return (
        session.client("sts", region_name=scope.region, config=config),
        session.client("ec2", region_name=scope.region, config=config),
    )


def expected_entry(scope: FaultScope) -> dict[str, Any]:
    return {
        "RuleNumber": scope.rule_number,
        "Egress": False,
        "Protocol": PROTOCOL_NUMBER,
        "RuleAction": "deny",
        "CidrBlock": scope.source_cidr,
        "PortRange": {"From": scope.port, "To": scope.port},
    }


def is_exact_entry(entry: dict[str, Any], scope: FaultScope) -> bool:
    port_range = entry.get("PortRange") or {}
    return (
        int(entry.get("RuleNumber", -1)) == scope.rule_number
        and entry.get("Egress") is False
        and str(entry.get("Protocol")) == PROTOCOL_NUMBER
        and entry.get("RuleAction") == "deny"
        and entry.get("CidrBlock") == scope.source_cidr
        and int(port_range.get("From", -1)) == scope.port
        and int(port_range.get("To", -1)) == scope.port
    )


def has_baseline_allow(entries: Sequence[dict[str, Any]], *, egress: bool) -> bool:
    return any(
        entry.get("Egress") is egress
        and int(entry.get("RuleNumber", -1)) == 100
        and str(entry.get("Protocol")) == "-1"
        and entry.get("RuleAction") == "allow"
        and entry.get("CidrBlock") == "0.0.0.0/0"
        for entry in entries
    )


def inspect_topology(scope: FaultScope, nacls: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(nacl.get("NetworkAclId")): nacl for nacl in nacls}
    if set(by_id) != set(scope.nacl_ids):
        raise RuntimeError(
            f"AWS returned NACL IDs {sorted(by_id)}, expected exactly {sorted(scope.nacl_ids)}"
        )

    associated_subnets: set[str] = set()
    summaries: list[dict[str, Any]] = []
    states: list[str] = []
    for nacl_id in scope.nacl_ids:
        nacl = by_id[nacl_id]
        if nacl.get("VpcId") != scope.vpc_id:
            raise RuntimeError(f"NACL {nacl_id} is not in Terraform C2 VPC {scope.vpc_id}")
        subnet_ids = sorted(
            str(association["SubnetId"])
            for association in nacl.get("Associations", [])
            if association.get("SubnetId")
        )
        unexpected = sorted(set(subnet_ids) - set(scope.private_subnet_ids))
        if unexpected:
            raise RuntimeError(f"NACL {nacl_id} also affects out-of-scope subnets: {unexpected}")
        associated_subnets.update(subnet_ids)
        entries = list(nacl.get("Entries", []))
        if not has_baseline_allow(entries, egress=False) or not has_baseline_allow(entries, egress=True):
            raise RuntimeError(f"NACL {nacl_id} lacks the expected ingress/egress rule-100 baseline allows")
        reserved = [entry for entry in entries if int(entry.get("RuleNumber", -1)) == scope.rule_number]
        if not reserved:
            state = "absent"
        elif len(reserved) == 1 and is_exact_entry(reserved[0], scope):
            state = "exact"
        else:
            state = "collision"
        states.append(state)
        summaries.append(
            {
                "network_acl_id": nacl_id,
                "subnet_ids": subnet_ids,
                "reserved_rule_state": state,
                "reserved_entries": reserved,
                "entries": entries,
            }
        )

    if associated_subnets != set(scope.private_subnet_ids):
        raise RuntimeError(
            "C2 private-subnet NACL association mismatch: "
            f"expected {sorted(scope.private_subnet_ids)}, found {sorted(associated_subnets)}"
        )
    if "collision" in states:
        overall = "collision"
    elif all(state == "absent" for state in states):
        overall = "healthy"
    elif all(state == "exact" for state in states):
        overall = "fault1"
    else:
        overall = "partial"
    return {"state": overall, "network_acls": summaries}


def describe_topology(ec2: Any, scope: FaultScope) -> dict[str, Any]:
    response = ec2.describe_network_acls(NetworkAclIds=list(scope.nacl_ids))
    return inspect_topology(scope, response.get("NetworkAcls", []))


def create_parameters(scope: FaultScope, nacl_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    return {
        "NetworkAclId": nacl_id,
        "RuleNumber": scope.rule_number,
        "Protocol": PROTOCOL_NUMBER,
        "RuleAction": "deny",
        "Egress": False,
        "CidrBlock": scope.source_cidr,
        "PortRange": {"From": scope.port, "To": scope.port},
        **({"DryRun": True} if dry_run else {}),
    }


def delete_parameters(scope: FaultScope, nacl_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    return {
        "NetworkAclId": nacl_id,
        "RuleNumber": scope.rule_number,
        "Egress": False,
        **({"DryRun": True} if dry_run else {}),
    }


def expect_dry_run(operation: Callable[..., Any], parameters: dict[str, Any], label: str) -> dict[str, str]:
    try:
        operation(**parameters)
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = str(response.get("Error", {}).get("Code", ""))
        message = str(response.get("Error", {}).get("Message", str(exc)))
        if code == "DryRunOperation":
            return {"operation": label, "status": "authorized", "aws_code": code}
        raise RuntimeError(f"AWS DryRun failed for {label}: {code or type(exc).__name__}: {message}") from exc
    raise RuntimeError(f"AWS unexpectedly executed {label} even though DryRun=True")


def validate_mutation_permissions(ec2: Any, scope: FaultScope) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for nacl_id in scope.nacl_ids:
        results.append(
            expect_dry_run(
                ec2.create_network_acl_entry,
                create_parameters(scope, nacl_id, dry_run=True),
                f"create ingress rule {scope.rule_number} on {nacl_id}",
            )
        )
        results.append(
            expect_dry_run(
                ec2.delete_network_acl_entry,
                delete_parameters(scope, nacl_id, dry_run=True),
                f"delete ingress rule {scope.rule_number} on {nacl_id}",
            )
        )
    return results


def inject_entries(
    ec2: Any,
    scope: FaultScope,
    on_created: Callable[[str], None] | None = None,
) -> list[str]:
    created: list[str] = []
    for nacl_id in scope.nacl_ids:
        ec2.create_network_acl_entry(**create_parameters(scope, nacl_id))
        created.append(nacl_id)
        if on_created:
            on_created(nacl_id)
    return created


def restore_entries(
    ec2: Any,
    scope: FaultScope,
    nacl_ids: Sequence[str],
    on_deleted: Callable[[str], None] | None = None,
) -> list[str]:
    deleted: list[str] = []
    for nacl_id in nacl_ids:
        ec2.delete_network_acl_entry(**delete_parameters(scope, nacl_id))
        deleted.append(nacl_id)
        if on_deleted:
            on_deleted(nacl_id)
    return deleted


def scope_document(scope: FaultScope) -> dict[str, Any]:
    value = asdict(scope)
    value["private_subnet_ids"] = list(scope.private_subnet_ids)
    value["nacl_ids"] = list(scope.nacl_ids)
    return value


def validate_manifest_scope(document: dict[str, Any], scope: FaultScope) -> None:
    if document.get("project") != PROJECT or document.get("fault") != FAULT_NAME:
        raise RuntimeError("Manifest does not belong to this project's Fault 1 tool")
    recorded = document.get("scope")
    if not isinstance(recorded, dict):
        raise RuntimeError("Manifest is missing its recorded fault scope")
    current = scope_document(scope)
    mismatches = [key for key, value in current.items() if recorded.get(key) != value]
    if mismatches:
        raise RuntimeError(f"Current Terraform scope differs from the injection manifest: {mismatches}")
    intended = document.get("intended_nacl_ids")
    if intended != list(scope.nacl_ids):
        raise RuntimeError("Manifest mutation intent does not exactly match the current NACL scope")


def unique_bundle(root: Path, prefix: str, started_at: str) -> Path:
    base = root / f"{prefix}-{utc_stamp(started_at)}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = Path(f"{base}-{suffix}")
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def common_document(action: str, scope: FaultScope, started_at: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": PROJECT,
        "phase": 10,
        "fault": FAULT_NAME,
        "action": action,
        "started_at": started_at,
        "scope": scope_document(scope),
        "expected_entry": expected_entry(scope),
        "intended_nacl_ids": list(scope.nacl_ids),
    }


def global_arguments(profile: str | None, terraform_dir: Path) -> str:
    arguments = f" --profile {profile}" if profile else ""
    normalized = str(terraform_dir)
    if normalized not in ("terraform", ".\\terraform"):
        arguments += f' --terraform-dir "{normalized}"'
    return arguments


def restore_command(script: Path, manifest: Path, profile: str | None, terraform_dir: Path) -> str:
    return (
        f'"{sys.executable}" "{script}"{global_arguments(profile, terraform_dir)} '
        f'restore --manifest "{manifest}" --execute'
    )


def print_scope(scope: FaultScope) -> None:
    print(f"UTC: {utc_now()}")
    print(f"AWS account: {scope.account_id}")
    print(f"Region: {scope.region}")
    print(f"C2 VPC: {scope.vpc_id}")
    print(f"C2 private subnets: {', '.join(scope.private_subnet_ids)}")
    print(f"Target NACLs: {', '.join(scope.nacl_ids)}")
    print(f"Planned entry: ingress DENY {scope.source_cidr} TCP/{scope.port}, rule {scope.rule_number}")


def require_identity(sts: Any, scope: FaultScope) -> dict[str, str]:
    identity = sts.get_caller_identity()
    actual = str(identity.get("Account"))
    if actual != scope.account_id:
        raise RuntimeError(f"AWS caller account {actual} does not match Terraform account {scope.account_id}")
    return {"account_id": actual, "arn": str(identity.get("Arn", ""))}


def command_preflight(args: argparse.Namespace, script: Path) -> int:
    started_at = utc_now()
    scope = load_scope(args.terraform_dir.resolve())
    sts, ec2 = create_boto_clients(args.profile, scope)
    identity = require_identity(sts, scope)
    topology = describe_topology(ec2, scope)
    if topology["state"] != "healthy":
        raise RuntimeError(
            f"Preflight requires an absent reserved rule on every NACL; observed state: {topology['state']}"
        )
    permissions = validate_mutation_permissions(ec2, scope)

    rehearsal = common_document("inject", scope, started_at)
    rehearsal["status"] = "REHEARSAL"
    validate_manifest_scope(rehearsal, scope)

    bundle = unique_bundle(args.output_root.resolve(), "preflight", started_at)
    document = common_document("preflight", scope, started_at)
    document.update(
        {
            "completed_at": utc_now(),
            "outcome": "PASS",
            "live_fault_injected": False,
            "identity": identity,
            "topology": topology,
            "dry_run_permissions": permissions,
            "restoration_validation": {
                "status": "PASS",
                "detail": "Manifest scope validation and AWS delete-entry DryRun authorization passed for every target NACL.",
            },
        }
    )
    manifest = bundle / "manifest.json"
    write_json_atomic(manifest, document)
    print_scope(scope)
    print("Preflight: PASS (no live NACL entry was created)")
    print("Live fault injected: false")
    print(f"Evidence: {manifest}")
    print(
        "Injection command (run only when the live incident is authorized): "
        f'"{sys.executable}" "{script}"{global_arguments(args.profile, args.terraform_dir)} inject --execute'
    )
    return 0


def find_matching_injection(output_root: Path, scope: FaultScope) -> Path | None:
    candidates = sorted(output_root.glob("fault1-*/injection.json"), reverse=True)
    for candidate in candidates:
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
            validate_manifest_scope(document, scope)
            if document.get("status") == "INJECTED":
                return candidate
        except (OSError, json.JSONDecodeError, RuntimeError):
            continue
    return None


def command_inject(args: argparse.Namespace, script: Path) -> int:
    if not args.execute:
        raise RuntimeError("Refusing live fault injection without --execute; run the preflight command first")
    started_at = utc_now()
    scope = load_scope(args.terraform_dir.resolve())
    sts, ec2 = create_boto_clients(args.profile, scope)
    identity = require_identity(sts, scope)
    topology = describe_topology(ec2, scope)

    if topology["state"] == "fault1":
        existing = find_matching_injection(args.output_root.resolve(), scope)
        if existing is None:
            raise RuntimeError(
                "The exact Fault 1 entry is already active, but no matching local injection manifest proves ownership; refusing to adopt it"
            )
        print_scope(scope)
        print("Injection is already active; no AWS mutation was performed.")
        print(f"Restore with: {restore_command(script, existing, args.profile, args.terraform_dir)}")
        return 0
    if topology["state"] != "healthy":
        raise RuntimeError(
            f"Reserved rule collision or partial state detected ({topology['state']}); no entry was created"
        )

    permissions = validate_mutation_permissions(ec2, scope)
    bundle = unique_bundle(args.output_root.resolve(), "fault1", started_at)
    manifest = bundle / "injection.json"
    document = common_document("inject", scope, started_at)
    document.update(
        {
            "status": "INJECTING",
            "identity": identity,
            "pre_injection_topology": topology,
            "dry_run_permissions": permissions,
            "created_nacl_ids": [],
            "restore_command": restore_command(script, manifest, args.profile, args.terraform_dir),
        }
    )
    write_json_atomic(manifest, document)

    def record_created(nacl_id: str) -> None:
        document["created_nacl_ids"].append(nacl_id)
        document["last_updated_at"] = utc_now()
        write_json_atomic(manifest, document)

    try:
        inject_entries(ec2, scope, record_created)
        post = describe_topology(ec2, scope)
        if post["state"] != "fault1":
            raise RuntimeError(f"Post-injection verification returned unexpected state: {post['state']}")
    except Exception as injection_error:
        rollback_errors: list[str] = []
        rollback_ids = list(reversed(document["created_nacl_ids"]))
        try:
            failed_topology = describe_topology(ec2, scope)
            rollback_ids = list(
                dict.fromkeys(
                    rollback_ids
                    + [
                        item["network_acl_id"]
                        for item in failed_topology["network_acls"]
                        if item["reserved_rule_state"] == "exact"
                    ]
                )
            )
        except Exception as exc:
            rollback_errors.append(f"unable to discover rollback scope: {exc}")
        for nacl_id in rollback_ids:
            try:
                ec2.delete_network_acl_entry(**delete_parameters(scope, nacl_id))
            except Exception as exc:
                rollback_errors.append(f"{nacl_id}: {exc}")
        document.update(
            {
                "completed_at": utc_now(),
                "status": "MANUAL_RESTORE_REQUIRED" if rollback_errors else "ROLLED_BACK",
                "outcome": "FAIL",
                "error": str(injection_error),
                "rollback_errors": rollback_errors,
            }
        )
        write_json_atomic(manifest, document)
        if rollback_errors:
            raise RuntimeError(
                f"Injection failed and automatic rollback was incomplete. Manifest: {manifest}. "
                f"Run: {document['restore_command']}"
            ) from injection_error
        raise RuntimeError(f"Injection failed; all entries created by this run were rolled back. Manifest: {manifest}") from injection_error

    document.update(
        {
            "completed_at": utc_now(),
            "status": "INJECTED",
            "outcome": "PASS",
            "post_injection_topology": post,
        }
    )
    write_json_atomic(manifest, document)
    print_scope(scope)
    print(f"Injection: PASS at {document['completed_at']}")
    print(f"Changed NACLs: {', '.join(document['created_nacl_ids'])}")
    print(f"Evidence: {manifest}")
    print(f"Restore with: {document['restore_command']}")
    return 0


def command_restore(args: argparse.Namespace, script: Path) -> int:
    started_at = utc_now()
    try:
        injection = json.loads(args.manifest.resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read injection manifest {args.manifest}: {exc}") from exc
    if injection.get("action") != "inject":
        raise RuntimeError("Restore requires an injection manifest, not a preflight or restoration manifest")

    scope = load_scope(args.terraform_dir.resolve())
    validate_manifest_scope(injection, scope)
    sts, ec2 = create_boto_clients(args.profile, scope)
    identity = require_identity(sts, scope)
    topology = describe_topology(ec2, scope)
    if topology["state"] == "collision":
        raise RuntimeError("Reserved rule collision detected; refusing to delete any NACL entry")
    if topology["state"] not in ("healthy", "fault1", "partial"):
        raise RuntimeError(f"Unsupported restoration state: {topology['state']}")

    exact_ids = [
        item["network_acl_id"]
        for item in topology["network_acls"]
        if item["reserved_rule_state"] == "exact"
    ]
    permissions: list[dict[str, str]] = []
    for nacl_id in exact_ids:
        permissions.append(
            expect_dry_run(
                ec2.delete_network_acl_entry,
                delete_parameters(scope, nacl_id, dry_run=True),
                f"delete ingress rule {scope.rule_number} on {nacl_id}",
            )
        )

    if not args.execute:
        print_scope(scope)
        print(f"Restore preview: PASS; exact entries eligible for deletion: {', '.join(exact_ids) or 'none'}")
        print("No AWS mutation was performed. Add --execute to restore.")
        return 0

    bundle = args.manifest.resolve().parent
    restoration = bundle / f"restoration-{utc_stamp(started_at)}.json"
    document = common_document("restore", scope, started_at)
    document.update(
        {
            "status": "RESTORING",
            "identity": identity,
            "injection_manifest": str(args.manifest.resolve()),
            "pre_restore_topology": topology,
            "dry_run_permissions": permissions,
            "eligible_nacl_ids": exact_ids,
            "deleted_nacl_ids": [],
        }
    )
    write_json_atomic(restoration, document)

    def record_deleted(nacl_id: str) -> None:
        document["deleted_nacl_ids"].append(nacl_id)
        document["last_updated_at"] = utc_now()
        write_json_atomic(restoration, document)

    restore_entries(ec2, scope, exact_ids, record_deleted)
    post = describe_topology(ec2, scope)
    if post["state"] != "healthy":
        document.update(
            {
                "completed_at": utc_now(),
                "status": "RESTORE_INCOMPLETE",
                "outcome": "FAIL",
                "post_restore_topology": post,
            }
        )
        write_json_atomic(restoration, document)
        raise RuntimeError(f"Restoration verification failed; evidence: {restoration}")

    document.update(
        {
            "completed_at": utc_now(),
            "status": "RESTORED" if exact_ids else "ALREADY_RESTORED",
            "outcome": "PASS",
            "post_restore_topology": post,
        }
    )
    write_json_atomic(restoration, document)
    print_scope(scope)
    print(f"Restoration: PASS at {document['completed_at']}")
    print(f"Changed NACLs: {', '.join(document['deleted_nacl_ids']) or 'none (already restored)'}")
    print(f"Evidence: {restoration}")
    verifier_profile = f" --profile {args.profile}" if args.profile else ""
    print(
        "Verify recovery with: "
        f'python "{Path("verification/verify.py")}" --state restored{verifier_profile}'
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight, inject, or restore the Phase 10 C1-to-C2 TCP/7070 NACL fault."
    )
    parser.add_argument("--terraform-dir", type=Path, default=Path("terraform"))
    parser.add_argument("--profile", help="Optional boto3 shared-profile name")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("evidence/generated/phase10"),
        help="Generated evidence root (default: evidence/generated/phase10)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="Read state and DryRun both mutation APIs; never inject")
    inject = subparsers.add_parser("inject", help="Create the exact ingress deny on every scoped NACL")
    inject.add_argument("--execute", action="store_true", help="Required acknowledgement for live injection")
    restore = subparsers.add_parser("restore", help="Delete only exact entries bound to an injection manifest")
    restore.add_argument("--manifest", type=Path, required=True, help="Path to the injection.json journal")
    restore.add_argument("--execute", action="store_true", help="Perform deletion after the safe preview checks")
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
