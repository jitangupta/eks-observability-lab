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

## 2026-08-02 - Phase 5: Prepare C1 deployment

### Assistance

- Added a rerunnable C1 installer that consumes current Terraform outputs and the
  live C2 `cartservice-internal` Service status.
- Added a C1 `cartservice` ExternalName Service template, preserving the stock
  `cartservice:7070` address used by frontend and checkout.
- Added an internet-facing ALB Ingress template with explicit public subnets, the
  Terraform-managed ALB security group, IP targets, frontend health checks, and the
  Terraform-managed WAF WebACL.
- Documented the current HTTP-only demo trade-off and the Phase 5 user-journey exit
  checks.

### Gate status

- Phase 5 deployment has not started because the Phase 4 NLB is still pending and
  its targets cannot become healthy while AWS blocks load-balancer creation.
- The installer enforces the locked phase order before making any C1 change: it
  requires a live AWS ELB hostname from the C2 Service and rejects the temporary
  NodePort/node-IP workaround as an alias target.

### Verification and corrections

- Verified both direct-resource templates by loading them through a temporary Helm
  chart; Helm accepted the Kubernetes YAML structure.
- Parsed `install.ps1` with the PowerShell parser and exercised mocked blocked and
  successful paths. The blocked path made no apply/upgrade call without a C2 NLB
  hostname; the successful path resolved every template placeholder and applied the
  alias before the chart and Ingress.
- Linted and rendered the C1 chart. It contains the ten intended C1 Deployments and
  does not render `cartservice`, `redis-cart`, or `frontend-external`.
- Cross-checked the ALB annotations against the AWS Load Balancer Controller
  documentation, including IP targets, explicit frontend security groups, disabled
  controller backend-rule management, and WAFv2 association.
- Live C1 application, ALB, WAF association, and user-journey verification remain
  blocked on the Phase 4 exit gate.

### Time-boxed continuation

- Rechecked the live C2 Service after the initial failure. The controller continued
  retrying `CreateLoadBalancer` and AWS continued returning the same account-level
  `OperationNotPermitted`, including request ID
  `8976e7ab-8037-4bd3-b68c-db7ff34cf548` less than two minutes before the check.
- Added a separate, explicitly acknowledged fallback installer rather than weakening
  the target NLB-gated installer. It maps the stock C1 `cartservice:7070` Service to
  the current C2 node's approved temporary TCP/30770 NodePort through a manually
  managed EndpointSlice.
- The fallback is private and reversible but is not load balanced, depends on one
  node address, bypasses NLB target health, and cannot satisfy the Phase 4 or Phase 5
  exit gates. The public user journey must use local port-forwarding while the same
  ELBv2 account restriction prevents ALB creation.
- The operator confirmed that the AWS account was approximately four hours old when
  the failure occurred. AWS's account documentation says activation can sometimes
  take up to 24 hours, and AWS expert-reviewed re:Post guidance for this exact ELB
  error recommends waiting 24–48 hours for new accounts. Account age is now the
  leading explanation, but it remains an inference until ELBv2 creation succeeds or
  AWS Support confirms the restriction; the 24–48-hour window is not recorded as a
  guaranteed service-activation SLA.

### Fallback deployment and verification

- At `2026-08-02T11:04:36Z`, the fallback was active with the C1 Service on
  `cartservice:7070` forwarding through its EndpointSlice to the single Ready C2 node
  at `10.20.20.222:30770`.
- Installed C1 Helm release `online-boutique` revision 1. All ten intended C1 Pods
  became Ready with zero restarts, and no `frontend-external` Service was created.
- Confirmed the unmodified `CART_SERVICE_ADDR` value is `cartservice:7070` in both
  frontend and checkout Deployments.
- C2 cartservice logs showed live cross-region `GetCart`, `AddItem`, `EmptyCart`, and
  health calls generated after the C1 deployment.
- Ran an independent session through a temporary local frontend port-forward. Home,
  product detail, add-to-cart, cart view, and checkout all returned HTTP 200, and the
  checkout confirmation was present.
- Corrected the first local verification command after it used `$home` as a response
  variable. PowerShell variable names are case-insensitive, so this attempted to
  overwrite the protected `$HOME` variable. The corrected command used
  `$homeResponse`; this error affected only the local verifier and did not change the
  cluster.

## 2026-08-02 - Phase 6: Apply and prove application NetworkPolicies

### Assistance

- Added permanent default-deny, DNS, same-namespace application, ALB-to-frontend,
  cross-region cart, cart-to-Redis, and Redis-from-cart policies.
- Added a separately labeled fallback policy that allows only frontend and checkout
  to use the temporary selectorless C1 cart Service or translated C2 TCP/30770
  endpoint. The permanent TCP/7070 NLB rule remains present for recovery.
