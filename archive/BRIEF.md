# Ciroos Assignment — Reverse-Engineered Brief

Assignment received from Albert (Ciroos Hiring Team). Decoded into what is actually
being graded, what must physically exist, and how to spend the clock.

---

## 1. The timeline conflict

Two dates appear in the email:

| Source | Says | Status |
|---|---|---|
| Body of email | "complete by Tuesdcay [sic] EST morning" | **Binding.** This is the human commitment, tied to a booked 45-min call. |
| Requirements §Timeframe | "24 hours from the time this exercise is assigned" | **Boilerplate.** Reused template text that sets *scope expectation*, not the due date. |

**Resolution: build to Tuesday morning (2026-08-04), scope to 24 hours of effort.**

The 24-hour clause is not a deadline — read what it's actually doing:

> "We do not expect production-grade perfection... Clear documentation of what you
> built, what you skipped, and why."

It exists to license you to cut scope and to make *the cut list itself* a graded
artifact. Treating it as a deadline is the wrong read; treating it as permission to
ship an unpolished-but-honest build is the right one.

**Two actions, both today:**
1. Book the Ashby slot now — <https://you.ashbyhq.com/meeting/e96fa030-b559-428e-8a00-41c3bd1a99fe/>.
   Booking resolves the ambiguity: the slot time *is* the deadline. Slots also fill.
2. Optional one-liner to Albert: *"Booked for <time> Tue — treating that as the
   deadline, with the 24h guidance as the scope bar. Shout if you meant a hard 24h
   clock from today."* Low cost, removes all doubt, signals you read carefully.

**Hard constraint that follows:** requirement 7e says they may ask you to
**re-inject a fault live during the call**. The environment must still be running
Tuesday morning. Do not plan a Monday-night teardown. Budget for ~48h uptime.

---

## 2. What is actually being graded

The document is ordered by build sequence, not by weight. The weights are inverted
from the way it reads.

> "Then — and this is the part we care most about — deliberately break the
> environment, triage the resulting alerts, and debug it hands-on to an
> evidence-backed root cause."

Ciroos builds an AI SRE that separates symptom from cause in a noisy alert stream.
**The AWS environment is the stage set. The RCA is the product.**

Estimated weighting:

| Requirement | Est. weight | Role |
|---|---|---|
| 1–3 Environment, connectivity, security | ~20% | Table stakes. Must work; won't win. |
| 4 Python verification tool | ~10% | Cheap to nail. A real differentiator if it's genuinely runnable. |
| 5 Observability | ~15% | Enabler. Alerts must *actually fire* — "monitors configured" ≠ "monitors fired". |
| 6 Fault injection | ~15% | Quality of fault *design* is what's scored, not the injection. |
| **7 Debugging & RCA** | **~30%** | The whole point. |
| 6e/7e Live demo + presentation | ~10% | VP-of-Eng framing. Explicitly called out twice. |

**Corollary: build the minimum infra that makes a *good incident* possible, then
spend the remaining time on evidence capture and narrative.** Every extra hour on
Terraform polish is an hour stolen from the graded part.

---

## 3. Decoded requirements — the traps

### 3.1 The "no public exposure" contradiction

Three clauses appear to fight:

- §1b/§6a — app must be "accessible by an end-user"
- §Objective — "protected by an ALB and WAF"
- §3c — "Ensure no public network exposure of the services"

They are not in conflict; the resolution is the answer they want:

> **Exactly one public surface: the internet-facing ALB, fronted by WAF. Everything
> behind it is private.**

Concretely that means: services are `ClusterIP` only (no `LoadBalancer`, no
`NodePort`), nodes sit in private subnets with no public IPs, egress via NAT,
the EKS API endpoint is private (or CIDR-restricted to your IP), and the C1→C2 path
never touches the internet.

Call this reconciliation out explicitly in the write-up. It's the clause most
candidates will either miss or silently violate by exposing a second LoadBalancer.

