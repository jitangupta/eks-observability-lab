# AWS Load Balancer Controller installation

This directory contains the reproducible Phase 3 installation for both EKS
clusters. It installs the controller pods only. The actual AWS-managed NLB and ALB
are created later from the Phase 4 Service and Phase 5 Ingress resources.

## Version pin

- Helm repository: `https://aws.github.io/eks-charts`
- Chart: `eks/aws-load-balancer-controller`
- Chart version: `1.14.0`
- Controller version: `v2.14.1`

The version pair follows the AWS EKS Helm installation guide:
<https://docs.aws.amazon.com/eks/latest/userguide/lbc-helm.html>.

## Inputs and ownership

Terraform owns the EKS clusters, VPCs, VPC CNI configuration, subnet tags, and the
two cluster-specific IRSA roles. The installer reads these current Terraform outputs:

- `clusters`
- `networking`
- `iam_roles`

Cluster names, contexts, regions, VPC IDs, and IAM role ARNs are therefore not
hardcoded. This is important after `terraform destroy` followed by a new apply,
because resource IDs and generated IAM role names can change.

The script also creates the `online-boutique` namespace when absent and applies the
regional labels required by later telemetry configuration. The controller itself is
installed into `kube-system` with a two-replica deployment and a Helm-managed
ServiceAccount annotated with the appropriate IRSA role.

## Prerequisites

1. Complete Phase 1 and apply Terraform successfully.
2. Run both `kubeconfig_commands` from the Terraform output so the context aliases
   match the EKS cluster names.
3. Install `terraform`, `kubectl`, and `helm` and make them available on `PATH`.
4. Run the script with AWS credentials that can access both clusters.

## Install or recreate

From the repository root in Windows PowerShell:

```powershell
& .\kubernetes\controllers\aws-load-balancer-controller\install.ps1
```

The script uses `helm upgrade --install`, so the same command handles a clean
installation and an idempotent reinstall of the pinned release. Helm's `--atomic`
behavior rolls back a failed controller release rather than leaving a partially
installed release.

Gateway API CRDs, cert-manager, ExternalDNS, and service-mesh components are not
installed. The lab uses Kubernetes `Ingress` and `Service` resources and does not
require those additional controllers.