- Added a rerunnable installer that verifies the VPC CNI `PolicyEndpoint` CRD and
  `aws-eks-nodeagent` container before applying policies.
- Added a verifier that uses short-lived Deployments for negative tests. AWS VPC CNI
  does not enforce NetworkPolicy on standalone Pods without owner references, so a
  `kubectl run` probe could have produced a false pass.
- Accounted for TCP NLB IP-target source translation: client-IP preservation is
  disabled by default, so C2 permits the NLB private-subnet range while the C1 egress
  policy and NLB security group enforce the authorized caller boundary.

### Verification and corrections

- Server-side dry-run accepted every policy in its target cluster and confirmed the
  VPC CNI agent and `PolicyEndpoint` CRD in both clusters.
- Corrected the probe Deployment after live API dry-run found that an unquoted shell
  command containing `:` had been parsed as a YAML mapping instead of a string.
- Corrected the verifier after `kubectl rollout status deployment --all` proved
  unsupported by the installed client. The replacement uses
  `kubectl wait deployment --all --for=condition=Available`.
- Corrected the cart log assertion after PowerShell applied `-notmatch` separately to
  each array element; joining the log output before matching fixed the false failure.
  Live load-generator, frontend, checkout, and C2 cart logs disproved the temporary
  assumption that authorized traffic had stopped.
- Corrected expected native-command failure handling because Windows PowerShell
  converted redirected `kubectl exec` stderr into a terminating error under
  `ErrorActionPreference=Stop`.
- The final verifier passed: an unauthorized C1 Deployment could not reach
  `cartservice:7070`, an unauthorized C2 Deployment could not reach
  `redis-cart:6379`, all application Deployments were Available, authorized cart
  calls reached C2, and Redis returned `PONG`.
- Repeated the complete local-port-forward user journey after enforcement. Home,
  product detail, add-to-cart, cart view, and checkout returned HTTP 200, and the
  checkout confirmation was present.
- Confirmed both worker nodes have no external IP, all ordinary application and Redis
  Services are `ClusterIP`, no ALB/NLB exists in either lab Region, and the temporary
  C2 TCP/30770 rule accepts only `10.10.0.0/16`. The pending NLB Service remains
  externally blocked and the fallback remains explicitly outside the formal Phase 4
  and Phase 5 gates.

## 2026-08-02 - Phase 8: Build scoped verification tooling

### Assistance

- Added a Python/boto3 verifier that derives its resource scope from Terraform
  outputs instead of scanning unrelated VPCs or clusters in the AWS account.
- Added explicit allowlists for the expected public C1 ALB and internal C2 NLB,
  including subnet, security-group, target-health, WAF, and Kubernetes ownership
  evidence.
- Added configuration checks for worker public addresses, security groups, private
  route tables, VPC peering, C2 private-subnet NACLs, EKS endpoint access, and the
  VPC CNI add-on configuration.
- Added runtime checks for the `aws-eks-nodeagent`, reconciled `PolicyEndpoint`
  objects, and Kubernetes Service/Ingress exposure.
- Added short-lived, owner-backed authorized and unauthorized C1 probe Deployments.
  The verifier expects the authorized probe to succeed in healthy/restored states,
  fail during Fault 1, and expects the unauthorized probe to fail in every state.
- Added UTC-stamped human and JSON reports under the ignored
  `evidence/generated/phase8/` directory and non-zero exit behavior for failures.

### Verification and corrections

- Python compilation and five deterministic unit tests passed. Kubernetes accepted
  both probe Deployments through server-side dry-run in C1.
- The first read-only live preflight exposed a local dependency assumption: this
  workstation's `aws login` credential profile requires AWS CRT in addition to
  base boto3. Added the documented `botocore[crt]` dependency.
- The same preflight found that the installed AWS CLI directory was absent from the
  elevated process `PATH`, preventing kubeconfig exec authentication. Located the
  existing per-user AWS CLI and documented that `aws` must be on `PATH`; no
  kubeconfig or cluster resource was changed.
- After those local corrections, the configuration-only live preflight passed all
  eleven executed checks: AWS identity, exact ALB/NLB inventory, healthy targets,
  two-rule WAF association, private workers, security-group boundaries, active
  peering and private routes, clean baseline NACLs, restricted EKS endpoints and
  active CNI configuration, Ready CNI agents with reconciled PolicyEndpoints, and
  intended Kubernetes exposure.
- The read-only report remained deliberately `INCOMPLETE` because active probe
  creation was skipped. The operator then ran the full healthy verifier at
  `2026-08-02T16:01:27Z`. All thirteen reported checks passed with zero failures or
  skips: the authorized probe connected, the unauthorized probe was denied, and
  cleanup returned exit code zero. The UTC-stamped text and JSON reports were saved
  under `evidence/generated/phase8/`, satisfying the Phase 8 exit gate.
