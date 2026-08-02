[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$chartRepositoryName = "eks"
$chartRepositoryUrl = "https://aws.github.io/eks-charts"
$chartName = "eks/aws-load-balancer-controller"
$chartVersion = "1.14.0"
$releaseName = "aws-load-balancer-controller"
$controllerNamespace = "kube-system"
$applicationNamespace = "online-boutique"
$valuesPath = Join-Path $PSScriptRoot "values.yaml"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
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

function Ensure-ApplicationNamespace {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Context,

    [Parameter(Mandatory = $true)]
    [string]$ClusterLabel,

    [Parameter(Mandatory = $true)]
    [string]$Region
  )

  $existingNamespace = & kubectl --context $Context get namespace $applicationNamespace --ignore-not-found -o name
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to query namespace $applicationNamespace in context $Context"
  }

  if ([string]::IsNullOrWhiteSpace(($existingNamespace | Out-String).Trim())) {
    Invoke-Checked kubectl @(
      "--context", $Context,
      "create", "namespace", $applicationNamespace
    )
  }

  Invoke-Checked kubectl @(
    "--context", $Context,
    "label", "namespace", $applicationNamespace,
    "app.kubernetes.io/part-of=online-boutique",
    "lab.openai.com/cluster=$ClusterLabel",
    "lab.openai.com/region=$Region",
    "lab.openai.com/telemetry=enabled",
    "--overwrite"
  )
}

function Install-Controller {
  param(
    [Parameter(Mandatory = $true)]
    [string]$ClusterKey,

    [Parameter(Mandatory = $true)]
    [object]$Clusters,

    [Parameter(Mandatory = $true)]
    [object]$Networking,

    [Parameter(Mandatory = $true)]
    [object]$IamRoles
  )

  $cluster = $Clusters.PSObject.Properties[$ClusterKey].Value
  $network = $Networking.PSObject.Properties[$ClusterKey].Value
  $rolePropertyName = "${ClusterKey}_load_balancer_controller"
  $roleArn = $IamRoles.PSObject.Properties[$rolePropertyName].Value
  $context = $cluster.name

  if ([string]::IsNullOrWhiteSpace($roleArn)) {
    throw "Terraform output iam_roles.$rolePropertyName is empty"
  }

  Ensure-ApplicationNamespace `
    -Context $context `
    -ClusterLabel $ClusterKey `
    -Region $cluster.region

  Write-Host "Installing AWS Load Balancer Controller in $context..."

  Invoke-Checked helm @(
    "upgrade", "--install", $releaseName, $chartName,
    "--kube-context", $context,
    "--namespace", $controllerNamespace,
    "--version", $chartVersion,
    "--values", $valuesPath,
    "--set-string", "clusterName=$($cluster.name)",
    "--set-string", "region=$($cluster.region)",
    "--set-string", "vpcId=$($network.vpc_id)",
    "--set-string", "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$roleArn",
    "--atomic",
    "--timeout", "10m"
  )
}

foreach ($command in @("terraform", "kubectl", "helm")) {
  if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "Required command is not installed or not on PATH: $command"
  }
}

if (-not (Test-Path -LiteralPath $valuesPath -PathType Leaf)) {
  throw "Controller values file not found: $valuesPath"
}

$clusters = Get-TerraformJsonOutput -OutputName "clusters"
$networking = Get-TerraformJsonOutput -OutputName "networking"
$iamRoles = Get-TerraformJsonOutput -OutputName "iam_roles"

Invoke-Checked helm @(
  "repo", "add", $chartRepositoryName, $chartRepositoryUrl, "--force-update"
)
Invoke-Checked helm @("repo", "update", $chartRepositoryName)

Install-Controller -ClusterKey "c1" -Clusters $clusters -Networking $networking -IamRoles $iamRoles
Install-Controller -ClusterKey "c2" -Clusters $clusters -Networking $networking -IamRoles $iamRoles

Write-Host "AWS Load Balancer Controller installation completed for C1 and C2."

