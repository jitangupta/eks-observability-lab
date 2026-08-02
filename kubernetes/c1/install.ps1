[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$releaseName = "online-boutique"
$applicationNamespace = "online-boutique"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$terraformDirectory = Join-Path $repositoryRoot "terraform"
$chartPath = Join-Path $repositoryRoot "kubernetes\charts\online-boutique"
$valuesPath = Join-Path $repositoryRoot "kubernetes\online-boutique\values-c1.yaml"
$aliasTemplatePath = Join-Path $PSScriptRoot "cartservice-alias.yaml.tmpl"
$ingressTemplatePath = Join-Path $PSScriptRoot "frontend-ingress.yaml.tmpl"

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

function Apply-Manifest {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Context,

    [Parameter(Mandatory = $true)]
    [string]$Manifest,

    [Parameter(Mandatory = $true)]
    [string]$Description
  )

  $Manifest | & kubectl --context $Context apply -f -
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to apply $Description"
  }
}

foreach ($command in @("terraform", "kubectl", "helm")) {
  if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "Required command is not installed or not on PATH: $command"
  }
}

foreach ($path in @($chartPath, $valuesPath, $aliasTemplatePath, $ingressTemplatePath)) {
  if (-not (Test-Path -LiteralPath $path)) {
    throw "Required Phase 5 input not found: $path"
  }
}

$clusters = Get-TerraformJsonOutput -OutputName "clusters"
$networking = Get-TerraformJsonOutput -OutputName "networking"
$security = Get-TerraformJsonOutput -OutputName "security"

$c1Context = $clusters.c1.name
$c2Context = $clusters.c2.name
$c1PublicSubnetIds = $networking.c1.public_subnet_ids -join ","
$c1AlbSecurityGroupId = $security.c1_alb_security_group_id
$c1WafWebAclArn = $security.waf_web_acl_arn

foreach ($requiredValue in @{
  "networking.c1.public_subnet_ids" = $c1PublicSubnetIds
  "security.c1_alb_security_group_id" = $c1AlbSecurityGroupId
  "security.waf_web_acl_arn" = $c1WafWebAclArn
}.GetEnumerator()) {
  if ([string]::IsNullOrWhiteSpace([string]$requiredValue.Value)) {
    throw "Terraform output $($requiredValue.Key) is empty"
  }
}

Invoke-Checked kubectl @("--context", $c1Context, "get", "namespace", $applicationNamespace)
Invoke-Checked kubectl @("--context", $c2Context, "get", "namespace", $applicationNamespace)

# Enforce the Phase 4 exit gate before creating any Phase 5 resource. An ExternalName
# Service cannot point at a pending load balancer and must use a DNS name, not an IP.
$c2CartNlbHostname = (& kubectl --context $c2Context --namespace $applicationNamespace get service cartservice-internal -o "jsonpath={.status.loadBalancer.ingress[0].hostname}" | Out-String).Trim().TrimEnd(".")
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read the C2 cartservice-internal Service"
}
if ([string]::IsNullOrWhiteSpace($c2CartNlbHostname)) {
  throw "Phase 4 exit gate is not met: cartservice-internal has no NLB hostname"
}
if ($c2CartNlbHostname -notmatch "^[a-zA-Z0-9.-]+\.elb\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$") {
  throw "C2 cart endpoint is not an AWS load-balancer DNS name: $c2CartNlbHostname"
}

$renderedChart = (& helm template $releaseName $chartPath --namespace $applicationNamespace --values $valuesPath | Out-String)
if ($LASTEXITCODE -ne 0) {
  throw "Unable to render the C1 Online Boutique chart"
}
if ($renderedChart -match "(?m)^\s*name:\s*frontend-external\s*$") {
  throw "The C1 chart unexpectedly renders the public frontend-external Service"
}

$aliasManifest = Get-Content -Raw -LiteralPath $aliasTemplatePath
$aliasManifest = $aliasManifest.Replace("__C2_CART_NLB_HOSTNAME__", $c2CartNlbHostname)
if ($aliasManifest -match "__[A-Z0-9_]+__") {
  throw "The rendered cartservice alias still contains an unresolved placeholder"
}

Write-Host "Creating the C1 cartservice alias for $c2CartNlbHostname..."
Apply-Manifest -Context $c1Context -Manifest $aliasManifest -Description "the C1 cartservice alias"

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

$ingressManifest = Get-Content -Raw -LiteralPath $ingressTemplatePath
$ingressManifest = $ingressManifest.Replace("__C1_PUBLIC_SUBNET_IDS__", $c1PublicSubnetIds)
$ingressManifest = $ingressManifest.Replace("__C1_ALB_SECURITY_GROUP_ID__", $c1AlbSecurityGroupId)
$ingressManifest = $ingressManifest.Replace("__C1_WAF_WEB_ACL_ARN__", $c1WafWebAclArn)
if ($ingressManifest -match "__[A-Z0-9_]+__") {
  throw "The rendered frontend Ingress still contains an unresolved placeholder"
}

Write-Host "Creating the internet-facing C1 ALB Ingress with the WAF WebACL attached..."
Apply-Manifest -Context $c1Context -Manifest $ingressManifest -Description "the C1 frontend Ingress"

Write-Host "C1 application installation submitted. Waiting for an ALB hostname..."
$deadline = [DateTime]::UtcNow.AddMinutes(10)
$albHostname = ""
do {
  $albHostname = (& kubectl --context $c1Context --namespace $applicationNamespace get ingress frontend -o "jsonpath={.status.loadBalancer.ingress[0].hostname}" | Out-String).Trim()
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to read the C1 frontend Ingress status"
  }
  if ([string]::IsNullOrWhiteSpace($albHostname)) {
    Start-Sleep -Seconds 5
  }
} while ([string]::IsNullOrWhiteSpace($albHostname) -and [DateTime]::UtcNow -lt $deadline)

if ([string]::IsNullOrWhiteSpace($albHostname)) {
  throw "The C1 frontend Ingress did not receive an ALB hostname within 10 minutes"
}

Write-Host "Phase 5 resources are ready for user-journey verification: http://$albHostname"