### 3.2 "AWS-native networking constructs" (§2a)

Rules out a Tailscale/WireGuard/service-mesh-over-internet shortcut. Three legitimate
options:

| Option | Setup cost | Least-privilege story | Verdict |
|---|---|---|---|
| **Cross-region VPC peering** | Low (~15 min) | CIDR-scoped SGs + NACLs + narrow routes | **Recommended.** Fastest path to a working, defensible build. |
| PrivateLink (NLB + endpoint service) | Medium | Strongest — endpoint policies, service-level not network-level | Best *design* answer. Cross-region endpoints exist but add risk under clock. |
| Transit Gateway peering | Medium-high | Route-table scoped | Overkill for two VPCs; TGW attachment cost. |

**Recommendation: build peering, and write PrivateLink up as the design you'd
choose with more runway.** §3b explicitly name-drops "endpoint policies" — that's a
nudge toward PrivateLink, so addressing it in prose earns the point without the risk.

**Detail worth stating in the write-up (shows depth):** cross-region VPC peering does
**not** support security-group referencing — that only works same-region. So C1→C2
least-privilege has to be expressed as CIDR-scoped SG rules on a single port, plus
NACLs, rather than `sg-c1 → sg-c2`. Naming that constraint unprompted is exactly the
kind of thing an interviewer marks as "has actually done this."

### 3.3 "Enterprise-class application" (§1b)

Means multi-service with real inter-service dependencies — not a hello-world. The
suggested list is the hint. What matters is §1b's real requirement: **a service in C1
must call a specific service in C2**, i.e. you must *split* a demo app across regions,
which none of them ship as a supported topology.

**Recommendation: Google Online Boutique**, split so that a C1 service calls a C2
service over the private path.

- Lowest setup friction of the four (no external DB, single manifest).
- Fan-in topology: `frontend` → `checkoutservice` → several backends. Breaking one
  backend degrades multiple upstream services → **alerts fire on services that are
  not the root cause**, which is precisely what §6a asks for.
- gRPC over an internal NLB across the peering link works; note it's gRPC in the
  write-up so nobody assumes plain HTTP.
- C2 only hosts 1–2 services, so C2's node group can be small.

Bank of Anthos is the runner-up but drags in Postgres + JWT keys — more moving parts
than the clock supports.

### 3.4 §4 verification tool — the cheapest win available

"Confirms there are no unintended public access paths" is a **negative** assertion.
Most candidates will submit a script that curls the ALB and prints "OK". That answers
nothing. What it must do:

- **Enumerate, don't assume.** Walk the account via boto3: every ELB (scheme),
  every SG with `0.0.0.0/0` ingress, every ENI with a public IP, every public subnet
  with a node in it, the EKS endpoint access config, every NACL allow rule.
- **Prove the positive case:** C1's caller pod *can* reach the specific C2 service.
- **Prove the negative cases** — these are what make it credible:
  - another C1 pod *cannot* reach that C2 service
  - the C2 service is unreachable from the public internet
  - the C2 service is unreachable from outside the peered CIDR
- **Machine-readable output.** JSON report + non-zero exit on failure, so it reads as
  something you'd wire into CI, not a demo prop.

This is a few hours of work and it's the deliverable you can most easily make *better
than everyone else's*.

### 3.5 §6 fault design — the actual skill test

"different root-cause signatures" is the operative phrase. Two faults that both
produce "pod is unhealthy" teach them nothing.

**Fault A — cascading / network-config (the one they care about).**
Break the C1→C2 path at the *network* layer — e.g. remove or mangle the C2-side SG
ingress rule, or blackhole the peering route. Effects:

- C1 `frontend` and `checkoutservice` throw 5xx / latency alerts
- ALB target-health and 5xx alerts fire
- **C2's own pods stay green** — CPU normal, no restarts, no crashes
- Zero alerts point at the actual broken object (an SG rule in another region)

