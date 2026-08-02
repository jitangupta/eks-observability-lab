# RCA: product catalog unavailable after invalid memory configuration

## Executive summary

The public frontend returned HTTP 500 because `productcatalogservice` could not
start. A configuration push reduced its memory request and limit from 64Mi/128Mi to
4Mi/4Mi, below the application's demonstrated startup working set. Kubernetes killed
the container with `OOMKilled` and exit code 137, and it entered CrashLoopBackOff.
The configuration was restored after 7 minutes 22 seconds; recovery checks later
confirmed HTTP 200, one Ready product-catalog replica, healthy cart isolation, and
zero active Grafana alerts.

## Impact

- **Customer experience:** the C1 frontend was observed returning HTTP 500 while
  product catalog was unavailable.
- **Affected capability:** product browsing and any frontend flow requiring
  `productcatalogservice`.
- **Observed fault window:** 2026-08-02 18:19:17Z to 18:26:39Z from invalid
  configuration push to restoration, 7 minutes 22 seconds.
- **Recovery validation:** frontend HTTP 200, the Deployment Ready, cart access
  controls unchanged, and all Grafana alerts quiet by the final capture.
- **Request count and exact outage interval:** not measured. The HTTP evidence is a
  point-in-time probe, so no continuous outage duration or production impact is
  claimed.

## Alert triage

Kubernetes recorded the first OOM at 18:19:22Z, five seconds after injection.
Grafana's direct `Container OOMKilled` and `Application container restarted` alerts,
plus the upstream frontend availability alert, began at 18:21:30Z. Detection latency
for the configured monitors was therefore 2 minutes 13 seconds.

`Application errors detected` was also active during capture, but its recorded start
time was 18:09:30Z—before this fault. It was not used as proof that the OOM incident
had been detected or caused by the configuration change.

Unlike Fault 1, the direct alerts were close to the root cause. The investigation
still verified the configuration history and isolated the blast radius rather than
stopping at the word `OOMKilled`.

## Investigation trail

1. **Confirmed user impact.** The frontend probe returned HTTP 500.
2. **Located the failed dependency.** The product-catalog pod was not Ready, had
   restarted five times, and was in CrashLoopBackOff.
3. **Read the termination signal.** Container status recorded `OOMKilled` and exit
   code 137. This established memory termination, not why the memory pressure
   existed.
4. **Compared desired configuration with baseline.** The Deployment showed a 4Mi
   request and 4Mi limit versus the journaled healthy 64Mi request and 128Mi limit.
   CPU settings were unchanged.
5. **Rejected an organic memory leak.** The failure began immediately after the
   resource patch and occurred at startup. The configured 4Mi ceiling was below the
   previously observed healthy working set.
6. **Rejected the cross-region cart path.** The authorized C1-to-C2 cart probe still
   connected and the unauthorized probe remained denied.
7. **Bound cause to change.** The injection journal recorded the previous resources,
   exact replacement, Deployment UID, generation, resource version, and restoration
   command.

### Dead ends and corrections during fault design

| Hypothesis or attempt | Evidence and correction |
|---|---|
| A normal RollingUpdate would expose the 1Mi fault | The single C1 node lacked spare CPU for a surge pod; the new pod stayed Pending and the healthy old pod kept serving. The gate returned `INCOMPLETE`. The fault workflow was changed to journal and temporarily use `Recreate`. |
| A 1Mi limit would create an application OOM | The pod sandbox failed before an application container existed, so there was no exit 137 or restart alert. Healthy memory measurements were used to choose 4Mi—above sandbox overhead and below application startup demand. |
| The cart network caused the frontend failure | The fault-state verifier proved the authorized cart probe still connected and the unauthorized caller stayed denied. |
| One-step resource and strategy restoration was safe | Returning to RollingUpdate too early recreated the packed-node scheduling deadlock. Restoration was changed to recover resources under Recreate, wait for readiness, and then restore the prior strategy. |

These failed trials were restored to baseline and are preserved in the private
evidence bundle. They are fault-design corrections, not hidden production events.

## Evidence

The generated bundle is intentionally ignored by Git because it contains live
cluster and cloud identifiers. It must accompany the private submission archive or
be shown locally.

- [Incident summary](../evidence/generated/phase11/fault2-20260802T181909Z/SUMMARY.txt)
  and [manifest](../evidence/generated/phase11/fault2-20260802T181909Z/manifest.json)
