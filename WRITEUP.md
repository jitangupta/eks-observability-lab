# Phase 12 design, trade-offs, and test results

## Deliverable map

| Deliverable | Repository artifact |
|---|---|
| Architecture diagram | [`architecture/corrected-architecture.excalidraw`](architecture/corrected-architecture.excalidraw) and the renderable Mermaid view in [`architecture/architecture.md`](architecture/architecture.md) |
| Infrastructure and configuration | [`terraform/`](terraform/), [`kubernetes/`](kubernetes/), and the accepted decisions in [`decisions/`](decisions/) |
| Python verification tool | [`verification/verify.py`](verification/verify.py), probes in [`verification/probes.yaml`](verification/probes.yaml), and usage in [`verification/README.md`](verification/README.md) |
| RCA per fault | [`rca/fault1-nacl.md`](rca/fault1-nacl.md) and [`rca/fault2-productcatalog-oom.md`](rca/fault2-productcatalog-oom.md) |
| Design and results write-up | This document |
| Live demo | [`DEMO.md`](DEMO.md), with supporting recordings indexed by [`evidence/README.md`](evidence/README.md) |

## Design summary

The environment runs Google Online Boutique across two regional EKS clusters. C1 in
`us-east-1` hosts the public frontend and all application services except cart. C2 in
`us-west-2` hosts `cartservice` and its Redis dependency. An end user has one public
path: AWS WAF to an internet-facing ALB in C1. Workers are private, the C2 NLB is
internal, and Redis is ClusterIP-only.

The only intentional cross-region application dependency is from the policy-labeled
C1 `frontend` and `checkoutservice` workloads to the C2 cart endpoint on TCP/7070.
It travels over inter-region VPC peering, never the public internet. Security groups,
peer-CIDR routes, and enforced Kubernetes NetworkPolicies narrow that path. Temporary
owner-backed probes prove both the positive and negative cases.

Prometheus and Fluent Bit send selected metrics and logs from both clusters to
Grafana Cloud. CloudWatch supplies WAF, ALB/NLB, and VPC Flow Log signals; CloudTrail
supplies configuration-change attribution. The incident dashboard is organized by
the user path and cross-region dependency rather than by isolated tool inventories.

## Important design choices

### Split the application at cart

Cart has a clear gRPC boundary and can remain colocated with Redis in C2. C1 retains
enough upstream callers to demonstrate a cascading dependency failure, while Redis
does not cross the regional link. This creates a meaningful distributed application
without inventing a custom demo service.

### Use VPC peering for the lab

Peering was the fastest low-risk private path for two VPCs. Cross-region security-
group references are not available, so the C2 NLB boundary is CIDR- and port-scoped,
then narrowed again by Kubernetes identity-based NetworkPolicy. PrivateLink would
offer a stronger service-consumer boundary and would be preferred for some
multi-account production designs, but its additional endpoint-service lifecycle was
not justified in a 12-hour build.

### Make exposure a positive allowlist

The verifier expects exactly one internet-facing C1 ALB and one internal C2 NLB in
the Terraform-scoped VPCs. It does not make the weak claim that every LoadBalancer
Service is unsafe. It also checks private workers, EKS endpoint restrictions, WAF
association/rules, routes, security groups, NACLs, target health, and Kubernetes
objects before running authorized and unauthorized probes.

### Use Grafana Cloud rather than host the observability control plane

Hosted Grafana, Loki, metrics, and alerting reduced cluster capacity and setup risk.
The clusters only need outbound telemetry, so no monitoring ingress path is opened.
The trade-off is dependence on an external service and a read-only token for evidence
capture. Tokens are never written into generated bundles.

### Choose faults with different signatures

Fault 1 is a cascading network-configuration incident: the C1 cart probe fails while
pods and load-balancer targets stay healthy, and the cause is a C2 NACL object. Fault
2 is a direct workload-configuration incident: the container reports OOMKilled,
exit 137, restarts, and CrashLoopBackOff. The contrast is intentional. The first
requires correlation across layers; the second tests disciplined confirmation and
blast-radius isolation even when the primary signal is obvious.

## Availability, security, and cost trade-offs

- One managed worker runs in each cluster by default. This controls cost but is not
  highly available and leaves little rollout headroom.
- One NAT gateway per VPC controls lab cost but is a single-AZ egress dependency.
- The public demo endpoint currently uses HTTP behind WAF because no validated ACM
  domain was available in the build window. This is not presented as production-
  equivalent transport security; production requires HTTPS and managed certificate
  lifecycle.
- Redis is one resource-limited pod with ephemeral `emptyDir` storage. Cart data can
  be lost on restart. It is acceptable for the disposable demo, not production.
