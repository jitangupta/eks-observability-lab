# RCA: C1-to-C2 cart traffic rejected by C2 Network ACL

## Executive summary

For 8 minutes 42 seconds, the authorized C1 cart caller could not reach the C2 cart
service. The site entry point remained available, but cart-dependent operations were
unavailable. The immediate root cause was a high-precedence ingress rule in the C2
private-subnet Network ACL that denied the C1 VPC CIDR on TCP/7070. Removing only
that rule restored the dependency; the post-recovery verifier passed all 13 checks.

## Impact

- **Customer experience:** the public frontend remained reachable, but the
  cross-region cart dependency was unavailable to its authorized C1 caller.
- **Affected capability:** cart operations using C2 `cartservice` on TCP/7070.
- **Observed incident window:** 2026-08-02 17:12:43Z to 17:21:25Z, a maximum of
  8 minutes 42 seconds from injection to configuration restoration.
- **Recovery validation:** the authorized probe connected, the unauthorized probe
  remained denied, the checkout journey completed, and Grafana returned to zero
  active alerts.
- **Request count:** not measured. This was a controlled lab incident, so no
  production customer or revenue impact is claimed.

## Alert triage

Grafana's `Cross-region cart probe failure` alert began at 17:14:30Z, 1 minute
47 seconds after injection. It correctly identified the broken dependency, but not
the configuration object causing it. The frontend availability probe stayed UP,
which narrowed the incident from a complete C1 ingress outage to a cart-path
failure.

The first investigation priority was therefore the dependency path: C1 caller,
peering routes, security boundaries, C2 NLB, and C2 workloads. The alert was treated
as a symptom and not as proof of root cause.

## Investigation trail

1. **Confirmed the symptom.** The state-aware verifier showed that the authorized
   C1 probe could no longer connect to `cartservice:7070`; the unauthorized probe
   also remained denied as designed.
2. **Rejected a broad application outage.** The frontend probe stayed healthy. All
   10 C1 and 2 C2 application pods were Ready, with zero restarts and zero OOMs.
3. **Rejected load-balancer target failure.** All registered ALB and NLB targets
   were healthy. Kubernetes exposure still matched the intended public ALB and
   internal cart NLB design.
4. **Rejected route and peering failure.** Inter-region peering was active and each
   private route table retained the exact peer-CIDR route.
5. **Rejected security-group and NetworkPolicy drift.** The ALB/NLB and EKS security
   groups retained their narrow rules. The VPC CNI node agent was Ready and had
   reconciled PolicyEndpoints in both clusters.
6. **Found the active configuration change.** The C2 private-subnet NACL contained
   rule 50: ingress `DENY`, source `10.10.0.0/16`, TCP/7070. Its lower rule number
   gave it precedence over the baseline allow rule 100.
7. **Corroborated the cause independently.** VPC Flow Logs contained 18 matching
   TCP/7070 `REJECT` records. CloudTrail recorded the successful
   `CreateNetworkAclEntry` event and its actor at 17:12:44Z.

### Dead ends and how they were rejected

| Hypothesis | Evidence used to reject it |
|---|---|
| C1 frontend or ALB outage | Frontend probe stayed UP and ALB targets were healthy. |
| C2 cart pod or Redis failure | Both C2 application pods remained Ready with no restart or OOM signal. |
| NLB target failure | Registered NLB targets remained healthy. |
| Peering or route-table drift | Peering was active and exact peer routes were present. |
| Security-group regression | The scoped verifier found the intended narrow ALB, NLB, node, and cluster rules. |
| Kubernetes NetworkPolicy failure | The node agent and PolicyEndpoints were healthy; the negative caller remained denied. |

## Evidence

The generated incident bundle is intentionally ignored by Git because it contains
live AWS identifiers. It must be included in the private submission archive or
screen-shared locally.

- [Incident summary](../evidence/generated/phase10/fault1-20260802T171237Z/SUMMARY.txt)
  and [manifest](../evidence/generated/phase10/fault1-20260802T171237Z/manifest.json)
