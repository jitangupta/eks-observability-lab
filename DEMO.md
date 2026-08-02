# Phase 12 live demo runbook

## Goal and timing

This is a 45-minute script for one operator. It covers the five required live-demo
items in order and uses Fault 1 for the live incident because it provides the clearer
hands-on investigation: customer impact appears in C1 while the cause is a C2 network
configuration object.

| Time | Segment | Requirement |
|---:|---|---|
| 00:00-04:00 | Outcome and architecture | Context |
| 04:00-09:00 | Live application journey | 6a |
| 09:00-15:00 | WAF, ALB, NLB, and observability state | 6b |
| 15:00-21:00 | Positive/negative access verification | 6c |
| 21:00-35:00 | Inject, detect, investigate, restore Fault 1 | 6d |
| 35:00-40:00 | Prove recovery | 6d |
| 40:00-44:00 | VP of Engineering RCA walkthrough | 6e |
| 44:00-45:00 | Close and evidence pointers | Summary |

## Safety rules

- Run from the repository root in PowerShell.
- Use Fault 1 only. Do not inject Fault 2 in the same session.
- Do not run `inject --execute` unless preflight is PASS and reports
  `live fault injected: false`.
- Keep the exact injection manifest path. Restore only through that manifest.
- If the session is interrupted after injection, skip presentation work and execute
  the manifest-bound restore command.
- Never delete NACL rule 50 manually unless the tool reports
  `MANUAL_RESTORE_REQUIRED` and the manifest/current entry have been inspected.

## Before the call

### Browser tabs

Open and authenticate these tabs in advance:

1. Public Online Boutique home page.
2. AWS WAF WebACL associated-resources view.
3. AWS EC2 Load Balancers view filtered to the lab ALB and NLB.
4. Grafana incident dashboard with timezone set to UTC and a 30-minute range.
5. Grafana alert rules/contact point view.
6. The architecture diagram.

Keep the public application tab incognito or signed out so it represents an end user.
Do not expose account IDs, tokens, email addresses, or Terraform state on screen.

### Terminal setup

Open two PowerShell terminals at the repository root. In both terminals:

```powershell
$pythonExe = ".\.venv\Scripts\python.exe"
$awsProfile = "observability-lab"
aws sts get-caller-identity --profile $awsProfile
kubectl config get-contexts
```

If a different profile is used by `terraform.tfvars`, set `$awsProfile` to that
value. Confirm the AWS CLI is on `PATH`; the kubeconfig contexts use it as an exec
credential plugin.

Run these non-mutating checks before the audience joins:

```powershell
& $pythonExe .\faults\fault1_nacl.py --profile $awsProfile preflight
& $pythonExe .\verification\verify.py --profile $awsProfile --state healthy
```

Both must pass. Confirm that Grafana has no active alerts and complete one end-user
checkout. Keep the newest preflight manifest available, but do not inject.

## 00:00-04:00 — architecture and intended outcome

Show `architecture/corrected-architecture.excalidraw` or the Mermaid diagram in
`architecture/architecture.md`.

Say:

> This is one application split across two AWS regions. The only public application
> path is WAF to the C1 ALB. Workers and the C2 cart NLB are private. Frontend and
> checkout in C1 are the only intended callers of cart in C2 on TCP/7070, over VPC
> peering. Metrics and logs leave both clusters for Grafana Cloud; no monitoring
> ingress is opened.

Trace three paths on the diagram: end user to WAF/ALB/frontend, authorized C1 caller
to the internal C2 NLB/cart/Redis, and outbound telemetry to Grafana Cloud.

## 04:00-09:00 — live end-user journey

In the incognito application tab:

1. Refresh the home page.
2. Open a product.
3. Add it to the cart.
4. Open the cart.
5. Complete checkout.

Narrate only observed behavior. A completed journey proves that the user entry path,
C1 services, the cross-region cart call, and C2 Redis dependency are working at this
moment.

