[CmdletBinding()]
param(
  [switch]$UseNodePortFallback
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$applicationNamespace = "online-boutique"
$probeName = "phase6-unauthorized"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$terraformDirectory = Join-Path $repositoryRoot "terraform"
$probePath = Join-Path $PSScriptRoot "probe-deployment.yaml"

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

function Invoke-ExpectedFailureProbe {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Context,

    [Parameter(Mandatory = $true)]
    [string]$HostName,

    [Parameter(Mandatory = $true)]
    [int]$Port
  )

  $previousErrorActionPreference = $ErrorActionPreference
  $probeExitCode = 0
  try {
    $ErrorActionPreference = "Continue"
    $null = & kubectl --context $Context --namespace $applicationNamespace exec "deployment/$probeName" -- nc -z -w 5 $HostName $Port 2>&1
    $probeExitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousErrorActionPreference
  }

  if ($probeExitCode -eq 0) {
    throw "Unauthorized probe unexpectedly connected to ${HostName}:${Port} in $Context"
  }
  Write-Host "PASS: unauthorized Deployment cannot connect to ${HostName}:${Port} in $Context"
}

foreach ($command in @("terraform", "kubectl")) {
  if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "Required command is not installed or not on PATH: $command"
  }
}
if (-not (Test-Path -LiteralPath $probePath)) {
  throw "Required Phase 6 probe manifest not found: $probePath"
}

$clustersJson = & terraform "-chdir=$terraformDirectory" output -json clusters
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read Terraform cluster outputs"
}
$clusters = $clustersJson | Out-String | ConvertFrom-Json
$c1Context = $clusters.c1.name
$c2Context = $clusters.c2.name

foreach ($context in @($c1Context, $c2Context)) {
  $policyCount = [int](& kubectl --context $context --namespace $applicationNamespace get networkpolicy -o "jsonpath={.items[*].metadata.name}" | ForEach-Object { @($_ -split " " | Where-Object { $_ }).Count })
  if ($LASTEXITCODE -ne 0 -or $policyCount -lt 5) {
    throw "Expected at least five NetworkPolicies in $context; found $policyCount"
  }
  $policyEndpointCount = [int](& kubectl --context $context --namespace $applicationNamespace get policyendpoints -o "jsonpath={.items[*].metadata.name}" | ForEach-Object { @($_ -split " " | Where-Object { $_ }).Count })
  if ($LASTEXITCODE -ne 0 -or $policyEndpointCount -eq 0) {
    throw "No VPC CNI PolicyEndpoints reconciled in $context"
  }
}

if ($UseNodePortFallback) {
  Invoke-Checked kubectl @(
    "--context", $c1Context,
    "--namespace", $applicationNamespace,
    "get", "networkpolicy", "allow-cart-nodeport-fallback"
  )
}

foreach ($context in @($c1Context, $c2Context)) {
  Invoke-Checked kubectl @("--context", $context, "apply", "-f", $probePath)
  Invoke-Checked kubectl @(
    "--context", $context,
    "--namespace", $applicationNamespace,
    "rollout", "status", "deployment/$probeName",
    "--timeout", "2m"
  )
}

try {
  Invoke-ExpectedFailureProbe -Context $c1Context -HostName "cartservice" -Port 7070
  Invoke-ExpectedFailureProbe -Context $c2Context -HostName "redis-cart" -Port 6379

  Invoke-Checked kubectl @(
    "--context", $c1Context,
    "--namespace", $applicationNamespace,
    "wait", "deployment", "--all", "--for=condition=Available",
    "--timeout", "2m"
  )
  Invoke-Checked kubectl @(
    "--context", $c2Context,
    "--namespace", $applicationNamespace,
    "wait", "deployment", "--all", "--for=condition=Available",
    "--timeout", "2m"
  )

  $cartLogs = & kubectl --context $c2Context --namespace $applicationNamespace logs deployment/cartservice --since=5m --tail=200
  $cartLogText = $cartLogs | Out-String
  if ($LASTEXITCODE -ne 0 -or $cartLogText -notmatch "(?:GetCart|AddItem|EmptyCart)Async called") {
    throw "No recent authorized C1 cart traffic was observed in C2 cartservice logs"
  }
  Write-Host "PASS: authorized C1 application traffic reached C2 cartservice"

  $redisPing = & kubectl --context $c2Context --namespace $applicationNamespace exec deployment/redis-cart -- redis-cli ping
  if ($LASTEXITCODE -ne 0 -or ($redisPing | Out-String).Trim() -ne "PONG") {
    throw "Redis health verification failed"
  }
  Write-Host "PASS: Redis is healthy"
} finally {
  foreach ($context in @($c1Context, $c2Context)) {
    & kubectl --context $context --namespace $applicationNamespace delete deployment $probeName --ignore-not-found --wait=true
  }
}

Write-Host "Phase 6 positive and negative policy verification passed."