That gap — every alert in C1, the fault in C2's control plane config — is the exact
symptom/cause separation Ciroos's product exists to do. It's the strongest possible
demo of the skill they're hiring for.

**Fault B — resource exhaustion.**
Drop a memory limit (or run a memory hog) on a C1 service → OOMKill →
`CrashLoopBackOff`. Different signature entirely: pod-level, self-evident in
`kubectl describe` (`Reason: OOMKilled`, `Exit Code: 137`), restart-count metric,
kernel OOM in logs. Contrast in the write-up: *Fault B the alerts point straight at
the cause; Fault A they point everywhere except.* Making that contrast explicit is
the insight §Guidelines-2 is fishing for.

Avoid the bad-image-tag fault — it's too trivially diagnosable to be interesting.

### 3.6 §7c "quoted or screenshotted, not asserted" — plan for this *before* you break things

This is the requirement most likely to be silently failed, because the evidence is
**perishable**. Once you restore the environment, the pod is gone, the events have
aged out (k8s events default to 1h TTL), and the dashboard window has scrolled.

**Discipline: capture as you go, into a git-tracked folder.**

```
evidence/
  fault-a/
    T0-injection.txt            # exact command + UTC timestamp
    alerts-fired.png            # alert list w/ timestamps visible
    kubectl-get-pods-c1.txt
    kubectl-describe-<pod>.txt
    logs-checkoutservice.txt    # the actual error lines
    dashboard-latency.png       # time axis legible
    dead-end-<n>.txt            # hypotheses you rejected + why
```

Timestamp everything in **UTC** and keep one clock. The RCA timeline
(first symptom → detection → diagnosis → fix) is only credible if the artifacts
corroborate it.

**Record your dead ends.** §7b says so explicitly:

> "Dead ends included; how you recover from a wrong hypothesis is signal, not noise."

A clean, linear, obviously-retrofitted investigation reads as fabricated and will
score *worse* than a messy real one. Keep a running scratch log during the incident.

### 3.7 §Guidelines-1 — the AI-collaboration log

> "Tell us where AI helped, where it was wrong, and how you caught it."

This is a real scored item at a company building an AI SRE. It cannot be
reconstructed convincingly afterward. Keep `AI-LOG.md` open from the start and append
as you work — specifically the cases where AI-generated Terraform/manifests were
wrong and what signal caught it (plan diff, apply error, failing verification check).
"AI was wrong about X, the verification tool caught it" is the single most on-message
sentence you can put in this submission.

### 3.8 §6e "VP of Engineering" framing

> "lead with impact and root cause, not with tooling."

Structure for the RCA walkthrough:

1. **Impact** — what the user couldn't do, for how long, how many requests failed.
2. **Root cause** — one sentence, one object, no jargon.
3. **Why detection took N minutes** — and what would make it faster.
4. **Fix + prevention** — immediate remediation, then the control that stops recurrence.

Tooling appears only as supporting evidence. "I ran a PromQL query" is not a finding.

---

## 4. Deliverables → concrete artifacts

| # | Deliverable | Artifact | Notes |
|---|---|---|---|
| 1 | Architecture diagram | `docs/architecture.excalidraw` + `.png` | Show trust boundaries and the single public surface. One diagram, legible in a screen share. |
| 2 | Infra code | `terraform/` + `k8s/` | Must apply from clean. README with region/profile vars. |
| 3 | Python verification tool | `verify/` + usage in README | JSON output, exit codes, `--region` flags. See §3.4. |
| 4 | RCA per fault | `rca/fault-a.md`, `rca/fault-b.md` | Exact structure of §7a–d as headings. Inline evidence. |
| 5 | Write-up | `WRITEUP.md` | Design choices, trade-offs, **explicit skipped list with reasons**, test results. |
| 6 | Live demo | `DEMO.md` runbook | 6a–6e as an ordered script. Rehearse once. |
| + | AI log | `AI-LOG.md` | §Guidelines-1. Append live. |

Proposed layout:

