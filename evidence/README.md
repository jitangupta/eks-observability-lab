# Phase 9: healthy baseline evidence

`capture_healthy.py` creates one immutable, UTC-stamped evidence bundle before either
fault is injected. It is a strict gate: every required Phase 9 check must pass for
the command to return zero and write a `PASS` manifest.

## Prerequisites

Complete the Phase 8 healthy gate first. The command uses the same Python packages,
AWS identity, Terraform state, `kubectl` contexts, and AWS CLI-on-`PATH` requirement
documented in [`../verification/README.md`](../verification/README.md).

Use the repository-local ignored virtual environment so the verifier and capture
command use the same dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\verification\requirements.txt
aws sts get-caller-identity
```

The Grafana portion requires a short-lived, read-only service-account token that can
read dashboards and alert rules. It is read only from an environment variable and
is never written to the bundle. Keep the dashboard UID
`eks-cross-region-incident-lab`; use `--grafana-dashboard-uid` only if the imported
dashboard was deliberately renamed.

From the repository root in PowerShell, prompt for the token without adding it to
shell history, run the capture, and remove the process-scoped value:

```powershell
$grafanaSecret = Read-Host "Grafana read-only service-account token" -AsSecureString
$grafanaPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($grafanaSecret)
try {
  $env:GRAFANA_SERVICE_ACCOUNT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($grafanaPointer)
  .\.venv\Scripts\python.exe .\evidence\capture_healthy.py
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($grafanaPointer)
  Remove-Item Env:GRAFANA_SERVICE_ACCOUNT_TOKEN -ErrorAction SilentlyContinue
}
```

Add `--profile NAME` when the Terraform deployment uses a named AWS profile. Use
`--application-url https://...` only when DNS/TLS routing differs from the ALB
hostname discovered by the verifier.

## Captured evidence

Each run writes `evidence/generated/phase9/healthy-<UTC>/` containing:

- `SUMMARY.txt` and machine-readable `manifest.json`;
- the full Phase 8 human and JSON verifier reports, including the authorized and
  unauthorized probes;
- an HTTP user journey through home, product, add-to-cart, cart, and checkout, with
  status, latency, response size, and response hashes but no response bodies;
- the C1 blackbox probe's current cart success and duration values;
- C1 and C2 application Pod readiness/restart snapshots and a Redis `PONG` record;
- ALB/NLB inventory and target health plus the WAF association extracted from the
  just-completed verifier;
- the Grafana dashboard definition, alert-rule definition, active-alert state, and
  a rendered PNG covering the preceding UTC hour.

Generated evidence remains ignored by Git because it can contain account and
resource identifiers. Review it and copy only deliberately selected, redacted
artifacts into a publication location.

## Gate behavior

Exit code `0` means every Phase 9 item passed. Exit code `1` means the bundle is
useful but incomplete or unhealthy; inspect `SUMMARY.txt` and do not inject a fault.
Exit code `2` means the bundle or final report could not be written.

Run deterministic tests without cloud access:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\evidence -p "test_*.py" -v
```
