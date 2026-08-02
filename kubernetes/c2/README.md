# Phase 4: C2 cartservice, Redis, and internal NLB

Phase 4 deploys C2 before C1 so the private cart endpoint exists before the C1
`cartservice` alias is created.

## Installed resources

- Helm release `online-boutique` in the C2 `online-boutique` namespace.
- `cartservice` Deployment and ClusterIP Service on TCP/7070.
- One resource-bounded `redis-cart` Deployment and ClusterIP Service on TCP/6379.
- A second Service named `cartservice-internal` that provisions an AWS-managed,
  internal Network Load Balancer on TCP/7070.

The NLB uses IP targets, so traffic is sent directly to cartservice Pod IPs through
the Amazon VPC CNI. Cross-zone load balancing is enabled because this lab runs one
cartservice replica across two NLB subnets.

## Dynamic Terraform inputs

`install.ps1` reads the current C2 cluster name, private subnet IDs, and
Terraform-created NLB security-group ID. The generated IDs are not stored in the
manifest, so the same installation works after a full destroy and recreation.

The security-group annotation is present on the first creation of the Service. The
controller is told not to manage backend security-group rules because Terraform
already permits the NLB security group to reach C2 targets on TCP/7070.

The NLB security group accepts application traffic only from the C1 VPC CIDR. As a
result, the controlled cross-region NLB test must originate from C1. Testing the NLB
from C2 would conflict with the locked security boundary; C2-local health is tested
against the cartservice and Redis ClusterIP endpoints instead.

## Prerequisites

1. Complete Phases 1 through 3.
2. Ensure the C2 kubeconfig context alias matches the Terraform cluster name.
3. Ensure the AWS Load Balancer Controller is installed in C2.
4. Make `terraform`, `kubectl`, and `helm` available on `PATH`.

## Install or recreate

From the repository root in Windows PowerShell:

```powershell
& .\kubernetes\c2\install.ps1
```

The Helm deployment is atomic. The NLB Service is applied only after the cart and
Redis release becomes ready. The Service template is retained separately so the AWS
annotations remain reviewable.

Several NLB annotations, including its name, scheme, target type, subnets, and
security-group attachment, should be treated as creation-time settings. If one of
those settings must change, delete the `cartservice-internal` Service, wait for AWS
to remove the old NLB, and then rerun the installer.

Before destroying the Terraform infrastructure, delete the `cartservice-internal`
Service and wait for its AWS NLB to disappear. This prevents an orphaned load
balancer, target group, or security-group dependency from blocking Terraform.
