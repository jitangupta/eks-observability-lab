[CmdletBinding()]
param(
  [switch]$AcknowledgeTemporaryFallback
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $AcknowledgeTemporaryFallback) {
  throw "This does not satisfy the Phase 4/5 load-balancer gates. Rerun with -AcknowledgeTemporaryFallback to continue temporarily."
}

$releaseName = "online-boutique"
$applicationNamespace = "online-boutique"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$terraformDirectory = Join-Path $repositoryRoot "terraform"
$chartPath = Join-Path $repositoryRoot "kubernetes\charts\online-boutique"
$valuesPath = Join-Path $repositoryRoot "kubernetes\online-boutique\values-c1.yaml"
$fallbackTemplatePath = Join-Path $PSScriptRoot "cartservice-nodeport-fallback.yaml.tmpl"

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

foreach ($command in @("terraform", "kubectl", "helm")) {
  if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "Required command is not installed or not on PATH: $command"
  }
}

foreach ($path in @($chartPath, $valuesPath, $fallbackTemplatePath)) {
  if (-not (Test-Path -LiteralPath $path)) {
    throw "Required Phase 5 fallback input not found: $path"
  }
}

$clusters = Get-TerraformJsonOutput -OutputName "clusters"
$c1Context = $clusters.c1.name
$c2Context = $clusters.c2.name

Invoke-Checked kubectl @("--context", $c1Context, "get", "namespace", $applicationNamespace)
Invoke-Checked kubectl @("--context", $c2Context, "get", "namespace", $applicationNamespace)

$nodePortServiceJson = & kubectl --context $c2Context --namespace $applicationNamespace get service cartservice-nodeport -o json
if ($LASTEXITCODE -ne 0) {
  throw "The temporary C2 cartservice-nodeport Service does not exist"
}
$nodePortService = $nodePortServiceJson | Out-String | ConvertFrom-Json
$c2CartNodePort = [int]$nodePortService.spec.ports[0].nodePort
if ($c2CartNodePort -ne 30770) {
  throw "Expected the approved temporary C2 NodePort 30770, found $c2CartNodePort"
}

$nodesJson = & kubectl --context $c2Context get nodes -o json
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read C2 nodes"
}
$nodes = ($nodesJson | Out-String | ConvertFrom-Json).items
$readyNodes = @($nodes | Where-Object {
  ($_.status.conditions | Where-Object { $_.type -eq "Ready" }).status -eq "True"
})
if ($readyNodes.Count -ne 1) {
  throw "The temporary fallback requires exactly one Ready C2 node; found $($readyNodes.Count)"
}
$c2NodePrivateIp = ($readyNodes[0].status.addresses | Where-Object { $_.type -eq "InternalIP" }).address
if ($c2NodePrivateIp -notmatch "^10\.20\.(?:[0-9]{1,3}\.)[0-9]{1,3}$") {
  throw "C2 node address is not inside the expected 10.20.0.0/16 range: $c2NodePrivateIp"
}

$fallbackManifest = Get-Content -Raw -LiteralPath $fallbackTemplatePath
$fallbackManifest = $fallbackManifest.Replace("__C2_CART_NODE_PORT__", [string]$c2CartNodePort)
$fallbackManifest = $fallbackManifest.Replace("__C2_NODE_PRIVATE_IP__", $c2NodePrivateIp)
if ($fallbackManifest -match "__[A-Z0-9_]+__") {
  throw "The rendered NodePort fallback still contains an unresolved placeholder"
}

Write-Warning "Using temporary C2 node endpoint ${c2NodePrivateIp}:${c2CartNodePort}; this is not the target NLB architecture."
$fallbackManifest | & kubectl --context $c1Context apply -f -
if ($LASTEXITCODE -ne 0) {
  throw "Unable to apply the C1 cartservice NodePort fallback"
}

Write-Host "Installing the C1 Online Boutique application tier in $c1Context..."
Invoke-Checked helm @(
  "upgrade", "--install", $releaseName, $chartPath,
  "--kube-context", $c1Context,
  "--namespace", $applicationNamespace,
  "--values", $valuesPath,
  "--atomic",
  "--timeout", "15m"
)

$unexpectedExternalService = (& kubectl --context $c1Context --namespace $applicationNamespace get service frontend-external --ignore-not-found -o name | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
  throw "Unable to verify that frontend-external is absent"
}
if (-not [string]::IsNullOrWhiteSpace($unexpectedExternalService)) {
  throw "Unexpected public Service exists in C1: $unexpectedExternalService"
}

Write-Host "Temporary Phase 5 continuation is installed."
Write-Host "Verify locally with: kubectl --context $c1Context -n $applicationNamespace port-forward service/frontend 8080:80"
