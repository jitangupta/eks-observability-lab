# Fault injection and evidence runbooks

## Phase 10 Fault 1: NACL inject and restore

`fault1_nacl.py` owns the repeatable Fault 1 mutation. It targets only the C2 private
NACL IDs and reserved rule number emitted by Terraform. The injected entry is an
ingress `DENY` from the C1 VPC CIDR to TCP/7070; no security group, route, baseline
NACL entry, or Kubernetes object is changed.

## Safety contract

- `preflight` is non-mutating. It verifies the AWS account, Terraform scope, exact
  subnet-to-NACL associations, rule-100 baseline allows, and absence of reserved rule
  50. It then sends `DryRun=True` create and delete requests for every target NACL.
- Injection requires the explicit `--execute` flag. The tool refuses collisions,
  partial states, out-of-scope subnet associations, or account/scope drift.
- The injection journal is written before the first AWS mutation and updated after
  every successful create. A partial failure triggers automatic rollback.
- Restoration requires that injection journal. It compares the journal with current
  Terraform outputs, checks every reserved entry before deleting anything, refuses
  shape mismatches, and deletes only exact ingress rule-50 entries in the journaled
  NACL scope.
- Re-running injection while the exact fault is active is a no-op only when a matching
  local injection journal proves ownership. Re-running restore after recovery is also
  a no-op and records `ALREADY_RESTORED`.

Generated journals and preflight reports are placed below
`evidence/generated/phase10/`, which is intentionally ignored by Git because it
contains live account and resource identifiers.

### 1. Validate without injecting

From the repository root, use the same Python environment and AWS profile as the
Phase 9 healthy capture:

```powershell
.\.venv\Scripts\python.exe .\faults\fault1_nacl.py preflight
```

Add global options before the subcommand when required:

```powershell
.\.venv\Scripts\python.exe .\faults\fault1_nacl.py `
  --profile NAME `
  --terraform-dir .\terraform `
  preflight
```

A successful preflight prints `live fault injected: false`, writes a UTC-stamped
PASS manifest, and proves that both the create and restoration API request shapes
are authorized without changing the NACL. Inspect the manifest before proceeding.

Run deterministic round-trip tests without AWS access:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\faults -p "test_*.py" -v
```

### 2. Inject only for the authorized live incident

Do not run this command while merely validating tooling:

```powershell
.\.venv\Scripts\python.exe .\faults\fault1_nacl.py inject --execute
```

The output includes the UTC time, changed NACL IDs, injection journal path, and an
exact restore command. Preserve that terminal output and journal. Then wait for real
alerts, investigate independently, and capture the during-fault verifier result:

```powershell
.\.venv\Scripts\python.exe .\verification\verify.py --state fault1
```

After the verifier passes, capture the ordered dead ends, NACL, Flow Log, CloudTrail,
active-alert, and dashboard evidence while the fault remains active:

```powershell
.\.venv\Scripts\python.exe .\faults\capture_fault1.py `
  --manifest .\evidence\generated\phase10\fault1-<UTC>\injection.json
```

The capture returns `INCOMPLETE` when delayed Flow Log, CloudTrail, or Grafana data
has not arrived yet; wait briefly and rerun it before restoration.

### 3. Restore the journaled injection

Preview restoration first; this validates scope, current entry shapes, and delete
authorization without changing AWS:

```powershell
.\.venv\Scripts\python.exe .\faults\fault1_nacl.py restore `
  --manifest .\evidence\generated\phase10\fault1-<UTC>\injection.json
```

Use the exact path printed by injection, then execute:

```powershell
.\.venv\Scripts\python.exe .\faults\fault1_nacl.py restore `
  --manifest .\evidence\generated\phase10\fault1-<UTC>\injection.json `
  --execute
```

Finally verify the recovered state:

```powershell
.\.venv\Scripts\python.exe .\verification\verify.py --state restored
```

For a strict application-and-alert recovery bundle, rerun the Phase 9 capture with a
Phase 10 recovery output root after the alert evaluation window clears. This checks
the checkout journey, cart latency, Pods, Redis, edge targets, policy probes, quiet
alerts, and the recovery dashboard together.

If automatic injection rollback reports `MANUAL_RESTORE_REQUIRED`, use the exact
restore command printed by the tool. Never delete rule 50 manually without first
checking the injection journal and the current entry shape.

## Phase 11 Fault 2: product catalog configuration-pushed OOM