- [Injection journal](../evidence/generated/phase10/fault1-20260802T171237Z/injection.json)
- [Passing fault-state verification](../evidence/generated/phase10/fault1-20260802T171237Z/verification/verification-fault1-20260802T171432Z.json)
- [Ordered investigation record](../evidence/generated/phase10/fault1-20260802T171237Z/investigation/investigation-20260802T172032Z.json)
  and [fault dashboard](../evidence/generated/phase10/fault1-20260802T171237Z/investigation/grafana-fault1-20260802T172032Z.png)
- [FIRING email](../evidence/fault1-grafana-email-firing.png) and
  [RESOLVED email](../evidence/fault1-grafana-email-resolved.png)
- [Restoration journal](../evidence/generated/phase10/fault1-20260802T171237Z/restoration-20260802T172120Z.json)
- [Passing restored verification](../evidence/generated/phase10/fault1-20260802T171237Z/recovery/verification/verification-restored-20260802T172135Z.json)

Two evidence-collection issues did not change the incident conclusion. The first
during-fault verifier attempt could not find the AWS CLI used by the kubeconfig exec
plugin; correcting `PATH` produced a 13/13 pass. The first broad recovery capture
saw an old ALB target still inside its configured 300-second deregistration window;
after it drained, the strict recovery gate passed 9/9.

## Root cause

A high-precedence ingress rule was added to the Network ACL associated with the C2
private NLB subnets. Rule 50 denied the entire C1 CIDR (`10.10.0.0/16`) on TCP/7070,
so the stateless NACL rejected authorized cart traffic before the healthy NLB targets
or cart pods could serve it.

This was a controlled fault injected by the lab operator. CloudTrail identifies the
configuration actor and exact request. It was not caused by pod failure, load-
balancer health, VPC peering, a route-table change, a security-group rule, or
Kubernetes NetworkPolicy.

## Blast radius

- **Affected:** authorized C1 callers of the C2 cart endpoint on TCP/7070 and
  customer workflows depending on that call.
- **Not affected:** public frontend reachability, C1 and C2 pod readiness, Redis,
  ALB/NLB target health, other ports, and the monitoring egress path.
- **Security posture during the fault:** the unauthorized C1 probe remained denied.
  No new public exposure was created.

## Timeline

All timestamps are UTC.

| Time | Event | Elapsed from injection |
|---|---|---:|
| 17:06:04 | Non-mutating preflight passed. | - |
| 17:12:43 | NACL fault injected. | 0:00 |
| 17:12:51 | First matching Flow Log rejection occurred. | 0:08 |
| 17:14:30 | Grafana dependency alert began firing. | 1:47 |
| 17:20:32 | Ordered investigation bundle completed with an unambiguous cause. | 7:49 |
| 17:21:25 | Exact journaled NACL rule removed. | 8:42 |
| 17:22:33 | Restored verifier passed 13/13. | 9:50 |
| 17:31:09 | Strict application and observability recovery passed 9/9. | 18:26 |
| 17:33:14 | A fresh preflight confirmed repeatability and a clean baseline. | 20:31 |

## Remediation

The injector's manifest-bound restore operation deleted only ingress rule 50 from
the NACL recorded in the injection journal. It refused unrelated scope or rule
drift. After deletion, the authorized C1 probe connected again while the negative
probe remained denied. The full user journey, targets, workload health, and quiet
Grafana state were then revalidated.

## Preventative actions

### Implemented in the lab

- Reserve the fault rule number and fail injection on a collision.
- Require a dry-run preflight, explicit `--execute`, exact Terraform scope, and an
  immutable restoration journal.
- Continuously probe the cross-region dependency independently of frontend uptime.
- Retain one-minute VPC Flow Logs and CloudTrail management events.
- Use the verifier before, during, and after the incident to detect configuration
  drift and unintended exposure.

### Recommended for production

- Permit NACL, route, and security-group changes only through reviewed IaC and a
  deployment role; alert on direct mutation through CloudTrail/EventBridge.
- Add an AWS Config or equivalent policy rule for protected subnet NACL invariants.
- Require automated dependency canaries and rollback gates before a network change
  is promoted.
- Use a service-scoped boundary such as PrivateLink when the stronger consumer/
  provider isolation justifies its operational cost.
- Define a target of less than one minute to detect and less than five minutes to
  restore this dependency, then rehearse against those objectives.
