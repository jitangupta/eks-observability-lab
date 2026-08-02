[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$namespace = "observability"
$fluentBitChartVersion = "0.57.9"
$prometheusChartVersion = "29.20.1"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$terraformDirectory = Join-Path $repositoryRoot "terraform"
$namespacePath = Join-Path $PSScriptRoot "namespace.yaml"
$fluentBitValuesPath = Join-Path $PSScriptRoot "fluent-bit-values.yaml"
$prometheusValuesPath = Join-Path $PSScriptRoot "prometheus-values.yaml"
$blackboxPath = Join-Path $PSScriptRoot "blackbox-c1.yaml"

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

function Get-TerraformJsonOutput {
  param(
    [Parameter(Mandatory = $true)]
    [string]$OutputName
  )

  $json = & terraform "-chdir=$terraformDirectory" output -json $OutputName
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Terraform output: $OutputName"
  }
  return ($json | Out-String | ConvertFrom-Json)
}

function ConvertTo-Base64 {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Value
  )

  return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Value))
}

function Get-GrafanaCloudToken {
  if (-not [string]::IsNullOrWhiteSpace($env:GRAFANA_CLOUD_TOKEN)) {
    return $env:GRAFANA_CLOUD_TOKEN
  }

  $secureToken = Read-Host "Grafana Cloud ingest token (input is hidden)" -AsSecureString
  $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
  try {
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
  }
}

function Apply-GrafanaSecret {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Context,

    [Parameter(Mandatory = $true)]
    [string]$Token
  )

  $secret = @{
    apiVersion = "v1"
    kind = "Secret"
    metadata = @{
      name = "grafana-cloud-credentials"
      namespace = $namespace
    }
    type = "Opaque"
    data = @{
      token = ConvertTo-Base64 -Value $Token
    }
  }

  $secretJson = $secret | ConvertTo-Json -Depth 6 -Compress
  $secretJson | & kubectl --context $Context apply -f -
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to apply Grafana Cloud credentials in $Context"
  }
}

function Install-ClusterObservability {
  param(
    [Parameter(Mandatory = $true)]
    [string]$ClusterKey,

    [Parameter(Mandatory = $true)]
    [object]$Cluster,

    [Parameter(Mandatory = $true)]
    [string]$Token
  )

  $context = [string]$Cluster.name
  $region = [string]$Cluster.region
  $fluentBitOverlay = Join-Path $PSScriptRoot "fluent-bit-values-${ClusterKey}.yaml"
  $prometheusOverlay = Join-Path $PSScriptRoot "prometheus-values-${ClusterKey}.yaml"

  Write-Host "Preparing $namespace in $context..."
  Invoke-Checked kubectl @("--context", $context, "apply", "-f", $namespacePath)
  Invoke-Checked kubectl @(
    "--context", $context,
    "label", "namespace", $namespace,
    "lab.openai.com/cluster=$ClusterKey",
    "lab.openai.com/region=$region",
    "--overwrite"
  )
  Apply-GrafanaSecret -Context $context -Token $Token

  if ($ClusterKey -eq "c1") {
    Write-Host "Installing C1 blackbox probes..."
    Invoke-Checked kubectl @("--context", $context, "apply", "-f", $blackboxPath)
    Invoke-Checked kubectl @(
      "--context", $context,
      "--namespace", $namespace,
      "rollout", "status", "deployment/blackbox-exporter",
      "--timeout=5m"
    )
  }

  Write-Host "Installing Fluent Bit in $context..."
  Invoke-Checked helm @(
    "upgrade", "--install", "fluent-bit", "fluent/fluent-bit",
    "--kube-context", $context,
    "--namespace", $namespace,
    "--version", $fluentBitChartVersion,
    "--values", $fluentBitValuesPath,
    "--values", $fluentBitOverlay,
    "--atomic",
    "--timeout", "10m"
  )

  Write-Host "Installing Prometheus in $context..."
  Invoke-Checked helm @(
    "upgrade", "--install", "prometheus", "prometheus-community/prometheus",
    "--kube-context", $context,
    "--namespace", $namespace,
    "--version", $prometheusChartVersion,
    "--values", $prometheusValuesPath,
    "--values", $prometheusOverlay,
    "--atomic",
    "--timeout", "10m"
  )

  # A Secret update does not refresh environment variables in an existing Pod.
  # Restart both collectors so token rotation takes effect immediately.
  Write-Host "Restarting collectors in $context to load the current Grafana credential..."
  Invoke-Checked kubectl @(
    "--context", $context,
    "--namespace", $namespace,
    "rollout", "restart", "daemonset/fluent-bit"
  )
  Invoke-Checked kubectl @(
    "--context", $context,
    "--namespace", $namespace,
    "rollout", "restart", "deployment/prometheus-server"
  )
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
}

foreach ($command in @("terraform", "kubectl", "helm")) {
  if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "Required command is not installed or not on PATH: $command"
  }
}

foreach ($path in @($namespacePath, $fluentBitValuesPath, $prometheusValuesPath, $blackboxPath)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Required Phase 7 input not found: $path"
  }
}

$clusters = Get-TerraformJsonOutput -OutputName "clusters"
$token = (Get-GrafanaCloudToken).Trim()
if ([string]::IsNullOrWhiteSpace($token)) {
  throw "Grafana Cloud token cannot be empty"
}
if ($token -notmatch "^glc_") {
  Write-Warning "This does not look like a Grafana Cloud access-policy token (expected a glc_ prefix)."
}

try {
  Invoke-Checked helm @("repo", "add", "fluent", "https://fluent.github.io/helm-charts", "--force-update")
  Invoke-Checked helm @("repo", "add", "prometheus-community", "https://prometheus-community.github.io/helm-charts", "--force-update")
  Invoke-Checked helm @("repo", "update", "fluent", "prometheus-community")

  Install-ClusterObservability -ClusterKey "c1" -Cluster $clusters.c1 -Token $token
  Install-ClusterObservability -ClusterKey "c2" -Cluster $clusters.c2 -Token $token
} finally {
  $token = $null
  Remove-Variable token -ErrorAction SilentlyContinue
}

Write-Host "Phase 7 collectors installed in C1 and C2."
Write-Host "Run kubernetes/observability/verify.ps1, then confirm data in Grafana Explore."