`fault2_oom.py` owns the C1 workload mutation. It targets only
`deployment/productcatalogservice`, container `server`, in `online-boutique`. The
fault atomically replaces the live container's full `resources` object after testing
its Kubernetes `resourceVersion` and current resources, changing request memory and
limit memory to the locked invalid value `4Mi`. CPU settings are preserved. Healthy
telemetry measured approximately 5.70 MB for the application and 0.22 MB for its
pause container, making `4Mi` low enough to kill the application but high enough for
the Pod sandbox to start. Because this lab has one tightly packed C1 node, the same
atomic patch temporarily changes
the one-replica Deployment strategy from its exact journaled `RollingUpdate` value to
`Recreate`; otherwise the surge Pod can remain Pending for insufficient CPU and never
exercise the invalid memory configuration.

This is a deliberately invalid configuration pushed at startup. Describe it as a
configuration-caused OOM, never as an organic memory leak.

### Safety contract

- `preflight` discovers the C1 context from Terraform, validates the exact Deployment
  UID and shape, checks `patch deployments.apps` authorization, and uses Kubernetes
  server-side dry-run. It creates no ReplicaSet or rollout.
- Injection requires `--execute`. Before patching, it journals the exact previous
  `resources` object, Deployment UID, resource version, generation, intended fault
  object, UTC time, and exact restore command.
- The JSON Patch atomically tests the current resource version, entire current
  resource object, and rollout strategy before replacement, so concurrent operator
  or Helm drift causes a refusal instead of an overwrite.
- Injection immediately preserves Pod JSON, namespace Events, Deployment JSON, and
  current/previous logs. It waits for all four direct signals: `OOMKilled`, exit 137,
  `CrashLoopBackOff`, and a positive restart count.
- Restoration requires the injection manifest and the same live Deployment UID. It
  restores the entire exact prior `resources` object and rollout strategy only when
  both current values match the journaled fault. Any third state is treated as drift
  and refused.
- Restoration first restores the exact resources while retaining `Recreate`, waits
  for the healthy one-replica rollout, and only then restores the exact
  `RollingUpdate` strategy. This avoids a capacity deadlock during recovery.
  Interrupted runs safely resume from either journaled stage. Final verification
  checks generation observation, available replicas, resources, and strategy. A
  repeated restore is a safe no-op.

Generated Phase 11 evidence is placed below `evidence/generated/phase11/`, which is
ignored because it contains live infrastructure identifiers and operational data.

### 1. Validate without injecting

```powershell
.\.venv\Scripts\python.exe .\faults\fault2_oom.py preflight
```

Inspect the generated PASS manifest and confirm it says
`live fault injected: false`. Run all deterministic tests when tooling changed:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\faults -p "test_*.py" -v
```

### 2. Inject and capture the active incident

Run only for the authorized live fault session:

```powershell
.\.venv\Scripts\python.exe .\faults\fault2_oom.py inject --execute
```

The command prints the UTC timestamp, Deployment UID, exact memory change, direct
termination evidence, injection manifest, and restore command. Leave the fault active
until the one-minute Grafana alert evaluation and email notification arrive. Then run:

```powershell
.\.venv\Scripts\python.exe .\faults\capture_fault2.py fault `
  --manifest .\evidence\generated\phase11\fault2-<UTC>\injection.json
```

This capture reruns the scoped verifier with `--state fault2`, which requires the
authorized C1 cart probe to connect and the unauthorized probe to remain denied. It
also preserves the frontend upstream effect, Deployment/ReplicaSet/Pod objects,
events, current and previous logs, active Grafana alerts, and a UTC dashboard render.
It returns `INCOMPLETE` until both `Container OOMKilled` and
`Application container restarted` are active.

Save the firing email screenshot below the incident bundle's `email/` directory
before restoring; mailbox evidence is intentionally a human capture rather than a
repository credential integration.

### 3. Restore the exact previous resources and prove recovery

Preview first:

```powershell
.\.venv\Scripts\python.exe .\faults\fault2_oom.py restore `
  --manifest .\evidence\generated\phase11\fault2-<UTC>\injection.json
```

Then execute the exact command printed by injection:

```powershell
.\.venv\Scripts\python.exe .\faults\fault2_oom.py restore `
  --manifest .\evidence\generated\phase11\fault2-<UTC>\injection.json `
  --execute
```

After the Grafana evaluation window clears and the resolved email arrives, capture
recovery using the exact restoration file printed above:

```powershell
.\.venv\Scripts\python.exe .\faults\capture_fault2.py recovery `
  --manifest .\evidence\generated\phase11\fault2-<UTC>\injection.json `
  --restoration .\evidence\generated\phase11\fault2-<UTC>\restoration-<UTC>.json
```

Recovery passes only when the exact old resources and strategy are live, the rollout
is fully available, the restored verifier passes, the frontend returns HTTP 200,
cart remains healthy, and neither Fault 2 alert is active. Save the resolved email
screenshot in the same `email/` directory.
