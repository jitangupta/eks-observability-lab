# Implementation Decisions & Risk Register

Review of the proposed implementation plan. Companion to [BRIEF.md](BRIEF.md).

**Verdict: adopt the plan as written, with one mandatory change to Fault 1 and six
smaller corrections.** The `cartservice` split and the evidence chain design are both
better than my initial proposal.

---

## 1. Verified: NLB over inter-region peering works

I flagged this as the highest architectural risk — a half-remembered limitation about
internal load balancers being unreachable across inter-region peering. **It does not
exist.** AWS's current VPC peering limitations page lists exactly two inter-region
restrictions, and neither involves load balancers:

- MTU drops to 8500 bytes (vs 9001 same-region)
- You must explicitly enable DNS resolution support on the peering connection to
  resolve peer-VPC private hostnames to private IPs

**The C1 → internal NLB (C2) path is sound. Proceed.**

Two adjacent gotchas from the same page that *do* affect the design:

- **"You cannot connect to or query the Amazon DNS server in a peer VPC."** So the
  private-DNS plan must use a Route 53 **private hosted zone associated with both
  VPCs** (same-account cross-region association is supported), not a pointer at C2's
  resolver.
- Set `allow_remote_vpc_dns_resolution = true` on **both** peering options blocks.

**Simplification worth taking:** an internal NLB's AWS-assigned DNS name is publicly
resolvable but returns **private** IPs — so it's not an exposure, and you don't
strictly need the PHZ. Cleanest zero-app-change wiring:

```yaml
# k8s/c1/cartservice-externalname.yaml
apiVersion: v1
kind: Service
metadata:
  name: cartservice          # matches default CART_SERVICE_ADDR=cartservice:7070
spec:
  type: ExternalName
  externalName: cartservice.internal.<your-zone>   # PHZ CNAME → C2 NLB
```

Stock Online Boutique manifests then work unmodified in C1, and the seam is one
declarative object you can point at anything. Note in the write-up that the resolvable
public DNS name is not a public *path* — an interviewer may probe this.

