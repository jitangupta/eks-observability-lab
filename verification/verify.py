#!/usr/bin/env python3
"""Scoped AWS and Kubernetes verifier for the EKS Observability Lab."""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


PROJECT = "eks-observability-lab"
APPLICATION_NAMESPACE = "online-boutique"
EXPECTED_ALB_NAME = f"{PROJECT}-c1-web"
EXPECTED_NLB_NAME = f"{PROJECT}-c2-cart"
FAULT_STATES = ("healthy", "fault1", "restored")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def is_world_cidr(cidr: str) -> bool:
    try:
        return ipaddress.ip_network(cidr, strict=False).prefixlen == 0
    except ValueError:
        return False


def endpoint_cidrs_are_restricted(cidrs: Sequence[str]) -> bool:
    if not cidrs:
        return False
    try:
        networks = [ipaddress.ip_network(cidr, strict=False) for cidr in cidrs]
    except ValueError:
        return False
    return all(
        network.version == 4 and network.prefixlen >= 24 and not is_world_cidr(str(network))
        for network in networks
    )


def find_nacl_fault_entries(
    entries: Sequence[dict[str, Any]], rule_number: int
) -> list[dict[str, Any]]:
    return [entry for entry in entries if int(entry.get("RuleNumber", -1)) == rule_number]


def is_exact_fault_entry(entry: dict[str, Any], source_cidr: str) -> bool:
    port_range = entry.get("PortRange") or {}
    return (
        entry.get("Egress") is False
        and str(entry.get("Protocol")) == "6"
        and entry.get("RuleAction") == "deny"
        and entry.get("CidrBlock") == source_cidr
        and int(port_range.get("From", -1)) == 7070
        and int(port_range.get("To", -1)) == 7070
    )


def has_permission(
    permissions: Sequence[dict[str, Any]],
    *,
    protocol: str,
    from_port: int | None,
    to_port: int | None,
    cidr: str,
) -> bool:
    for permission in permissions:
        if str(permission.get("IpProtocol")) != protocol:
            continue
        if permission.get("FromPort") != from_port or permission.get("ToPort") != to_port:
            continue
        if any(item.get("CidrIp") == cidr for item in permission.get("IpRanges", [])):
            return True
    return False


def permission_is_exact(
    permission: dict[str, Any],
    *,
    protocol: str,
    from_port: int | None,
    to_port: int | None,
    cidr: str,
) -> bool:
    ranges = permission.get("IpRanges", [])
    return (
        str(permission.get("IpProtocol")) == protocol
        and permission.get("FromPort") == from_port
        and permission.get("ToPort") == to_port
        and len(ranges) == 1
        and ranges[0].get("CidrIp") == cidr
        and not permission.get("Ipv6Ranges")
        and not permission.get("PrefixListIds")
        and not permission.get("UserIdGroupPairs")
    )