```
observability-stack/
├── README.md              # start here: what this is, how to run it
├── BRIEF.md               # this file
├── WRITEUP.md             # deliverable 5
├── DEMO.md                # deliverable 6 runbook
├── AI-LOG.md              # Guidelines-1
├── docs/architecture.*    # deliverable 1
├── terraform/
│   ├── c1-<region>/       # VPC, EKS, ALB, WAF
│   ├── c2-<region>/       # VPC, EKS, internal NLB
│   └── peering/           # cross-region peering, routes, SGs, NACLs
├── k8s/{c1,c2}/           # split Online Boutique manifests
├── observability/         # agent config, dashboards, alert rules (as code)
├── verify/                # deliverable 3
├── faults/                # inject.py / restore.py — one command each
├── evidence/{fault-a,fault-b}/
└── rca/                   # deliverable 4
```

**`faults/` must be one command to inject and one to restore** — §7e means you will
run it live, under observation, while talking. Anything hand-typed will be fumbled.

---

## 5. Recommended stack decisions

| Decision | Pick | Why | Swap cost if you disagree |
|---|---|---|---|
| App | Online Boutique, split | Fan-in topology → cascading alerts; no external DB | Medium — Sock Shop also splits cleanly |
| Regions | `us-east-1` (C1) + `us-west-2` (C2) | Best quota/AMI availability, cheapest, EST-friendly | Free |
| C1→C2 path | Cross-region VPC peering + internal NLB | Fastest defensible build; PrivateLink written up as the ideal | Low if decided now, high later |
| Observability | **Grafana Cloud free tier** | Single pane over both regions/clusters; k8s-monitoring Helm chart ships metrics+logs+events in one install; alert rules as code; free tier is enough | High — two self-hosted stacks = two panes = weak demo |
| Node groups | C1: 2–3× `t3.large`, C2: 2× `t3.medium` | C2 hosts only 1–2 services | Free |
| IaC | Terraform + `terraform-aws-modules/eks` | Don't hand-roll EKS under a clock | High |

**On observability:** self-hosted `kube-prometheus-stack` in each cluster gives you
two disconnected UIs and no cross-region correlation — which actively undermines the
Fault A narrative, where the whole point is that C1 alerts and the C2 cause must be
seen together. Grafana Cloud (or Datadog trial) is the right call here.

---

## 6. Cost and time realities

