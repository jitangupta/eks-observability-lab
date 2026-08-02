# AI Assistance Log

This log records AI-assisted work, the validation performed by the operator or
automation, and any corrections made before the result was accepted. AI output is
treated as a proposal until it passes the repository's documented checks.

The log begins during Phase 2 on 2026-08-02. If AI assistance was used before this
file was created, those earlier entries should be backfilled when the details are
available.

## 2026-08-02 - Phase 2: Pin and prepare Online Boutique

### Assistance

- Read `IMPLEMENTATION_PLAN.md` and mapped Phase 2 to the official Online Boutique
  Helm chart.
- Resolved the upstream `v0` ref to an exact commit and recorded its chart and
  application versions.
- Vendored only the upstream `helm-chart/` directory and copied the upstream
  Apache-2.0 license outside the chart.
- Created separate C1 and C2 values files for the planned regional workload split.
- Proposed Helm lint, render, workload-placement, service-exposure, and image-pin
  checks for the Phase 2 exit gate.

### Human authorization

- The operator confirmed that Phase 1 was complete and explicitly authorized adding
  this log and implementing the Phase 2 Helm configuration.

### Verification and corrections

- Verified that the upstream `v0` ref resolves to commit
  `5f4ccc7d1c4312c72e97cba777c4f6a586026e59`, chart version `0.10.4`, and app/image
  version `v0.10.4`.
- Corrected generic setting names against the pinned chart schema. The actual keys
  include `cartService`, `loadGenerator`, and
  `cartDatabase.inClusterRedis`; similarly named keys from other chart revisions were
  not used.
- Verified the final files with Helm and explicit manifest checks. The command
  results are summarized in the Phase 2 handoff and can be reproduced from the
  repository.

## 2026-08-02 - Phase 3: Install cluster prerequisites

### Assistance

- Derived the AWS Load Balancer Controller configuration from the Terraform cluster,
  networking, and IRSA outputs.
- Provided the operator with commands to install the pinned controller release in
  both EKS clusters and label the application namespaces.
- Added a rerunnable PowerShell installer and installation documentation. The script
  reads current Terraform outputs rather than retaining resource IDs from one apply.

### Human authorization and execution

- The operator requested commands and executed the cluster changes directly.
- The operator then authorized repository documentation so the controller can be
  recreated after the lab infrastructure is decommissioned.

### Verification and corrections

- The operator reported both managed VPC CNI add-ons as `ACTIVE`, version
  `v1.22.4-eksbuild.3`, with `enableNetworkPolicy` set to `true`.
- Corrected the expected VPC CNI NetworkPolicy container name. In this installed
  version the node agent is `aws-eks-nodeagent`, not the older
  `aws-network-policy-agent` name initially suggested. The current agent attaches the
  eBPF programs that enforce NetworkPolicy on each node.
- Verification commands are intentionally not stored as Kubernetes installation
  resources; Phase 3 repository content is limited to installation and recreation.

## 2026-08-02 - Phase 4: Prepare C2 first

### Assistance

- Created a rerunnable C2 installer for the pinned Online Boutique chart using the
  Phase 2 `values-c2.yaml` workload split.
- Added an internal NLB Service template with IP targets, explicit private subnets,
  the Terraform-created cart NLB security group, TCP health checks, and cross-zone
  load balancing.
- Kept controller management of backend security-group rules disabled because the
  required NLB-to-target rule is owned by Terraform.

### Verification and corrections

- Identified a conflict between the original Phase 4 test location and the security
  design. The NLB security group permits TCP/7070 only from the C1 VPC, so a test pod
  inside C2 cannot legitimately reach the NLB.
- Corrected the plan to originate the controlled NLB test from C1. This preserves the
  intended security boundary without adding an unnecessary C2 ingress rule.
- The operator deployed `cartservice` and `redis-cart` successfully in C2. Both
  Deployments rolled out, Redis returned `PONG`, and `cartservice` was configured to
  use `redis-cart:6379`.
- The AWS Load Balancer Controller accepted the Service, resolved the cart endpoint,
  and built the intended internal NLB model. AWS then rejected the ELBv2
  `CreateLoadBalancer` request with `OperationNotPermitted: This AWS account
  currently does not support creating load balancers`.
- Confirmed through `GetAccountPlanState` that the AWS account is `ACTIVE` and
  `PAID`. The failure is therefore an AWS-side account restriction rather than an
  inactive Free plan, Kubernetes configuration error, IRSA failure, subnet problem,
  security-group problem, or load-balancer quota error.
- The required internal NLB remains `<pending>`, so its DNS name, target health, and
  the C1-to-C2 TCP/7070 test cannot yet be verified. Phase 4 remains incomplete and
  externally blocked until AWS Support enables load-balancer creation.

### Temporary workaround note

- The operator created a separate C2 `cartservice-nodeport` Service on TCP/30770 and
  allowed that single port on the C2 node security group only from the C1 VPC CIDR,
  `10.10.0.0/16`. AWS created temporary security-group rule
  `sgr-061382e780b672c54`.
- The current C2 node private IP was `10.20.20.222`. A controlled BusyBox pod in C1
  successfully connected to `10.20.20.222:30770`. This verifies the inter-Region VPC
  peering route, baseline NACL path, node security-group rule, Kubernetes NodePort,
  and cartservice endpoint independently of ELB.
- This workaround is deliberately not part of the committed target architecture. It
  depends on the C2 node's private IP, bypasses NLB health checks and availability,
  does not satisfy the Phase 4 exit gate, and must be removed after ELB access is
  restored.
- Manual cleanup, if performed before Terraform destroys the node security group,
  consists of deleting the `cartservice-nodeport` Service and revoking TCP/30770 from
  `10.10.0.0/16` on the C2 node security group. The original
  `cartservice-internal` Service remains pending for automatic controller retries.
- The permanent recovery is an AWS Account and billing support case containing the
  ELBv2 `OperationNotPermitted` message and request IDs from the controller events.
