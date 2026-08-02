# EKS Observability Lab

This repository contains the implementation and evidence for a two-region Amazon EKS
environment. A user-facing Online Boutique deployment runs across two clusters, with
an explicitly permitted private dependency from Cluster C1 to Cluster C2. The
environment is monitored, deliberately disrupted through network and configuration
faults, and investigated to evidence-backed root causes.

The implementation is intentionally scoped for a 12-hour build window. The priority
is a working, repeatable incident demonstration rather than production-grade breadth.

## Success criteria

- Two independent EKS clusters run in different AWS regions.
- The application has exactly one intentional public path: WAF-protected ALB ingress.
- Worker nodes and the C2 application endpoint have no direct internet exposure.
- C1 `frontend` and `checkoutservice` reach C2 `cartservice` privately.
- Unauthorized C1 workloads cannot use the C2 application path.
- Application, Kubernetes, and AWS infrastructure logs and metrics are observable.
- Alerts send real notifications for both selected faults.
- The verification tool proves intended access and detects unintended exposure.
- Each fault has a repeatable inject/restore workflow and an evidence-backed RCA.

## Selected design

- **Application:** Google Online Boutique.
- **C1:** `us-east-1`; frontend and all services except cart and Redis.
- **C2:** `us-west-2`; `cartservice` and Redis.
- **Private path:** inter-region VPC peering to an internal C2 Network Load Balancer.
- **Public path:** AWS WAF to an internet-facing ALB in C1.
- **Observability:** Grafana Cloud using Fluent Bit for logs and Prometheus
  remote-write for metrics, with AWS CloudWatch integration for ALB, NLB, and WAF
  signals.
- **Fault 1:** stateless NACL deny of C1 traffic to C2 TCP/7070.
- **Fault 2:** invalid memory-limit configuration applied to
  `productcatalogservice`, producing OOMKill and CrashLoopBackOff.
- **Cart datastore:** one resource-limited, in-cluster Redis pod in C2 with ephemeral
  storage and access restricted to `cartservice`.

See [architecture/architecture.md](architecture/architecture.md) for the complete
design and traffic flows.