**Time (unavoidable, plan around it):**
- `terraform apply` for EKS: **~15–20 min per cluster**, and they can run in parallel.
- Cluster destroy: ~10–15 min. Don't start it before the interview ends.
- Grafana Cloud k8s integration → first metrics: ~5 min.
- Alert rule → first real fire: allow for the evaluation interval (set 1m `for:` on
  demo alerts, not the 5m default, or you'll wait around on the live call).

**Cost, ~48h with both clusters up:**

| Item | Est. |
|---|---|
| EKS control planes (2 × $0.10/hr) | ~$10 |
| Worker nodes (~5 instances) | ~$20 |
| NAT gateways (1 per VPC) | ~$5 + data |
| ALB + internal NLB | ~$3 |
| WAF web ACL + rules | ~$2 |
| Cross-region data transfer | <$1 |
| **Total** | **~$40–60** |

Use **one NAT gateway per VPC**, not one per AZ — that's the line item that silently
triples this. Set a billing alarm. Destroy immediately after the call.

---

## 7. Plan for the clock

Ordered so that the graded 30% is never what gets squeezed. Durations, not clock
times — slide as needed.

**Block 1 — Decisions & foundation (~3h, today)**
- Book the Ashby slot. ← do first, it sets the real deadline
- Lock the decisions in §5. Start `AI-LOG.md` now.
- Terraform: two VPCs (private subnets, 1 NAT each), two EKS clusters. **Apply both in parallel** and write manifests while they build.
- Billing alarm.

**Block 2 — App + private path (~3h)**
- Split Online Boutique: most services in C1, target service(s) in C2.
- Internal NLB in front of the C2 service; cross-region peering + routes.
- Prove C1 pod → C2 service works (`grpcurl` / in-cluster curl). **Save this output — it's the "before" evidence.**
- ALB + WAF in front of C1 `frontend`. Confirm app loads in a browser.

**Block 3 — Lock it down + verification tool (~3h)**
- SGs/NACLs to least privilege. EKS endpoints private/restricted. Confirm no
  `LoadBalancer`/`NodePort` services other than the intended ALB.
- Build `verify/` (§3.4). **Run it and fix what it finds** — that fix is a genuine
  AI-LOG entry if the tool catches something you or the AI missed.

**Block 4 — Observability (~3h)**
- Grafana Cloud k8s-monitoring on both clusters; confirm metrics *and* logs *and*
  k8s events arrive from both.
- One dashboard covering both clusters + the cross-region path.
- Alert rules: ALB 5xx, service latency, pod restarts/CrashLoop, target health,
  cross-region call error rate. Route to a real contact point (email/Slack) — §5c
  says **"must fire real alerts"**, so a notification must land somewhere visible.
- **Verify one alert actually fires end-to-end before you trust the setup.**

**Block 5 — Faults + evidence (~4h) ← the graded core, protect this block**
- Fault A: break the C1→C2 network path. Let it burn 10–15 min so the alert stream
  is genuinely noisy. Investigate live, capturing into `evidence/fault-a/`. Log dead
  ends as they happen.
- Restore. Confirm recovery is visible in the dashboards.
- Fault B: OOMKill/CrashLoop. Same discipline, `evidence/fault-b/`.
- Both faults scripted in `faults/` and **tested via the script**, since that's what
  runs live Tuesday.

**Block 6 — Writing (~3h)**
- `rca/fault-a.md`, `rca/fault-b.md` — §7a–d as literal headings, evidence inline.
- `WRITEUP.md` — design choices, trade-offs, **skipped list with reasons**.
- Architecture diagram.
- `AI-LOG.md` final pass.

**Block 7 — Demo prep (~2h, Tuesday early)**
- `DEMO.md` as an ordered script mapped to 6a–6e.
- **Rehearse the live fault injection once, timed**, including recovery.
- Confirm the environment is healthy and alerts are quiet at the start of the call.
- Pre-open tabs: app, ALB/WAF console, Grafana dashboard, alert list, two terminals
  with kubeconfig contexts already set.

**If you fall behind, cut in this order:** WAF managed rule tuning → NACLs (keep SGs)
→ dashboard polish → Fault B (keep A) → **never cut the RCA or the evidence capture.**
Then document each cut in `WRITEUP.md` — §Timeframe explicitly rewards that.

---

## 8. Ways to exceed the bar

§Guidelines-2 says "Wow us." Highest signal-per-hour, roughly in order:

1. **A dependency/topology map**, and use it in triage to explain *why* the C1 alerts
   are downstream of the C2 fault. This is literally what Ciroos's product does —
   showing you reason that way is the strongest possible fit signal.
2. **Time-to-detect as a measured number**, per fault: fault injected at T0, first
   alert at T0+Xs. Then say what you'd change to shrink it. Nobody does this.
3. **An "alert noise ratio"** for Fault A: N alerts fired, 0 pointed at the cause.
   Quantifying the noise problem is stating their thesis back with your own data.
4. **The verification tool run before *and* after fault injection**, showing it
   catches the mangled SG rule. Turns a compliance script into a detection control.
5. **A short "what an AI SRE would need"** section in the write-up: the signals,
   topology, and context an agent would require to reach your conclusion
   automatically — and where it would still get stuck. Direct product empathy.

---

## 9. Open questions

- Exact call time Tuesday (booking the slot answers this).
- AWS account: personal or sandbox? Determines quota risk (EIP/VPC limits) and who
  eats the ~$50.
- Grafana Cloud account exists, or does it need signing up? Affects Block 4 start.