## 09:00-15:00 — WAF, load balancers, and observability

Show, in this order:

1. **WAF:** the WebACL is associated with the C1 ALB and has the managed common-rule
   group plus rate limiting.
2. **ALB:** scheme is internet-facing and its targets are healthy.
3. **NLB:** scheme is internal, listener is TCP/7070, and targets are healthy.
4. **Grafana:** cart and frontend probes are UP, C1/C2 Ready counts are 10/2, restart
   and OOM panels are zero, and alert rules are Normal.

State the limitation clearly: the current demo endpoint is HTTP plus WAF because no
validated ACM domain was available; production would require HTTPS.

## 15:00-21:00 — prove intended access and no accidental exposure

In Terminal 1, run the full verifier again so the audience sees current evidence:

```powershell
& $pythonExe .\verification\verify.py --profile $awsProfile --state healthy
```

Call out these results as they appear:

- exactly one internet-facing C1 ALB and one internal C2 NLB are allowlisted;
- worker nodes have no public IP;
- WAF association and rules pass;
- private routes, peering, security groups, NACL baseline, and EKS endpoint checks
  pass;
- VPC CNI enforcement and PolicyEndpoints are active;
- the authorized C1 probe connects to cart;
- the unauthorized C1 probe is denied and cleanup succeeds.

Say:

> This is scoped to the two Terraform-managed VPCs and cluster identities. It proves
> the positive and negative cases; it does not claim to inventory unrelated account
> resources.

## 21:00-35:00 — inject and investigate Fault 1

### 21:00-23:00 — final preflight and injection

In Terminal 1:

```powershell
& $pythonExe .\faults\fault1_nacl.py --profile $awsProfile preflight
& $pythonExe .\faults\fault1_nacl.py --profile $awsProfile inject --execute
$incidentManifest = Get-ChildItem .\evidence\generated\phase10 -Filter injection.json -Recurse `
  | Sort-Object LastWriteTimeUtc -Descending `
  Select-Object -First 1 -ExpandProperty FullName
$incidentBundle = Split-Path $incidentManifest
Get-Content $incidentManifest -Raw | ConvertFrom-Json `
  |
  Select-Object completed_at,status,created_nacl_ids,restore_command
```

Keep the printed restore command visible. Start a stopwatch at the injection's UTC
completion time.

### 23:00-27:00 — observe symptoms and alert

Refresh the cart page and show the cart dependency failure. In Grafana, keep the
cart probe and alert state visible until `Cross-region cart probe failure` fires.
Do not claim the root cause yet.

In Terminal 2, establish the symptom and preserve a fault-state report inside this
incident bundle:

```powershell
$faultVerification = Join-Path $incidentBundle "verification"
& $pythonExe .\verification\verify.py `
  --profile $awsProfile `
  --state fault1 `
  --output-dir $faultVerification
```

Point out that the authorized caller fails while the unauthorized caller remains
denied. The frontend probe may remain UP; the incident is the cart dependency, not
necessarily total site ingress.

### 27:00-32:00 — hands-on debugging

Start with hypotheses, not the known injection command:

```powershell
kubectl --context eks-observability-lab-c1 get pods -n online-boutique
kubectl --context eks-observability-lab-c2 get pods -n online-boutique
```

Call out that pods are Ready with no OOM/restart signature. Use the fault-state
verifier output to show that ALB/NLB targets, routes, peering, security groups, CNI
enforcement, and Kubernetes exposure passed. This rejects application rollout,
target-health, routing, security-group, and policy-agent hypotheses.

Now inspect the changed C2 NACL directly:

```powershell
$incident = Get-Content $incidentManifest -Raw | ConvertFrom-Json
$targetNaclId = $incident.scope.nacl_ids[0]
$naclJson = aws ec2 describe-network-acls `
  --profile $awsProfile `
  --region $incident.scope.region `
  --network-acl-ids $targetNaclId `
  --output json | ConvertFrom-Json
$naclJson.NetworkAcls[0].Entries `
  |
  Where-Object RuleNumber -eq $incident.scope.rule_number |
  Format-List RuleNumber,Egress,Protocol,RuleAction,CidrBlock,PortRange
```

Explain the finding: ingress rule 50 has higher precedence than baseline rule 100
and denies the C1 CIDR on TCP/7070. This is the root cause.

### 32:00-35:00 — corroborate and restore

Capture Flow Log, CloudTrail, Grafana, dashboard, and dead-end evidence. It may need
one rerun while AWS/Grafana data arrives:

```powershell
& $pythonExe .\faults\capture_fault1.py `
  --profile $awsProfile `
  --manifest $incidentManifest
```

Then preview and execute the journal-bound restore:

```powershell
& $pythonExe .\faults\fault1_nacl.py --profile $awsProfile restore --manifest $incidentManifest
& $pythonExe .\faults\fault1_nacl.py --profile $awsProfile restore --manifest $incidentManifest --execute
```

If the capture is still waiting for delayed supporting data at minute 34, preserve
its `INCOMPLETE` output and restore on schedule. The live root cause is already
established by the exact active NACL entry; delayed Flow Logs and CloudTrail are
corroboration, not a reason to extend customer impact.

## 35:00-40:00 — prove recovery

In Terminal 1:

```powershell
& $pythonExe .\verification\verify.py --profile $awsProfile --state restored
```

Refresh the product/cart/checkout flow. Show the Grafana probe returning UP. The
alert may remain FIRING for one evaluation interval; explain that service recovery
and alert-state convergence are separate measurements. Show RESOLVED when it arrives.

Required recovery statements:

- only the manifest-owned rule was removed;
- the authorized probe connects again;
- the unauthorized probe is still denied;
- no new public surface exists;
- workloads and targets remain healthy.

## 40:00-44:00 — VP of Engineering RCA walkthrough

Lead with the business outcome, not the tool names:

> Cart operations were unavailable to C1 for 8 minutes 42 seconds in the rehearsed
> incident, while the storefront entry point remained reachable. The root cause was
> one high-precedence C2 subnet rule that denied C1 traffic to the cart port. Our
> dependency monitor detected the break in 1 minute 47 seconds. Workloads and load-
> balancer targets remained healthy, so the investigation moved below the service
> layer. We removed only the changed rule, revalidated authorized and unauthorized
> access, and completed the user journey. To prevent recurrence, network changes
> should be IaC-only, protected by policy and canary gates, with direct alerts on
> control-plane mutations.

Then show the timeline and evidence links in `rca/fault1-nacl.md`. Mention Fault 2 in
one sentence as the contrasting signature: its direct OOM/137 signal made diagnosis
shorter, while the cart path remained healthy.

## 44:00-45:00 — close

Return to `WRITEUP.md` and point to the six deliverables. State that the recordings
are presentation aids; the verifier JSON, incident manifests, CloudTrail/Flow Log
capture, Kubernetes status, and email evidence support the claims.

## Recovery card if the demo is interrupted

From any PowerShell terminal at the repository root:

```powershell
$pythonExe = ".\.venv\Scripts\python.exe"
$awsProfile = "observability-lab"
$incidentManifest = Get-ChildItem .\evidence\generated\phase10 -Filter injection.json -Recurse `
  | Sort-Object LastWriteTimeUtc -Descending `
  Select-Object -First 1 -ExpandProperty FullName
& $pythonExe .\faults\fault1_nacl.py --profile $awsProfile restore --manifest $incidentManifest
& $pythonExe .\faults\fault1_nacl.py --profile $awsProfile restore --manifest $incidentManifest --execute
& $pythonExe .\verification\verify.py --profile $awsProfile --state restored
```

Do not continue the presentation until the restored verifier passes.
