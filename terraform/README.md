# Terraform: AWS foundation

This root configuration creates the disposable two-region foundation described in
the repository architecture:

- C1 in `us-east-1` and C2 in `us-west-2`, each with two public and two private
  subnets and one NAT gateway.
- One EKS managed worker in each cluster by default (`t3a.large` in C1 and
  `t3a.medium` in C2), in private subnets.
- AWS-managed EKS control planes, control-plane logging, restricted API endpoints,
  IRSA, and VPC CNI network-policy enforcement.
- Inter-region peering with peer-CIDR routes only on private route tables.
- A dedicated C2 private-subnet NACL for the deterministic TCP/7070 fault.
- Security groups for the future public ALB and internal cart NLB.
- A regional WAF WebACL with AWS managed common rules and per-IP rate limiting.
- `REJECT`-only, one-minute VPC Flow Logs and seven-day CloudWatch retention.
- AWS Load Balancer Controller IRSA roles for both clusters.

The configuration intentionally does **not** create either load balancer. The AWS
Load Balancer Controller will create them from the later Kubernetes Ingress and
Service. Use the output security-group IDs and WAF ARN in those manifests.

## Prerequisites

- Terraform 1.14.x
- AWS credentials with permission to create VPC, EKS, EC2, IAM, WAF, and CloudWatch
  resources in both regions
- An operator public IPv4 address expressed as a `/32`
- Sufficient quotas for two EKS clusters, two EIPs, NAT gateways, and later load
  balancers

This lab creates billable EKS clusters, NAT gateways, nodes, public IPv4 addresses,
and logs. Review the defaults and configure an AWS Budget before applying.

The one-worker layout is intentionally not highly available. AWS manages the EKS
control plane and does not expose a control-plane replica count. C1 is larger because
it runs most Online Boutique services; C2 only runs cartservice and Redis. Spot is not
the default because interruption of the sole worker would stop the demo cluster.

## Configure AWS login on Windows

Do not create access keys for the root user. Secure the root user with MFA. AWS CLI
2.32.0 and newer can exchange the existing AWS Console session for temporary local
credentials, so IAM Identity Center is not required for this standalone lab account.

1. [Install or update AWS CLI v2 for Windows](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
2. Confirm `aws --version` reports 2.32.0 or newer.
3. Configure and test a temporary console-login profile:

```powershell
aws login --profile observability-lab
aws sts get-caller-identity --profile observability-lab
```

The command opens a browser and uses the active AWS Console session. The CLI refreshes
the temporary credentials for up to 12 hours. Set
`aws_profile = "observability-lab"` in the local `terraform.tfvars` file.

For regular team or multi-account use, configure IAM Identity Center instead. It
provides centrally assigned users and permission sets and its access portal supplies
the SSO start URL requested by `aws configure sso`. It is optional for this lab.

## Check quotas

Quotas already exist on an AWS account; they are limits, not resources that Terraform
creates. This design creates one cluster in each region, so the normal EKS quota is
already sufficient. Before applying, open the AWS Service Quotas console and check
both `us-east-1` and `us-west-2` for:

- Amazon EKS: at least one cluster.
- Amazon EC2: at least two running On-Demand Standard vCPUs (each selected worker has
  two vCPUs).
- Amazon VPC: at least one VPC, internet gateway, Elastic IP, and NAT gateway.
- Elastic Load Balancing: at least one ALB in C1 and one NLB in C2.

If an **applied** value is below the requirement, select the quota and choose
**Request quota increase**. New accounts can have applied values below the published
defaults. Requests are regional, so make the request in the affected region only.

After the CLI profile works, list the applied EKS quotas from PowerShell with:

```powershell
foreach ($region in @("us-east-1", "us-west-2")) {
  aws service-quotas list-service-quotas `
    --service-code eks `
    --region $region `
    --profile observability-lab `
    --query "Quotas[?QuotaName=='Clusters'].{Region:'$region',Applied:Value,Code:QuotaCode}" `
    --output table
}
```

## Deploy

```powershell
Set-Location terraform
Copy-Item terraform.tfvars.example terraform.tfvars
# Edit operator_cidr, aws_profile, tags, and capacity in terraform.tfvars.

terraform init
terraform fmt -check
terraform validate
terraform plan -out lab.tfplan
terraform apply lab.tfplan
```

The example uses local state so it can start without bootstrapping an S3 backend.
For shared or longer-lived use, migrate state to an encrypted S3 backend with state
locking before the first team apply. Never commit state or a populated `.tfvars`.

After apply, run the two commands in the `kubeconfig_commands` output. The API is
reachable privately from each VPC and publicly only from `operator_cidr`.

## Kubernetes hand-off

Use `terraform output -json` to consume identifiers. In particular:

- Annotate the C1 Ingress with `security.c1_alb_security_group_id` and
  `security.waf_web_acl_arn`.
- Annotate the C2 internal NLB Service with
  `security.c2_cart_nlb_security_group_id` at its first creation.
- Annotate each `aws-load-balancer-controller` service account with its corresponding
  ARN from `iam_roles`.
- Scope the fault injector to `networking.c2.private_nacl_ids` and reserve rule 50.

The steady-state C2 NACL allows all traffic. The fault workflow must add an ingress
`DENY`, rule 50, for C1's CIDR on TCP/7070 and remove only that exact rule on restore.
Security groups and Kubernetes NetworkPolicies remain the steady-state controls.

## Teardown

Delete Kubernetes Ingress and `LoadBalancer` Services first and wait for their ALB
and NLB to disappear. Then run:

```powershell
terraform destroy
```

Confirm EKS clusters, NAT gateways, EIPs, load balancers, WAF, and log groups are
gone. Terraform-managed log groups are deleted during destroy, so export incident
evidence first.
