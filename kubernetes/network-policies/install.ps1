[CmdletBinding()]
param(
  [switch]$UseNodePortFallback
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$applicationNamespace = "online-boutique"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$terraformDirectory = Join-Path $repositoryRoot "terraform"
$c1PolicyPath = Join-Path $PSScriptRoot "c1.yaml"
$c2PolicyPath = Join-Path $PSScriptRoot "c2.yaml"
$fallbackTemplatePath = Join-Path $PSScriptRoot "c1-nodeport-fallback.yaml.tmpl"

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

function Apply-ManifestFile {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Context,

    [Parameter(Mandatory = $true)]
    [string]$Path
  )

  Invoke-Checked kubectl @("--context", $Context, "apply", "-f", $Path)
}

function Assert-NetworkPolicySupport {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Context
  )

  Invoke-Checked kubectl @("--context", $Context, "get", "customresourcedefinition", "policyendpoints.networking.k8s.aws")
  $agentContainers = & kubectl --context $Context --namespace kube-system get daemonset aws-node -o "jsonpath={.spec.template.spec.containers[*].name}"
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the VPC CNI DaemonSet in $Context"
  }
  if ($agentContainers -notmatch "aws-eks-nodeagent") {
    throw "The VPC CNI NetworkPolicy agent is not present in $Context"
  }
}

foreach ($command in @("terraform", "kubectl")) {
  if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "Required command is not installed or not on PATH: $command"
  }
}

foreach ($path in @($c1PolicyPath, $c2PolicyPath, $fallbackTemplatePath)) {
  if (-not (Test-Path -LiteralPath $path)) {
    throw "Required Phase 6 input not found: $path"
  }
}

$clustersJson = & terraform "-chdir=$terraformDirectory" output -json clusters
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read Terraform cluster outputs"
}
$clusters = $clustersJson | Out-String | ConvertFrom-Json
$c1Context = $clusters.c1.name
$c2Context = $clusters.c2.name

foreach ($context in @($c1Context, $c2Context)) {
  Invoke-Checked kubectl @("--context", $context, "get", "namespace", $applicationNamespace)
  Assert-NetworkPolicySupport -Context $context
}

if ($UseNodePortFallback) {
  $cartServiceJson = & kubectl --context $c1Context --namespace $applicationNamespace get service cartservice -o json
  if ($LASTEXITCODE -ne 0) {
    throw "The C1 cartservice fallback Service does not exist"
  }
  $cartService = $cartServiceJson | Out-String | ConvertFrom-Json
  if ($cartService.metadata.labels.'lab.openai.com/temporary-workaround' -ne "nodeport") {
    throw "C1 cartservice is not labeled as the approved NodePort fallback"
  }
  $cartClusterIp = [string]$cartService.spec.clusterIP
  if ($cartClusterIp -notmatch "^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$") {
    throw "C1 cartservice has no usable ClusterIP: $cartClusterIp"
  }

  $fallbackManifest = Get-Content -Raw -LiteralPath $fallbackTemplatePath
  $fallbackManifest = $fallbackManifest.Replace("__C1_CART_SERVICE_CLUSTER_IP__", $cartClusterIp)
  if ($fallbackManifest -match "__[A-Z0-9_]+__") {
    throw "The rendered fallback policy still contains an unresolved placeholder"
  }

  Write-Warning "Applying a temporary TCP/30770 egress policy for the NodePort fallback."
  $fallbackManifest | & kubectl --context $c1Context apply -f -
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to apply the C1 NodePort fallback policy"
  }
} else {
  Invoke-Checked kubectl @(
    "--context", $c1Context,
    "--namespace", $applicationNamespace,
    "delete", "networkpolicy", "allow-cart-nodeport-fallback",
    "--ignore-not-found"
  )
}

# Allow policies appear before default-deny in each file, reducing the transient
# disruption while the VPC CNI controller reconciles the full policy set.
Apply-ManifestFile -Context $c1Context -Path $c1PolicyPath
Apply-ManifestFile -Context $c2Context -Path $c2PolicyPath

Write-Host "Phase 6 NetworkPolicies applied to C1 and C2."
Write-Host "Run verify.ps1 with the same fallback switch after PolicyEndpoints reconcile."