Sources: [VPC peering limitations](https://docs.aws.amazon.com/vpc/latest/peering/vpc-peering-basics.html)

---

## 2. BLOCKER: Fault 1 as designed will probably do nothing

**This is the most important finding in this review.** Removing the C2 NLB
security-group ingress rule will very likely leave the application **fully working**,
producing zero alerts. Three AWS behaviours compound:

1. Security groups are stateful and connection-tracked. Per AWS: when you change a
   rule, **"its tracked connections are not immediately interrupted"** — the group
   keeps allowing packets until existing connections time out.
2. There is no escaping tracking here. AWS lists connections made through
   **Network Load Balancers** as *automatically tracked*, "even if the security group
   configuration does not otherwise require tracking." The usual untracked-flow
   escape hatch (an open `0.0.0.0/0` rule) does not apply.
3. gRPC holds **long-lived HTTP/2 connections**, and Online Boutique's
   `loadgenerator` keeps them warm, so they never hit the idle timeout — which
   defaults to **432,000s (5 days)** on most instance types.

Net effect: you inject the fault on the live demo, narrate confidently, and the site
stays green. Worst possible failure mode in front of an interviewer.

Sources: [Security group connection tracking](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/security-group-connection-tracking.html)

### The fix: NACL deny, not SG removal

AWS names the remedy directly — network ACLs are stateless, and adding one that
blocks traffic in either direction **breaks existing connections**.

**Fault 1 (revised):** add a `DENY` entry for the C1 CIDR on port 7070 to the C2
private-subnet NACL, at a rule number below the existing allow.

This is strictly better on every axis that matters:

| | SG removal | NACL deny |
|---|---|---|
| Breaks live connections | ✗ tracked, survives | ✓ immediate |
| VPC Flow Log `REJECT` on C2 ENIs | ✓ | ✓ |
| CloudTrail actor + timestamp | ✓ `RevokeSecurityGroupIngress` | ✓ `CreateNetworkAclEntry` |
| C2 pods stay healthy | ✓ | ✓ |
| NLB target health stays green | ✓ | ✓ (health checks are intra-VPC, unaffected by a C1-scoped rule) |
| One-command restore | ✓ | ✓ `DeleteNetworkAclEntry` |
| In assignment scope | ✓ | ✓ §3b names "network ACLs" explicitly |

Everything else about Fault 1 — the symptom set, the innocent-component alerting, the
flow-log/CloudTrail evidence chain — survives unchanged.

### Turn this into your best write-up material

Do not quietly swap the fault. **This is the strongest single story in the
submission**, and it lands directly on §Guidelines-1 ("tell us where AI was wrong and
how you caught it"):

> Designed Fault 1 as a security-group ingress removal. Tested it — the application
> kept serving. Root cause of the *test* failure: NLB connections are automatically
> tracked, and SG changes don't interrupt tracked flows, so a stateful-firewall change
> can't break an established gRPC session. Switched to a stateless NACL deny, which
> AWS documents as the way to interrupt existing connections. Caught by testing the
> fault before trusting it, not by reading the plan.

That paragraph demonstrates: you test your own assumptions, you read primary docs, you
understand stateful vs stateless filtering, and you don't ship unverified AI output.
It is worth more than another hour of Terraform.

**Optional advanced variant (only if you're ahead of schedule):** inject *both* — the
SG revoke and the NACL deny. The investigator finds the SG change first in CloudTrail,
fixes it, and service **doesn't recover** — forcing a second iteration to the NACL.
That's a superb two-layer incident, but it doubles live-demo risk. Ship the single
NACL fault; describe the two-layer variant in the RCA as "how I'd make this harder."

---

## 3. Corrections to the rest of the plan

**3.1 — Flow Logs aggregation interval.** Default delivery is **10 minutes**. On a
45-minute call your flow-log evidence would arrive after the demo ends. Set
`max_aggregation_interval = 60` on both VPCs' flow logs. Enable them **before** any
fault — they are not retroactive.

**3.2 — CloudTrail lag.** Delivery to S3/CloudWatch runs up to ~15 min; the console
**Event History** view is much fresher (~5 min). During the live demo, pull the actor
evidence from Event History, and don't promise a CloudTrail line within 60 seconds of
injection.

**3.3 — NetworkPolicies are a silent no-op by default.** The EKS VPC CNI does not
enforce `NetworkPolicy` unless you explicitly enable it on the addon
(`enableNetworkPolicy=true`, VPC CNI ≥1.14). Applied without that, your policies do
nothing and the verification tool reports a false PASS. **Make the verification tool
assert enforcement is enabled, not just that policy objects exist** — a policy that
parses is not a policy that filters. If it catches this on you, that's a second
AI-USAGE entry.

**3.4 — NLB security groups are creation-time only.** You cannot attach an SG to an
existing NLB that was created without one. If the NLB comes from the AWS Load Balancer
Controller, set `service.beta.kubernetes.io/aws-load-balancer-security-groups` in the
Service annotations **from the start**, or you'll be recreating it.

**3.5 — HTTPS on the ALB needs a decision now.** ACM certs require a domain you
control plus DNS validation. If you don't have one in Route 53, pick deliberately:
(a) use a domain you own (~15 min if it's already in Route 53), (b) import a
self-signed cert and document the browser warning, or (c) run HTTP and document that
production would terminate TLS via ACM. **WAF attaches to HTTP listeners too**, so
this does not block the WAF story — don't let it burn an hour.

**3.6 — Make the WAF visibly *do* something.** Deliverable 6b asks for "the state of
the WAF." A WebACL that exists is a weak demo. Attach AWS Managed Rules (Common Rule
Set + Known Bad Inputs) plus a rate-based rule, enable WAF logging, and during the
demo fire a request that trips a managed rule so blocked-request count moves and the
sampled request is inspectable. Thirty minutes of work, and it converts a static
screenshot into a live control.

**3.7 — Fault 2 signature honesty.** Lowering a memory limit triggers a rolling
restart; the new pod OOMs *at startup* if the limit is below baseline. That's
`CrashLoopBackOff` from a bad config push — realistic and clean — but it is **not**
organic memory growth, and the `kubectl describe` output differs. Label it accurately
in the RCA ("config-pushed invalid limit → OOMKill at startup"). If you want genuine
run-then-die behaviour, set the limit modestly above idle and drive load, or add a
`stress-ng` sidecar sharing the pod's limit.

Also: **target a non-frontend C1 service** — `productcatalogservice` or
`recommendationservice`. You get real blast radius (frontend errors) while pod-level
alerts point *straight at the cause* — the exact inverse of Fault 1. Say that contrast
out loud in the write-up; it's the analytical point the whole exercise is testing.

---

## 4. Notes on choices I'd keep

**`cartservice` + Redis in C2 is the right split** — better than my currency/payment
suggestion:

- Redis stays co-located with its only client, so no chatty cross-region protocol.
- **Two independent C1 services cross the boundary** — `frontend` (cart badge on
  nearly every page) *and* `checkoutservice` (GetCart/EmptyCart). Two separate
  innocent components alerting from one remote cause.
- `productcatalogservice`, `recommendationservice`, and `adservice` stay green,
  proving the failure is selective rather than "everything is down."
- Expect a visible cross-region latency baseline (~60–70ms us-east-1↔us-west-2 RTT) on
  cart calls. **Screenshot that as the healthy baseline before injecting** — it makes
  the during-incident graph legible.

**Grafana Cloud + Alloy per cluster is correct.** One addition: add the **CloudWatch
datasource** so ALB/NLB/WAF metrics (`HTTPCode_ELB_5XX_Count`, `TargetResponseTime`,
`UnHealthyHostCount`, WAF `BlockedRequests`) land on the same dashboard. That makes
deliverable 6b one pane instead of console-hopping mid-demo. Don't try to pipe VPC
Flow Logs into Loki under time pressure — query them in CloudWatch Logs Insights and
document that as a deliberate trade-off.

---

## 5. Repository structure — deltas

The proposed tree is good. Four changes:

```diff
  observability-stack/
  ├── README.md
+ ├── WRITEUP.md                    # deliverable 5 is a DISTINCT graded item:
+ │                                 # design choices, trade-offs, test results,
+ │                                 # and the explicit skipped-with-reasons list
  ├── faults/
- │   ├── inject-network-fault.*
- │   ├── repair-network-fault.*
+ │   ├── inject_network_fault.py   # Python, not shell — you're on Windows and
+ │   ├── repair_network_fault.py   # will demo from it; one runtime, no shell roulette
+ │   ├── inject_oom_fault.py
+ │   └── repair_oom_fault.py
  ├── evidence/
  │   ├── alerts/  logs/  metrics/  kubernetes-events/  verification/
+ │   ├── cloudtrail/                # the actor+timestamp smoking gun for Fault 1
+ │   ├── flow-logs/                 # the REJECT records
+ │   └── baseline/                  # healthy screenshots taken BEFORE injection
```

`baseline/` matters more than it looks. Every "during incident" graph is unreadable
without the healthy one beside it, and you cannot go back and take it afterward.

---

## 6. Verification tool — additions

The proposed check list is solid, especially the insistence on evidence over `PASS`.
Add four:

- **NetworkPolicy enforcement is actually enabled** on the VPC CNI addon (§3.3) — not
  just that policy objects exist.
- **EKS control-plane endpoint** is private or CIDR-restricted for both clusters
  (easy to leave public and nobody looks).
- **No `LoadBalancer`/`NodePort` Services** anywhere except the one intended ALB
  ingress — enumerate across both clusters and assert the count.
- **The ALB's WAF WebACL association resolves to a WebACL with rules**, not merely
  that an association exists.

**Highest-value usage pattern:** run the tool **before, during, and after** fault
injection and commit all three JSON reports. During Fault 1 it should flag the NACL
deny that the monitors never pointed at. That reframes it from a compliance script
into a **detection control that found the root cause the alerts missed** — which is,
almost exactly, Ciroos's product thesis stated in your own artifacts.

---

## 7. Risk register

| # | Risk | Severity | Mitigation | When |
|---|---|---|---|---|
| 1 | Fault 1 doesn't manifest (conn tracking) | **Resolved** | NACL deny instead of SG removal (§2) | Design |
| 2 | NLB unreachable over inter-region peering | **Cleared** | Verified against AWS docs (§1) | — |
| 3 | Flow logs arrive too late for demo | High | `max_aggregation_interval = 60`, enable before faults | Block 1 |
| 4 | NetworkPolicy silently unenforced | High | Enable on CNI addon; assert in verify tool | Block 3 |
| 5 | NLB SG can't be added later | Medium | Set annotation at creation | Block 2 |
| 6 | No domain for ACM/HTTPS | Medium | Decide now — HTTP + documented is acceptable | Block 1 |
| 7 | Environment down at demo time | High | No teardown before the call; health check 30 min prior | Block 7 |
| 8 | Alert `for:` duration too long to fire live | Medium | 1m evaluation on demo alerts, not the 5m default | Block 4 |

---

## 8. Still open

- Exact call time Tuesday — book the slot, it sets the real deadline.
- AWS account: personal or sandbox? Drives quota risk and who absorbs the ~$40–60.
- Do you have a Route 53 domain available for ACM? Decides §3.5.
- Grafana Cloud account already provisioned, or signing up fresh?