Follow [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the locked, dependency-
ordered build, validation, fault, evidence, and teardown sequence.

## Repository layout

The initial scaffold is deliberately small. The remaining folders are created in the
implementation session that owns them.

```text
eks-observability-lab/
|-- README.md
|-- IMPLEMENTATION_PLAN.md
|-- architecture/
|   `-- architecture.md
|-- decisions/
|   |-- README.md
|   |-- 001-use-nacl-for-network-fault.md
|   |-- 002-use-grafana-cloud.md
|   `-- 003-use-in-cluster-redis.md
|-- terraform/
|   `-- README.md
`-- archive/
    |-- BRIEF.md
    `-- DECISIONS.md
```

Planned final layout:

```text
eks-observability-lab/
|-- README.md
|-- IMPLEMENTATION_PLAN.md
|-- WRITEUP.md
|-- DEMO.md
|-- AI-LOG.md
|-- architecture/
|-- decisions/
|-- terraform/
|-- kubernetes/
|   |-- charts/
|   |-- c1/
|   |-- c2/
|   |-- network-policies/
|   `-- observability/
|-- verification/
|-- faults/
|-- evidence/
|-- rca/
`-- archive/
```

`kubernetes/` remains broader than `helm-charts/`: it will hold Helm values or charts
alongside NetworkPolicies, namespaces, cross-cluster service wiring, and other small
manifests.

## Session plan

Each implementation area can be completed in a separate Codex session. Sessions
should read this README and the architecture document before making changes.

### 1. Terraform session

Create the AWS foundation:

- Non-overlapping C1 and C2 VPCs with public and private subnets.
- One NAT gateway per VPC as a documented cost/time trade-off.
- EKS clusters and managed node groups in private subnets.
- Inter-region peering and narrow bidirectional routes.
- C1 ALB prerequisites, WAF, and required IAM roles.
- C2 internal NLB security group supplied at NLB creation time.
- One-minute VPC Flow Logs delivered to CloudWatch Logs.
- EKS API endpoints private or public access restricted to the operator CIDR.
- Outputs needed by later sessions: VPC/subnet IDs, cluster names, CIDRs,
  security-group IDs, NACL IDs, and regions.

Do not implement reusable enterprise Terraform abstractions under this deadline.
Prefer small, readable root modules and pinned module/provider versions.

### 2. Kubernetes session

Deploy and split Online Boutique:

- C1 receives all required services except `cartservice` and Redis.
- C2 receives `cartservice` and Redis.
- Create the internal NLB Service in C2 with its security group annotation present
  from the first deployment.
- Wire the C1 `cartservice` DNS name to the internal C2 endpoint.
- Install AWS Load Balancer Controller and create the C1 ALB Ingress.
- Enable EKS VPC CNI NetworkPolicy enforcement explicitly.
- Apply default-deny plus narrowly scoped C1 egress and C2 ingress policies.
- Prove both positive and negative connectivity with temporary test pods.

Use `kubernetes/charts/` only for actual charts or chart wrappers. Keep direct
manifests and policy resources in their functional subfolders.

### 3. Observability session

Add the minimum signals required to investigate both incidents:

- Fluent Bit in both clusters for selected application and Kubernetes logs.
- Prometheus in both clusters with filtered remote-write to Grafana Cloud Metrics.
- CloudWatch integration for ALB, NLB, WAF, and VPC Flow Log investigation.
- A single incident dashboard showing both clusters and the cross-region dependency.
- Alerts for cross-region probe failure, upstream error rate, pod restart/OOM, and
  application availability.
- One-minute evaluation suitable for the live demonstration.
- A real email or other visible notification contact point.
- Canonical Loki labels: `cluster`, `namespace`, `service`, `container`, and `level`.
  Keep pod name and other high-churn Kubernetes metadata as log fields rather than
  indexed labels.

Test notification delivery before relying on any alert.

### 4. Verification session

Build a scoped Python/boto3 verifier that emits human-readable results and JSON and
returns non-zero on failure. It must:

- Allowlist the expected public ALB and expected internal NLB.
- Reject unexpected internet-facing load balancers and public worker nodes.
- Validate WAF association and the presence of at least one rule.
- Inspect security groups, routes, NACLs, and EKS endpoint access.
- Confirm VPC CNI NetworkPolicy enforcement, not just policy-object existence.
- Run an authorized C1-to-C2 request and an unauthorized negative request.
- Support before, during, and after reports for Fault 1.

Scope discovery to this deployment's VPC IDs, cluster names, and tags rather than the
entire AWS account.

### 5. Fault session

Create idempotent Python inject and restore commands.

Fault 1 must add a reserved, high-precedence `DENY` entry for the C1 CIDR and
TCP/7070 to every NACL associated with the C2 NLB subnets. Injection must fail safely
if the rule number is already occupied. Restoration removes only the exact injected
rule.

Fault 2 must patch `productcatalogservice` with a demonstrably invalid memory limit,
then restore the previously captured limit. Describe it as a configuration-pushed
OOM at startup, not as an organic memory leak.

Both workflows must print UTC timestamps, changed resource IDs, validation results,
and the exact restoration command.

### 6. Evidence session

Capture a healthy baseline before injecting either fault. For each incident preserve:

- Injection, first symptom, detection, diagnosis, remediation, and recovery times.
- Alert notifications and dashboard screenshots with readable time axes.
- Relevant application logs, Kubernetes events, and commands.
- NACL configuration, Flow Log rejects, and CloudTrail management event for Fault 1.
- OOM reason, exit code, restarts, limit configuration, and rollout evidence for
  Fault 2.
- Dead ends and the evidence used to reject each hypothesis.
- Verification JSON from before, during, and after the fault.

Use UTC consistently and never rely on Kubernetes events remaining available later.

### 7. RCA and presentation session

Write one RCA per fault with: impact, alert triage, investigation trail, evidence,
root cause, blast radius, timeline, remediation, and preventative actions. Then add:

- `WRITEUP.md` for decisions, trade-offs, tests, and explicitly skipped work.
- `DEMO.md` for the 45-minute demonstration and recovery sequence.
- `AI-LOG.md` describing where AI helped, where it was wrong, and how tests caught it.

The spoken RCA starts with customer impact and root cause; tools appear only as
supporting evidence.

## Scope guardrails

Do not spend the initial build window on a service mesh, Transit Gateway, PrivateLink,
custom Grafana plugins, generalized Terraform modules, a synthetic memory leak, or a
multi-layer SG-plus-NACL fault. These are valid follow-up designs but do not improve
the minimum demonstration enough to justify their delivery risk.
