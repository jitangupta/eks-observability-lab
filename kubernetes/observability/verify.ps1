[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$namespace = "observability"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$terraformDirectory = Join-Path $repositoryRoot "terraform"

function Invoke-Checked {
  param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,

    [Parameter(Mandatory = $true)]
    [string[]]$ArgumentList
  )

  & $FilePath @ArgumentList
  if ($LASTEXITCODE -ne 0) {
    throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
  }
}

foreach ($command in @("terraform", "kubectl", "helm")) {
  if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "Required command is not installed or not on PATH: $command"
  }
}

$clustersJson = & terraform "-chdir=$terraformDirectory" output -json clusters
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read Terraform cluster outputs"
}
$clusters = $clustersJson | Out-String | ConvertFrom-Json

foreach ($clusterKey in @("c1", "c2")) {
  $context = [string]$clusters.PSObject.Properties[$clusterKey].Value.name
  Write-Host "Verifying collectors in $context..."

  Invoke-Checked helm @("status", "fluent-bit", "--kube-context", $context, "--namespace", $namespace)
  Invoke-Checked helm @("status", "prometheus", "--kube-context", $context, "--namespace", $namespace)
  Invoke-Checked kubectl @(
    "--context", $context,
    "--namespace", $namespace,
    "rollout", "status", "daemonset/fluent-bit",
    "--timeout=5m"
  )
  Invoke-Checked kubectl @(
    "--context", $context,
    "--namespace", $namespace,
    "rollout", "status", "deployment/prometheus-server",
    "--timeout=5m"
  )

  $prometheusLogs = & kubectl --context $context --namespace $namespace logs deployment/prometheus-server -c prometheus-server --tail=200
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Prometheus logs in $context"
  }
  $prometheusFailureLines = @(
    $prometheusLogs | Select-String -Pattern "(?i)(remote write.*(401|403)|non-recoverable error|server returned HTTP status (401|403))"
  )
  if ($prometheusFailureLines.Count -gt 0) {
    Write-Host "Prometheus remote-write errors from ${context}:" -ForegroundColor Red
    $prometheusFailureLines | ForEach-Object { Write-Host $_.Line -ForegroundColor Red }
    throw "Prometheus reports a Grafana Cloud remote-write authentication error in $context"
  }

  $fluentBitLogs = & kubectl --context $context --namespace $namespace logs daemonset/fluent-bit --tail=200
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Fluent Bit logs in $context"
  }
  $fluentBitFailureLines = @(
    $fluentBitLogs | Select-String -Pattern "(?i)(HTTP status=(4[0-9]{2})|cannot flush.*retry in.*no retries left|loki.*(error|failed))"
  )
  if ($fluentBitFailureLines.Count -gt 0) {
    Write-Host "Fluent Bit Loki errors from ${context}:" -ForegroundColor Red
    $fluentBitFailureLines | ForEach-Object { Write-Host $_.Line -ForegroundColor Red }
    throw "Fluent Bit reports a Grafana Cloud Loki authentication or delivery error in $context"
  }
}

$c1Context = [string]$clusters.c1.name
Invoke-Checked kubectl @(
  "--context", $c1Context,
  "--namespace", $namespace,
  "rollout", "status", "deployment/blackbox-exporter",
  "--timeout=5m"
)

$probeOutput = & kubectl `
  --context $c1Context `
  --namespace $namespace `
  exec deployment/prometheus-server `
  -c prometheus-server `
  -- /bin/promtool query instant http://127.0.0.1:9090 probe_success
if ($LASTEXITCODE -ne 0) {
  throw "Unable to query C1 Prometheus from inside the Prometheus Pod"
}

foreach ($job in @("cross-region-cart", "c1-frontend")) {
  $result = @($probeOutput | Select-String -SimpleMatch "job=`"$job`"")
  if ($result.Count -ne 1 -or $result[0].Line -notmatch "=>\s+1(?:\.0+)?(?:\s|$)") {
    throw "Probe is not healthy: $job"
  }
  Write-Host "PASS: $job returned probe_success=1"
}

Write-Host "Collector and probe verification passed."
Write-Host "Next: confirm c1/c2 labels in Grafana Explore for metrics and logs."
