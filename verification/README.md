# Phase 8: scoped verification

This verifier implements the root README contract without enumerating the whole AWS
account. Terraform outputs provide the exact account, cluster, VPC, subnet, security
group, WAF, peering, and NACL scope. AWS discovery is then limited to those VPC IDs,
cluster names, and resource identifiers.

The formal healthy gate remains strict. The temporary NodePort fallback is useful
for development but cannot substitute for the expected public ALB, internal NLB,
healthy target groups, WAF association, or C1 `ExternalName` cart alias.

## What it checks

- The AWS caller matches the Terraform account.
- The scoped VPCs contain exactly the expected internet-facing C1 ALB and internal
  C2 NLB, with the intended subnets and Terraform security groups.
- ALB and NLB targets are registered and healthy.
- The Terraform WAF is associated with the ALB and contains at least one rule.
- EKS worker instances have no public IP and remain in Terraform private subnets.
- ALB, NLB, node, and cluster security groups retain the intended boundaries.
- Inter-region peering is active, every private route table has the exact peer-CIDR
  route, and no private route table points directly to an internet gateway.
- C2 private-subnet NACL associations, baseline allows, and reserved Fault 1 rule
  match the selected state.
- EKS public API access is disabled or restricted to narrow IPv4 ranges, private API
  access is enabled, and the `vpc-cni` add-on has NetworkPolicy enabled.
- The `aws-eks-nodeagent` is Ready and has reconciled `PolicyEndpoint` objects in
  both application namespaces.
- Kubernetes Services, Ingress, and the cart alias match the intended exposure.
- A short-lived authorized C1 Deployment has the `frontend` policy identity and an
  unauthorized Deployment has no allowed cart egress. The authorized probe must
  reach `cartservice:7070` when healthy/restored and fail during Fault 1. The
  unauthorized probe must always fail.

The probe manifest uses Deployments rather than standalone Pods because AWS VPC CNI
NetworkPolicy enforcement requires supported owner references. Both Deployments are
deleted in a `finally` cleanup path.

## Prerequisites

From the repository root in PowerShell:

```powershell
python -m pip install -r .\verification\requirements.txt
aws sts get-caller-identity
kubectl config get-contexts
```

Use `--profile NAME` if the Terraform deployment uses a named AWS profile. The
current caller must be able to read STS, EC2, ELBv2, Classic ELB, WAFv2, and EKS
configuration in both regions and must have Kubernetes access to both cluster
contexts named by Terraform. The requirements include AWS CRT support for shared
profiles authenticated with `aws login`. The AWS CLI must be on `PATH` because the
generated kubeconfig contexts use it as their exec credential plugin.

## Run modes

Healthy baseline, including the temporary in-cluster probes:

```powershell
python .\verification\verify.py --state healthy
```

Read-only preflight, which deliberately leaves the active-probe gate incomplete:

```powershell
python .\verification\verify.py --state healthy --config-only
```

Fault 1, Fault 2, and restoration evidence use the same checks with different
explicit expectations. Fault 2 retains a healthy authorized cart probe because its
blast radius is the C1 product catalog workload, not the cross-region network:

```powershell
python .\verification\verify.py --state fault1
python .\verification\verify.py --state fault2
python .\verification\verify.py --state restored
```

Each run writes a UTC-stamped human-readable `.txt` report and structured `.json`
report under `evidence/generated/phase8/`. That directory is ignored because live
AWS identifiers and request evidence should be reviewed before selective
publication. A failing check returns exit code 1; report-write failure returns 2.
`--config-only` reports `INCOMPLETE` rather than claiming the Phase 8 gate passed.

Run deterministic local tests without cloud access:

```powershell
python -m unittest discover -s .\verification -p "test_*.py" -v
```

## Interpreting expected failures

Missing or pending ALB/NLB resources are real Phase 8 failures, even when the
NodePort workaround still carries application traffic. Resolve the AWS account
ELBv2 restriction, remove the fallback as documented in
`kubernetes/c1/README.md`, rerun the target C2 and C1 installers, and only then save
the healthy baseline.

The verifier is diagnostic only except for its two labeled probe Deployments. It
does not repair security groups, routes, NACLs, load balancers, WAF associations, or
cluster configuration.
