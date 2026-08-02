# Phase 10 Fault 1: NACL inject and restore

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

## 1. Validate without injecting

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

## 2. Inject only for the authorized live incident

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

## 3. Restore the journaled injection

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