- Terraform uses local state to avoid backend bootstrap time. A team environment
  requires encrypted remote state and locking.
- Alert evaluation is deliberately one minute for a live demonstration. Production
  thresholds should be derived from SLOs and tuned against real traffic.
- VPC Flow Logs retain only rejects with one-minute aggregation. This lowers cost and
  accelerates network diagnosis but is not a complete traffic archive.

## Test and incident results

The latest captured healthy baseline and both final incident bundles passed. All
times are UTC on 2026-08-02.

| Gate | Result | Evidence |
|---|---|---|
| Healthy infrastructure and isolation | 13/13 verifier checks passed; authorized cart caller connected and unauthorized caller failed. | `evidence/generated/phase8/verification-healthy-20260802T160127Z.json` |
| Healthy application/observability baseline | 9/9 checks passed: user journey, 12 Ready pods, Redis, ALB/NLB/WAF, policy probes, quiet alerts, and dashboard render. | `evidence/generated/phase9/healthy-20260802T164357Z/manifest.json` |
| Fault 1 active | 13/13 state-aware checks passed; authorized cart probe failed while negative isolation remained enforced. | `evidence/generated/phase10/fault1-20260802T171237Z/manifest.json` |
| Fault 1 correlation | Exact NACL deny, 18 matching Flow Log rejects, one successful CloudTrail create event, and a delivered Grafana alert. | [`rca/fault1-nacl.md`](rca/fault1-nacl.md) |
| Fault 1 recovery | Restored verifier passed 13/13 and strict recovery passed 9/9. | `evidence/generated/phase10/fault1-20260802T171237Z/SUMMARY.txt` |
| Fault 2 active | OOMKilled, exit 137, CrashLoopBackOff, five restarts, HTTP 500, and direct Grafana alerts captured; cart positive/negative controls remained correct. | `evidence/generated/phase11/fault2-20260802T181909Z/manifest.json` |
| Fault 2 recovery | Exact 64Mi/128Mi resources and RollingUpdate strategy restored; HTTP 200, Ready rollout, healthy cart, and zero active alerts. | [`rca/fault2-productcatalog-oom.md`](rca/fault2-productcatalog-oom.md) |
| Phase 12 local validation (2026-08-03) | 17 fault tests, 5 verifier tests, and 8 healthy-capture tests passed; Terraform formatting and validation passed. | Reproduce with the commands below. |

Run the local suites without cloud mutation:

```powershell
python -m unittest discover -s .\verification -p "test_*.py" -v
python -m unittest discover -s .\evidence -p "test_*.py" -v
python -m unittest discover -s .\faults -p "test_*.py" -v
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
```

Generated evidence contains live identifiers and is ignored by Git. It should be
included only in the private delivery archive after review. The curated email images
and videos in `evidence/` are presentation aids; the JSON/text bundles are the
authoritative evidence for the numerical claims above.

## Explicitly deferred work

| Deferred item | Reason and production direction |
|---|---|
| Multi-account isolation and AWS Organizations controls | Too much landing-zone work for the exercise. Production should separate environments/accounts and apply SCPs. |
| PrivateLink or Transit Gateway | Peering is sufficient for two VPCs. Reconsider PrivateLink for a service-scoped consumer boundary or TGW for many VPCs. |
| Service mesh and mTLS | The demo is secured at cloud and Kubernetes network layers. Add workload identity and mTLS only with an operational ownership model. |
| Multi-AZ NAT and larger node groups | Omitted for cost. Production requires redundant egress and workload replicas across failure domains. |
| Durable managed Redis | The lab has disposable cart state. Production needs a managed, encrypted, backed-up multi-AZ datastore. |
| Full TLS/domain lifecycle | No validated domain was available. Production must redirect HTTP to HTTPS and automate certificate renewal. |
| Remote Terraform state and CI/CD promotion | Local state and operator-driven apply were faster for a disposable lab. Production needs locked remote state, plan review, policy checks, and staged rollout. |
| Long-term telemetry archive and DR | Seven-day lab retention is enough for the exercise. Production retention, legal requirements, recovery objectives, and replay strategy need separate design. |
| Synthetic memory leak or multi-layer network fault | These add demo risk without improving the required contrast between a remote network cause and a direct workload configuration cause. |

## What the recordings prove—and do not prove

`grafana-dashboard.mp4` shows the observability dashboard state, and
`live-alert.mp4` shows live alert behavior. They support the live-demo deliverable,
but neither recording alone proves root cause, access isolation, or complete fault
investigation. Those claims come from the state-aware verifier and the timestamped
incident bundles, and the live session repeats the Fault 1 investigation using
[`DEMO.md`](DEMO.md).