- [Injection journal](../evidence/generated/phase11/fault2-20260802T181909Z/injection.json)
- [Fault Deployment](../evidence/generated/phase11/fault2-20260802T181909Z/fault-deployment.json),
  [pods](../evidence/generated/phase11/fault2-20260802T181909Z/fault-pods.json), and
  [events](../evidence/generated/phase11/fault2-20260802T181909Z/fault-events.json)
- [Passing fault-state verification](../evidence/generated/phase11/fault2-20260802T181909Z/verification/verification-fault2-20260802T182016Z.json)
- [Investigation record](../evidence/generated/phase11/fault2-20260802T181909Z/investigation/investigation-20260802T182419Z.json)
  and [fault dashboard](../evidence/generated/phase11/fault2-20260802T181909Z/grafana/fault-dashboard-20260802T182435Z.png)
- [FIRING and RESOLVED mailbox evidence](../evidence/phase11/fault2-grafana-email-firing-resolved.png)
- [Restoration journal](../evidence/generated/phase11/fault2-20260802T181909Z/restoration-20260802T182455Z.json)
- [Final recovery record](../evidence/generated/phase11/fault2-20260802T181909Z/recovery/recovery-20260802T183950Z.json)

## Root cause

An operator-applied workload configuration set both the memory request and hard
limit of `productcatalogservice` to 4Mi. That ceiling was below the process's startup
requirement, so the kernel repeatedly terminated the application container for
exceeding its cgroup limit. Kubernetes reported `OOMKilled`, exit code 137, restarts,
and CrashLoopBackOff.

This was a configuration-pushed startup OOM in a controlled lab fault. It was not an
organic memory leak, node-wide memory exhaustion, or a cross-region network failure.

## Blast radius

- **Affected:** the single C1 `productcatalogservice` replica and upstream frontend
  requests that required it.
- **Not affected:** C2 cart and Redis, the private C1-to-C2 route, the authorized cart
  connection, the negative network-policy control, and public/private exposure.
- **Availability amplifier:** the lab intentionally used one product-catalog replica
  on one packed worker. There was no spare replica to absorb the bad rollout.

## Timeline

All timestamps are UTC.

| Time | Event | Elapsed from injection |
|---|---|---:|
| 18:19:00 | Final non-mutating preflight passed. | - |
| 18:19:17 | 4Mi/4Mi configuration and Recreate strategy applied. | 0:00 |
| 18:19:22 | First application OOM completed. | 0:05 |
| 18:21:30 | Direct OOM/restart and frontend availability alerts began. | 2:13 |
| 18:24:19 | Investigation captured five restarts, HTTP 500, exit 137, and unchanged cart isolation. | 5:02 |
| 18:26:39 | Healthy memory configuration and original rollout strategy restored. | 7:22 |
| 18:35:16 | Grafana reported all incident alerts quiet. | 15:59 |
| 18:39:50 | Final recovery capture confirmed HTTP 200 and Ready rollout. | 20:33 |
| 18:42:43 | Post-recovery preflight confirmed a clean, repeatable baseline. | 23:26 |

## Remediation

The manifest-bound restore workflow reapplied the exact journaled 64Mi request and
128Mi limit while retaining Recreate long enough for the baseline pod to become
Ready. It then restored the exact previous RollingUpdate strategy. Recovery checks
confirmed frontend HTTP 200, a Ready product-catalog replica, healthy authorized
cart connectivity, continued unauthorized denial, and zero active alerts.

## Preventative actions

### Implemented in the lab

- Journal and test the complete current resource configuration before mutation.
- Use optimistic JSON Patch tests so injection and restoration refuse concurrent
  drift.
- Fail evidence capture unless OOMKilled, exit 137, CrashLoopBackOff, restart, and
  blast-radius checks are all present.
- Restore in stages and wait for readiness before returning to the original rollout
  strategy.
- Alert on container OOM termination, restarts, upstream availability, and errors.

### Recommended for production

- Enforce minimum memory requests/limits and safe request-to-limit ratios with an
  admission policy, scoped per workload class.
- Require canary or staged deployment with automated rollback on readiness,
  restart, OOM, and user-journey regressions.
- Run at least two replicas across nodes/AZs and preserve enough headroom for the
  configured surge strategy.
- Use Vertical Pod Autoscaler recommendations or historical working-set data to
  review resource changes; do not automatically apply them without rollout gates.
- Add a deployment annotation/change identifier to dashboards and incident events so
  a resource change is immediately correlated with the first termination.