def public_ingress_sources(permissions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for permission in permissions:
        for item in permission.get("IpRanges", []):
            if is_world_cidr(item.get("CidrIp", "")):
                found.append({"protocol": permission.get("IpProtocol"), "cidr": item.get("CidrIp")})
        for item in permission.get("Ipv6Ranges", []):
            if is_world_cidr(item.get("CidrIpv6", "")):
                found.append({"protocol": permission.get("IpProtocol"), "cidr": item.get("CidrIpv6")})
    return found


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(
        self,
        args: Sequence[str | Path],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 180,
        check: bool = True,
    ) -> CommandResult:
        rendered = [str(item) for item in args]
        try:
            completed = subprocess.run(
                rendered,
                cwd=cwd,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Required command is not installed or not on PATH: {rendered[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(rendered)}") from exc

        result = CommandResult(rendered, completed.returncode, completed.stdout, completed.stderr)
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "no command output"
            raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(rendered)}\n{message}")
        return result


class Reporter:
    def __init__(self, state: str, config_only: bool) -> None:
        self.started_at = utc_now()
        self.state = state
        self.config_only = config_only
        self.checks: list[dict[str, Any]] = []

    def add(
        self,
        check_id: str,
        name: str,
        status: str,
        detail: str,
        evidence: Any | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "id": check_id,
            "name": name,
            "status": status,
            "detail": detail,
        }
        if evidence is not None:
            item["evidence"] = evidence
        self.checks.append(item)
        print(f"[{status}] {check_id}: {detail}")

    def run_check(
        self,
        check_id: str,
        name: str,
        check: Callable[[], tuple[str, Any]],
    ) -> None:
        try:
            detail, evidence = check()
            self.add(check_id, name, "PASS", detail, evidence)
        except Exception as exc:  # Each independent check must remain visible in the report.
            self.add(check_id, name, "FAIL", str(exc))

    def skip(self, check_id: str, name: str, detail: str) -> None:
        self.add(check_id, name, "SKIP", detail)

    def document(self) -> dict[str, Any]:
        counts = {status: sum(item["status"] == status for item in self.checks) for status in ("PASS", "FAIL", "SKIP")}
        if counts["FAIL"]:
            outcome = "FAIL"
        elif counts["SKIP"]:
            outcome = "INCOMPLETE"
        else:
            outcome = "PASS"
        return {
            "schema_version": 1,
            "project": PROJECT,
            "state": self.state,
            "mode": "configuration-only" if self.config_only else "full",
            "started_at": self.started_at,
            "completed_at": utc_now(),
            "outcome": outcome,
            "summary": counts,
            "checks": self.checks,
        }


@dataclass(frozen=True)
class DeploymentContext:
    account_id: str
    clusters: dict[str, Any]
    networking: dict[str, Any]
    security: dict[str, Any]

    @property
    def c1(self) -> dict[str, Any]:
        return self.clusters["c1"]

    @property
    def c2(self) -> dict[str, Any]:
        return self.clusters["c2"]


def load_terraform_context(runner: CommandRunner, terraform_dir: Path) -> DeploymentContext:
    result = runner.run(["terraform", f"-chdir={terraform_dir}", "output", "-json"], timeout=60)
    try:
        raw = json.loads(result.stdout)
        values = {name: item["value"] for name, item in raw.items()}
        return DeploymentContext(
            account_id=str(values["account_id"]),
            clusters=values["clusters"],
            networking=values["networking"],
            security=values["security"],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Terraform outputs are missing or do not match the Phase 1 contract") from exc


def create_boto_session(profile: str | None) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is not installed; run: python -m pip install -r verification/requirements.txt"
        ) from exc

    kwargs = {"profile_name": profile} if profile else {}
    return boto3.Session(**kwargs), Config(
        connect_timeout=10,
        read_timeout=30,
        retries={"max_attempts": 4, "mode": "standard"},
        user_agent_extra="eks-observability-lab-phase8-verifier",
    )


def paginate(client: Any, operation: str, result_key: str, **kwargs: Any) -> list[dict[str, Any]]:
    paginator = client.get_paginator(operation)
    return [item for page in paginator.paginate(**kwargs) for item in page.get(result_key, [])]


class AwsChecks:
    def __init__(self, session: Any, boto_config: Any, context: DeploymentContext, state: str) -> None:
        self.context = context
        self.state = state
        self.clients: dict[tuple[str, str], Any] = {}
        self.session = session
        self.boto_config = boto_config
        self.load_balancers: dict[str, dict[str, Any]] = {}

    def client(self, service: str, region: str) -> Any:
        key = (service, region)
        if key not in self.clients:
            self.clients[key] = self.session.client(service, region_name=region, config=self.boto_config)
        return self.clients[key]

    def identity(self) -> tuple[str, Any]:
        actual = str(self.client("sts", self.context.c1["region"]).get_caller_identity()["Account"])
        if actual != self.context.account_id:
            raise RuntimeError(f"AWS caller account {actual} does not match Terraform account {self.context.account_id}")
        return f"AWS caller is in Terraform account {actual}", {"account_id": actual}

    def _regional_load_balancers(self, side: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cluster = self.context.clusters[side]
        vpc_id = self.context.networking[side]["vpc_id"]
        elbv2 = self.client("elbv2", cluster["region"])
        modern = [
            lb for lb in paginate(elbv2, "describe_load_balancers", "LoadBalancers")
            if lb.get("VpcId") == vpc_id
        ]
        classic = [
            lb for lb in paginate(self.client("elb", cluster["region"]), "describe_load_balancers", "LoadBalancerDescriptions")
            if lb.get("VPCId") == vpc_id
        ]
        return modern, classic

    def load_balancer_allowlist(self) -> tuple[str, Any]:
        c1_lbs, c1_classic = self._regional_load_balancers("c1")
        c2_lbs, c2_classic = self._regional_load_balancers("c2")
        all_lbs = c1_lbs + c2_lbs
        unexpected = [lb["LoadBalancerName"] for lb in all_lbs if lb["LoadBalancerName"] not in {EXPECTED_ALB_NAME, EXPECTED_NLB_NAME}]
        if c1_classic or c2_classic:
            unexpected.extend(lb["LoadBalancerName"] for lb in c1_classic + c2_classic)
        if unexpected:
            raise RuntimeError(f"Unexpected load balancers exist in the scoped VPCs: {sorted(unexpected)}")

        by_name = {lb["LoadBalancerName"]: lb for lb in all_lbs}
        missing = sorted({EXPECTED_ALB_NAME, EXPECTED_NLB_NAME} - set(by_name))
        if missing:
            raise RuntimeError(f"Expected load balancers are missing: {missing}")
        alb, nlb = by_name[EXPECTED_ALB_NAME], by_name[EXPECTED_NLB_NAME]

        errors: list[str] = []
        if alb.get("VpcId") != self.context.networking["c1"]["vpc_id"] or alb.get("Type") != "application" or alb.get("Scheme") != "internet-facing":
            errors.append("C1 ALB is not an internet-facing application LB in the C1 VPC")
        if nlb.get("VpcId") != self.context.networking["c2"]["vpc_id"] or nlb.get("Type") != "network" or nlb.get("Scheme") != "internal":
            errors.append("C2 cart NLB is not an internal network LB in the C2 VPC")
        if alb.get("State", {}).get("Code") != "active":
            errors.append(f"C1 ALB state is {alb.get('State', {}).get('Code')}, not active")
        if nlb.get("State", {}).get("Code") != "active":
            errors.append(f"C2 NLB state is {nlb.get('State', {}).get('Code')}, not active")
        if {zone["SubnetId"] for zone in alb.get("AvailabilityZones", [])} != set(self.context.networking["c1"]["public_subnet_ids"]):
            errors.append("C1 ALB subnet set differs from Terraform public subnets")
        if {zone["SubnetId"] for zone in nlb.get("AvailabilityZones", [])} != set(self.context.networking["c2"]["private_subnet_ids"]):
            errors.append("C2 NLB subnet set differs from Terraform private subnets")
        if self.context.security["c1_alb_security_group_id"] not in alb.get("SecurityGroups", []):
            errors.append("C1 ALB does not use the Terraform ALB security group")
        if self.context.security["c2_cart_nlb_security_group_id"] not in nlb.get("SecurityGroups", []):
            errors.append("C2 NLB does not use the Terraform cart security group")
        if errors:
            raise RuntimeError("; ".join(errors))

        self.load_balancers = {"alb": alb, "nlb": nlb}
        evidence = {
            key: {
                "name": lb["LoadBalancerName"],
                "arn": lb["LoadBalancerArn"],
                "dns_name": lb["DNSName"],
                "scheme": lb["Scheme"],
                "type": lb["Type"],
                "state": lb.get("State", {}).get("Code"),
            }
            for key, lb in self.load_balancers.items()
        }
        return "The scoped VPCs contain exactly the expected public ALB and internal NLB", evidence

    def target_health(self) -> tuple[str, Any]:
        if not self.load_balancers:
            raise RuntimeError("Load-balancer discovery did not pass, so target health cannot be attributed safely")
        evidence: dict[str, Any] = {}
        errors: list[str] = []
        for key, side in (("alb", "c1"), ("nlb", "c2")):
            lb = self.load_balancers[key]
            client = self.client("elbv2", self.context.clusters[side]["region"])
            groups = client.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
            states: list[dict[str, Any]] = []
            for group in groups:
                descriptions = client.describe_target_health(TargetGroupArn=group["TargetGroupArn"])["TargetHealthDescriptions"]
                states.extend(
                    {
                        "target_group_arn": group["TargetGroupArn"],
                        "target_id": item["Target"]["Id"],
                        "port": item["Target"].get("Port"),
                        "state": item["TargetHealth"]["State"],
                        "reason": item["TargetHealth"].get("Reason"),
                    }
                    for item in descriptions
                )
            evidence[key] = states
            if not states:
                errors.append(f"{key.upper()} has no registered targets")
            unhealthy = [item for item in states if item["state"] != "healthy"]
            if unhealthy:
                errors.append(f"{key.upper()} has non-healthy targets: {compact(unhealthy)}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return "All registered ALB and NLB targets are healthy", evidence

    def waf(self) -> tuple[str, Any]:
        if "alb" not in self.load_balancers:
            raise RuntimeError("Expected ALB was not safely discovered")
        waf = self.client("wafv2", self.context.c1["region"])
        expected_arn = self.context.security["waf_web_acl_arn"]
        associated = waf.get_web_acl_for_resource(ResourceArn=self.load_balancers["alb"]["LoadBalancerArn"]).get("WebACL")
        if not associated or associated.get("ARN") != expected_arn:
            raise RuntimeError(f"Expected WAF {expected_arn} is not associated with the C1 ALB")
        resource = expected_arn.rsplit("/", 2)
        if len(resource) != 3:
            raise RuntimeError(f"Terraform WAF ARN has an unexpected shape: {expected_arn}")
        acl = waf.get_web_acl(Name=resource[-2], Scope="REGIONAL", Id=resource[-1])["WebACL"]
        rules = acl.get("Rules", [])
        if not rules:
            raise RuntimeError("Associated WAF WebACL contains no rules")
        return f"The expected WAF is associated with the ALB and has {len(rules)} rules", {
            "web_acl_arn": expected_arn,
            "rules": [{"name": rule["Name"], "priority": rule["Priority"]} for rule in rules],
        }

    def worker_nodes(self) -> tuple[str, Any]:
        evidence: dict[str, Any] = {}
        errors: list[str] = []
        for side in ("c1", "c2"):
            ec2 = self.client("ec2", self.context.clusters[side]["region"])
            reservations = paginate(
                ec2,
                "describe_instances",
                "Reservations",
                Filters=[
                    {"Name": "vpc-id", "Values": [self.context.networking[side]["vpc_id"]]},
                    {"Name": "tag:eks:cluster-name", "Values": [self.context.clusters[side]["name"]]},
                    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
                ],
            )
            instances = [instance for reservation in reservations for instance in reservation.get("Instances", [])]
            evidence[side] = [
                {
                    "instance_id": item["InstanceId"],
                    "private_ip": item.get("PrivateIpAddress"),
                    "public_ip": item.get("PublicIpAddress"),
                    "subnet_id": item.get("SubnetId"),
                    "state": item["State"]["Name"],
                }
                for item in instances
            ]
            if not instances:
                errors.append(f"No managed worker instances were found for {side}")
            invalid = [
                item["InstanceId"] for item in instances
                if item.get("PublicIpAddress") or item.get("SubnetId") not in self.context.networking[side]["private_subnet_ids"]
            ]
            if invalid:
                errors.append(f"{side} workers have a public IP or are outside private subnets: {invalid}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return "All scoped EKS workers are in Terraform private subnets without public IPs", evidence

    def security_groups(self) -> tuple[str, Any]:
        evidence: dict[str, Any] = {}
        errors: list[str] = []
        definitions = {
            "c1_alb": ("c1", self.context.security["c1_alb_security_group_id"]),
            "c1_node": ("c1", self.context.c1["node_security_group_id"]),
            "c1_cluster": ("c1", self.context.c1["cluster_security_group_id"]),
            "c2_nlb": ("c2", self.context.security["c2_cart_nlb_security_group_id"]),
            "c2_node": ("c2", self.context.c2["node_security_group_id"]),
            "c2_cluster": ("c2", self.context.c2["cluster_security_group_id"]),
        }
        groups: dict[str, dict[str, Any]] = {}
        for label, (side, group_id) in definitions.items():
            group = self.client("ec2", self.context.clusters[side]["region"]).describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
            groups[label] = group
            evidence[label] = {
                "group_id": group_id,
                "vpc_id": group["VpcId"],
                "ingress": group.get("IpPermissions", []),
                "egress": group.get("IpPermissionsEgress", []),
            }
            if group["VpcId"] != self.context.networking[side]["vpc_id"]:
                errors.append(f"{label} belongs to the wrong VPC")

        alb_ingress = groups["c1_alb"].get("IpPermissions", [])
        for permission in alb_ingress:
            if permission.get("IpProtocol") != "tcp" or permission.get("FromPort") not in (80, 443) or permission.get("ToPort") != permission.get("FromPort"):
                errors.append(f"C1 ALB SG has unexpected ingress: {compact(permission)}")
            if permission.get("UserIdGroupPairs") or permission.get("PrefixListIds") or permission.get("Ipv6Ranges"):
                errors.append(f"C1 ALB SG has a non-IPv4 ingress source: {compact(permission)}")
        if not any(permission.get("FromPort") == 80 for permission in alb_ingress):
            errors.append("C1 ALB SG has no HTTP listener ingress")
        alb_egress = groups["c1_alb"].get("IpPermissionsEgress", [])
        if len(alb_egress) != 1 or not permission_is_exact(
            alb_egress[0], protocol="-1", from_port=None, to_port=None, cidr=self.context.networking["c1"]["cidr"]
        ):
            errors.append("C1 ALB SG egress is not exactly all protocols to the C1 VPC CIDR")

        nlb_ingress = groups["c2_nlb"].get("IpPermissions", [])
        if len(nlb_ingress) != 1 or not permission_is_exact(
            nlb_ingress[0], protocol="tcp", from_port=7070, to_port=7070, cidr=self.context.networking["c1"]["cidr"]
        ):
            errors.append("C2 NLB SG ingress is not exactly TCP/7070 from the C1 CIDR")
        nlb_egress = groups["c2_nlb"].get("IpPermissionsEgress", [])
        if len(nlb_egress) != 1 or not permission_is_exact(
            nlb_egress[0], protocol="tcp", from_port=7070, to_port=7070, cidr=self.context.networking["c2"]["cidr"]
        ):
            errors.append("C2 NLB SG egress is not exactly TCP/7070 to the C2 CIDR")

        for label in ("c1_node", "c1_cluster", "c2_node", "c2_cluster"):
            public = public_ingress_sources(groups[label].get("IpPermissions", []))
            if public:
                errors.append(f"{label} has world-addressable ingress: {compact(public)}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return "ALB/NLB rules match the narrow design and EKS security groups have no world ingress", evidence

    def routes_and_peering(self) -> tuple[str, Any]:
        peering_id = self.context.networking["peering_connection_id"]
        peering = self.client("ec2", self.context.c1["region"]).describe_vpc_peering_connections(
            VpcPeeringConnectionIds=[peering_id]
        )["VpcPeeringConnections"][0]
        errors: list[str] = []
        if peering["Status"]["Code"] != "active":
            errors.append(f"VPC peering is {peering['Status']['Code']}, not active")
        evidence: dict[str, Any] = {"peering_connection_id": peering_id, "peering_status": peering["Status"]["Code"]}
        for side, peer_side in (("c1", "c2"), ("c2", "c1")):
            route_tables = self.client("ec2", self.context.clusters[side]["region"]).describe_route_tables(
                RouteTableIds=self.context.networking[side]["private_route_table_ids"]
            )["RouteTables"]
            summarized: list[dict[str, Any]] = []
            for table in route_tables:
                peer_routes = [route for route in table["Routes"] if route.get("DestinationCidrBlock") == self.context.networking[peer_side]["cidr"]]
                if len(peer_routes) != 1 or peer_routes[0].get("VpcPeeringConnectionId") != peering_id or peer_routes[0].get("State") != "active":
                    errors.append(f"{side} route table {table['RouteTableId']} lacks the active exact peer-CIDR route")
                internet_routes = [route for route in table["Routes"] if route.get("GatewayId", "").startswith("igw-")]
                if internet_routes:
                    errors.append(f"Private route table {table['RouteTableId']} routes directly to an internet gateway")
                summarized.append({
                    "route_table_id": table["RouteTableId"],
                    "routes": [
                        {key: route.get(key) for key in ("DestinationCidrBlock", "GatewayId", "NatGatewayId", "VpcPeeringConnectionId", "State") if route.get(key) is not None}
                        for route in table["Routes"]
                    ],
                })
            evidence[side] = summarized
        if errors:
            raise RuntimeError("; ".join(errors))
        return "VPC peering is active and every private route table has the exact peer route without direct IGW routing", evidence

    def nacls(self) -> tuple[str, Any]:
        expected_rule = int(self.context.security["fault_nacl_rule_number"])
        nacls = self.client("ec2", self.context.c2["region"]).describe_network_acls(
            NetworkAclIds=self.context.networking["c2"]["private_nacl_ids"]
        )["NetworkAcls"]
        errors: list[str] = []
        evidence: list[dict[str, Any]] = []
        associated_subnets: set[str] = set()
        for nacl in nacls:
            associated_subnets.update(association["SubnetId"] for association in nacl.get("Associations", []))
            entries = nacl["Entries"]
            baseline_ingress = any(entry.get("Egress") is False and entry.get("RuleNumber") == 100 and entry.get("RuleAction") == "allow" and str(entry.get("Protocol")) == "-1" and entry.get("CidrBlock") == "0.0.0.0/0" for entry in entries)
            baseline_egress = any(entry.get("Egress") is True and entry.get("RuleNumber") == 100 and entry.get("RuleAction") == "allow" and str(entry.get("Protocol")) == "-1" and entry.get("CidrBlock") == "0.0.0.0/0" for entry in entries)
            if not baseline_ingress or not baseline_egress:
                errors.append(f"NACL {nacl['NetworkAclId']} lacks the expected rule-100 baseline allows")
            fault_entries = find_nacl_fault_entries(entries, expected_rule)
            if self.state == "fault1":
                if len(fault_entries) != 1 or not is_exact_fault_entry(fault_entries[0], self.context.networking["c1"]["cidr"]):
                    errors.append(f"NACL {nacl['NetworkAclId']} lacks the exact active Fault 1 deny")
            elif fault_entries:
                errors.append(f"NACL {nacl['NetworkAclId']} still contains reserved rule {expected_rule} in {self.state} state")
            evidence.append({
                "network_acl_id": nacl["NetworkAclId"],
                "subnet_ids": sorted(association["SubnetId"] for association in nacl.get("Associations", [])),
                "entries": entries,
            })
        expected_subnets = set(self.context.networking["c2"]["private_subnet_ids"])
        if associated_subnets != expected_subnets:
            errors.append(f"C2 private NACL association mismatch: expected {sorted(expected_subnets)}, found {sorted(associated_subnets)}")
        if errors:
            raise RuntimeError("; ".join(errors))
        expectation = "contains the exact Fault 1 deny" if self.state == "fault1" else "has no reserved Fault 1 rule"
        return f"C2 private-subnet NACL baseline is intact and {expectation}", evidence

    def eks_endpoints_and_cni(self) -> tuple[str, Any]:
        errors: list[str] = []
        evidence: dict[str, Any] = {}
        for side in ("c1", "c2"):
            eks = self.client("eks", self.context.clusters[side]["region"])
            cluster = eks.describe_cluster(name=self.context.clusters[side]["name"])["cluster"]
            config = cluster["resourcesVpcConfig"]
            if cluster.get("status") != "ACTIVE":
                errors.append(f"{side} EKS cluster is {cluster.get('status')}, not ACTIVE")
            if config["vpcId"] != self.context.networking[side]["vpc_id"]:
                errors.append(f"{side} EKS cluster belongs to the wrong VPC")
            if set(config.get("subnetIds", [])) != set(self.context.networking[side]["private_subnet_ids"]):
                errors.append(f"{side} EKS control-plane subnet set differs from Terraform private subnets")
            if not config.get("endpointPrivateAccess"):
                errors.append(f"{side} EKS private endpoint access is disabled")
            cidrs = config.get("publicAccessCidrs", [])
            if config.get("endpointPublicAccess") and not endpoint_cidrs_are_restricted(cidrs):
                errors.append(f"{side} EKS public endpoint CIDRs are not restricted to narrow IPv4 ranges: {cidrs}")
            addon = eks.describe_addon(clusterName=cluster["name"], addonName="vpc-cni")["addon"]
            try:
                addon_config = json.loads(addon.get("configurationValues") or "{}")
            except json.JSONDecodeError:
                addon_config = {}
            enabled = addon_config.get("enableNetworkPolicy")
            if addon.get("status") != "ACTIVE" or enabled not in (True, "true", "True"):
                errors.append(f"{side} vpc-cni add-on is not ACTIVE with enableNetworkPolicy=true")
            evidence[side] = {
                "cluster_arn": cluster["arn"],
                "status": cluster["status"],
                "endpoint_private_access": config.get("endpointPrivateAccess"),
                "endpoint_public_access": config.get("endpointPublicAccess"),
                "public_access_cidrs": cidrs,
                "vpc_cni": {"status": addon.get("status"), "version": addon.get("addonVersion"), "configuration": addon_config},
            }
        if errors:
            raise RuntimeError("; ".join(errors))
        return "Both EKS APIs retain private access, narrow public CIDRs, and ACTIVE VPC CNI policy configuration", evidence


class KubernetesChecks:
    def __init__(self, runner: CommandRunner, context: DeploymentContext, probes_path: Path, state: str, timeout: int) -> None:
        self.runner = runner
        self.context = context
        self.probes_path = probes_path
        self.state = state
        self.timeout = timeout

    def kubectl(self, side: str, args: Sequence[str], *, check: bool = True) -> CommandResult:
        return self.runner.run(
            ["kubectl", "--context", self.context.clusters[side]["name"], *args],
            timeout=self.timeout,
            check=check,
        )

    def json_resource(self, side: str, args: Sequence[str]) -> dict[str, Any]:
        result = self.kubectl(side, [*args, "-o", "json"])
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"kubectl returned invalid JSON for {side}: {' '.join(args)}") from exc

    def cni_runtime(self) -> tuple[str, Any]:
        evidence: dict[str, Any] = {}
        errors: list[str] = []
        for side in ("c1", "c2"):
            daemonset = self.json_resource(side, ["--namespace", "kube-system", "get", "daemonset", "aws-node"])
            containers = [item["name"] for item in daemonset["spec"]["template"]["spec"]["containers"]]
            status = daemonset.get("status", {})
            if "aws-eks-nodeagent" not in containers:
                errors.append(f"{side} aws-node DaemonSet lacks aws-eks-nodeagent")
            if status.get("numberReady", 0) != status.get("desiredNumberScheduled", -1) or status.get("numberUnavailable", 0):
                errors.append(f"{side} aws-node DaemonSet is not fully Ready")
            policy_endpoints = self.json_resource(side, ["--namespace", APPLICATION_NAMESPACE, "get", "policyendpoints"])
            if not policy_endpoints.get("items"):
                errors.append(f"{side} has no reconciled PolicyEndpoints in {APPLICATION_NAMESPACE}")
            evidence[side] = {
                "aws_node_containers": containers,
                "desired_nodes": status.get("desiredNumberScheduled"),
                "ready_nodes": status.get("numberReady"),
                "policy_endpoint_count": len(policy_endpoints.get("items", [])),
            }
        if errors:
            raise RuntimeError("; ".join(errors))
        return "The VPC CNI node agent is Ready and has reconciled PolicyEndpoints in both clusters", evidence

    def exposure(self, aws_checks: AwsChecks) -> tuple[str, Any]:
        if not aws_checks.load_balancers:
            raise RuntimeError("Expected load balancers were not safely discovered")
        c1_services = self.json_resource("c1", ["--namespace", APPLICATION_NAMESPACE, "get", "services"])["items"]
        c2_services = self.json_resource("c2", ["--namespace", APPLICATION_NAMESPACE, "get", "services"])["items"]
        c1_lbs = [item["metadata"]["name"] for item in c1_services if item["spec"].get("type") == "LoadBalancer"]
        c2_lbs = [item["metadata"]["name"] for item in c2_services if item["spec"].get("type") == "LoadBalancer"]
        errors: list[str] = []
        if c1_lbs:
            errors.append(f"C1 has unexpected LoadBalancer Services: {c1_lbs}")
        if c2_lbs != ["cartservice-internal"]:
            errors.append(f"C2 LoadBalancer Service allowlist mismatch: {c2_lbs}")

        by_side = {"c1": c1_services, "c2": c2_services}
        for side, services in by_side.items():
            for service in services:
                name = service["metadata"]["name"]
                if name == "redis-cart" and service["spec"].get("type") != "ClusterIP":
                    errors.append(f"{side} Redis Service is not ClusterIP")
                external_ips = service["spec"].get("externalIPs", [])
                if external_ips:
                    errors.append(f"{side} Service {name} has explicit externalIPs: {external_ips}")

        nlb_service = next((item for item in c2_services if item["metadata"]["name"] == "cartservice-internal"), None)
        if nlb_service:
            annotations = nlb_service["metadata"].get("annotations", {})
            if annotations.get("service.beta.kubernetes.io/aws-load-balancer-scheme") != "internal":
                errors.append("C2 cartservice-internal is not annotated internal")
            hostname = ((nlb_service.get("status", {}).get("loadBalancer", {}).get("ingress") or [{}])[0].get("hostname"))
            if hostname != aws_checks.load_balancers["nlb"]["DNSName"]:
                errors.append("C2 Service hostname does not match the allowlisted NLB")

        ingress = self.json_resource("c1", ["--namespace", APPLICATION_NAMESPACE, "get", "ingress", "frontend"])
        ingress_hostname = ((ingress.get("status", {}).get("loadBalancer", {}).get("ingress") or [{}])[0].get("hostname"))
        if ingress_hostname != aws_checks.load_balancers["alb"]["DNSName"]:
            errors.append("C1 frontend Ingress hostname does not match the allowlisted ALB")
        alias = next((item for item in c1_services if item["metadata"]["name"] == "cartservice"), None)
        if not alias or alias["spec"].get("type") != "ExternalName" or alias["spec"].get("externalName") != aws_checks.load_balancers["nlb"]["DNSName"]:
            errors.append("C1 cartservice is not an ExternalName alias to the allowlisted NLB")
        if errors:
            raise RuntimeError("; ".join(errors))
        return "Kubernetes exposure matches the single ALB Ingress and internal cart NLB design", {
            "c1_load_balancer_services": c1_lbs,
            "c2_load_balancer_services": c2_lbs,
            "frontend_ingress_hostname": ingress_hostname,
            "cart_alias": alias["spec"].get("externalName") if alias else None,
        }

    def active_probes(self) -> tuple[str, Any]:
        if not self.probes_path.is_file():
            raise RuntimeError(f"Probe manifest not found: {self.probes_path}")
        context_name = self.context.c1["name"]
        evidence: dict[str, Any] = {"context": context_name}
        try:
            apply_result = self.kubectl("c1", ["apply", "-f", str(self.probes_path)])
            evidence["apply"] = apply_result.stdout.strip()
            for deployment in ("phase8-authorized", "phase8-unauthorized"):
                self.kubectl(
                    "c1",
                    ["--namespace", APPLICATION_NAMESPACE, "rollout", "status", f"deployment/{deployment}", "--timeout", f"{self.timeout}s"],
                )

            outcomes: dict[str, Any] = {}
            for role in ("authorized", "unauthorized"):
                result = self.kubectl(
                    "c1",
                    [
                        "--namespace", APPLICATION_NAMESPACE,
                        "exec", f"deployment/phase8-{role}", "--",
                        "nc", "-z", "-w", "8", "cartservice", "7070",
                    ],
                    check=False,
                )
                outcomes[role] = {
                    "exit_code": result.returncode,
                    "stdout": result.stdout.strip(),
                    "stderr": result.stderr.strip(),
                }
            evidence["outcomes"] = outcomes
            authorized_should_connect = self.state in ("healthy", "restored")
            authorized_connected = outcomes["authorized"]["exit_code"] == 0
            unauthorized_connected = outcomes["unauthorized"]["exit_code"] == 0
            errors: list[str] = []
            if authorized_connected != authorized_should_connect:
                expectation = "connect" if authorized_should_connect else "fail during Fault 1"
                errors.append(f"Authorized C1 probe did not {expectation}")
            if unauthorized_connected:
                errors.append("Unauthorized C1 probe unexpectedly connected to cartservice:7070")
            if errors:
                raise RuntimeError("; ".join(errors))
            authorized_detail = "connected" if authorized_connected else "failed as expected during Fault 1"
            return f"Authorized probe {authorized_detail}; unauthorized probe was denied", evidence
        finally:
            cleanup = self.kubectl(
                "c1",
                ["delete", "-f", str(self.probes_path), "--ignore-not-found", "--wait=true"],
                check=False,
            )
            evidence["cleanup"] = {"exit_code": cleanup.returncode, "output": cleanup.stdout.strip(), "error": cleanup.stderr.strip()}
            if cleanup.returncode != 0:
                raise RuntimeError(f"Probe cleanup failed in {context_name}: {cleanup.stderr.strip() or cleanup.stdout.strip()}")


def write_reports(reporter: Reporter, output_dir: Path) -> tuple[Path, Path, dict[str, Any]]:
    document = reporter.document()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = reporter.started_at.replace(":", "").replace("-", "")
    stem = f"verification-{reporter.state}-{stamp}"
    json_path = output_dir / f"{stem}.json"
    text_path = output_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    lines = [
        f"EKS Observability Lab verification: {document['outcome']}",
        f"State: {document['state']}",
        f"Mode: {document['mode']}",
        f"Started (UTC): {document['started_at']}",
        f"Completed (UTC): {document['completed_at']}",
        f"Summary: PASS={document['summary']['PASS']} FAIL={document['summary']['FAIL']} SKIP={document['summary']['SKIP']}",
        "",
    ]
    for item in document["checks"]:
        lines.append(f"[{item['status']}] {item['id']} - {item['name']}")
        lines.append(f"  {item['detail']}")
        if "evidence" in item:
            lines.append(f"  Evidence: {compact(item['evidence'])}")
        lines.append("")
    text_path.write_text("\n".join(lines), encoding="utf-8")
    return text_path, json_path, document


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    repository_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", choices=FAULT_STATES, default="healthy", help="Expected infrastructure and probe state")
    parser.add_argument("--profile", help="Optional AWS shared-config profile")
    parser.add_argument("--terraform-dir", type=Path, default=repository_root / "terraform")
    parser.add_argument("--output-dir", type=Path, default=repository_root / "evidence" / "generated" / "phase8")
    parser.add_argument("--config-only", action="store_true", help="Run read-only AWS/Kubernetes checks without creating probe Deployments")
    parser.add_argument("--timeout", type=int, default=120, help="Per-command and rollout timeout in seconds")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    reporter = Reporter(args.state, args.config_only)
    runner = CommandRunner()
    context: DeploymentContext | None = None
    session: Any = None
    boto_config: Any = None

    try:
        context = load_terraform_context(runner, args.terraform_dir.resolve())
        reporter.add("inputs", "Terraform deployment scope", "PASS", "Loaded cluster, VPC, subnet, security, WAF, and NACL scope from Terraform outputs", {
            "account_id": context.account_id,
            "clusters": {side: {"name": context.clusters[side]["name"], "region": context.clusters[side]["region"]} for side in ("c1", "c2")},
            "vpc_ids": {side: context.networking[side]["vpc_id"] for side in ("c1", "c2")},
        })
    except Exception as exc:
        reporter.add("inputs", "Terraform deployment scope", "FAIL", str(exc))

    if context is not None:
        try:
            session, boto_config = create_boto_session(args.profile)
        except Exception as exc:
            reporter.add("aws-sdk", "AWS SDK availability", "FAIL", str(exc))

    if context is not None and session is not None:
        aws = AwsChecks(session, boto_config, context, args.state)
        reporter.run_check("aws-identity", "AWS caller identity", aws.identity)
        reporter.run_check("load-balancers", "Scoped load-balancer allowlist", aws.load_balancer_allowlist)
        reporter.run_check("target-health", "ALB and NLB target health", aws.target_health)
        reporter.run_check("waf", "WAF association and rules", aws.waf)
        reporter.run_check("worker-nodes", "Private EKS workers", aws.worker_nodes)
        reporter.run_check("security-groups", "Security-group boundaries", aws.security_groups)
        reporter.run_check("routes", "Private routes and VPC peering", aws.routes_and_peering)
        reporter.run_check("nacls", f"C2 NACL state ({args.state})", aws.nacls)
        reporter.run_check("eks", "EKS endpoint and VPC CNI configuration", aws.eks_endpoints_and_cni)

        kube = KubernetesChecks(runner, context, Path(__file__).resolve().parent / "probes.yaml", args.state, args.timeout)
        reporter.run_check("cni-runtime", "VPC CNI runtime enforcement", kube.cni_runtime)
        reporter.run_check("kubernetes-exposure", "Kubernetes public/private exposure", lambda: kube.exposure(aws))
        if args.config_only:
            reporter.skip("active-probes", "Authorized and unauthorized C1 probes", "Skipped by --config-only; a full Phase 8 gate requires the short-lived probe Deployments")
        else:
            reporter.run_check("active-probes", "Authorized and unauthorized C1 probes", kube.active_probes)

    try:
        text_path, json_path, document = write_reports(reporter, args.output_dir.resolve())
        print(f"Human report: {text_path}")
        print(f"JSON report:  {json_path}")
        print(f"Outcome: {document['outcome']}")
        return 1 if document["outcome"] == "FAIL" else 0
    except OSError as exc:
        print(f"Unable to write verification reports: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
