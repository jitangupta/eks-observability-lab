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
