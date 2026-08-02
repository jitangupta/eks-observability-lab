[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$releaseName = "online-boutique"
$applicationNamespace = "online-boutique"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$terraformDirectory = Join-Path $repositoryRoot "terraform"
$chartPath = Join-Path $repositoryRoot "kubernetes\charts\online-boutique"
$valuesPath = Join-Path $repositoryRoot "kubernetes\online-boutique\values-c2.yaml"
$nlbTemplatePath = Join-Path $PSScriptRoot "cartservice-nlb.yaml.tmpl"

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

foreach ($path in @($chartPath, $valuesPath, $nlbTemplatePath)) {
  if (-not (Test-Path -LiteralPath $path)) {
    throw "Required Phase 4 input not found: $path"
  }
}

$clusters = Get-TerraformJsonOutput -OutputName "clusters"
$networking = Get-TerraformJsonOutput -OutputName "networking"
$security = Get-TerraformJsonOutput -OutputName "security"

$context = $clusters.c2.name
$privateSubnetIds = $networking.c2.private_subnet_ids -join ","
$nlbSecurityGroupId = $security.c2_cart_nlb_security_group_id

if ([string]::IsNullOrWhiteSpace($privateSubnetIds)) {
  throw "Terraform output networking.c2.private_subnet_ids is empty"
}
if ([string]::IsNullOrWhiteSpace($nlbSecurityGroupId)) {
  throw "Terraform output security.c2_cart_nlb_security_group_id is empty"
}

Invoke-Checked kubectl @(
  "--context", $context,
  "get", "namespace", $applicationNamespace
)

Write-Host "Installing cartservice and Redis in $context..."
Invoke-Checked helm @(
  "upgrade", "--install", $releaseName, $chartPath,
  "--kube-context", $context,
  "--namespace", $applicationNamespace,
  "--values", $valuesPath,
  "--atomic",
  "--timeout", "10m"
)

$nlbManifest = Get-Content -Raw -LiteralPath $nlbTemplatePath
$nlbManifest = $nlbManifest.Replace("__C2_PRIVATE_SUBNET_IDS__", $privateSubnetIds)
$nlbManifest = $nlbManifest.Replace("__C2_NLB_SECURITY_GROUP_ID__", $nlbSecurityGroupId)

if ($nlbManifest -match "__[A-Z0-9_]+__") {
  throw "The rendered NLB Service still contains an unresolved placeholder"
}

Write-Host "Creating the internal cartservice NLB Service with its security group attached..."
$nlbManifest | & kubectl --context $context apply -f -
if ($LASTEXITCODE -ne 0) {
  throw "Unable to apply the internal cartservice NLB Service"
}

Write-Host "C2 cartservice, Redis, and internal NLB Service installation submitted."
