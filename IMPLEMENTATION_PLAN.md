# Implementation Plan

This is the locked execution order for EKS Observability Lab. Each phase has an exit
gate. Do not build later features on top of a phase that has not passed its gate.

The plan uses the official Google Online Boutique Helm chart rather than creating a
new application chart. The upstream chart provides per-component `create` flags and
an in-cluster Redis option, which are the seams needed for the regional split.

## Phase 0: Freeze inputs and prerequisites

Before applying infrastructure:

1. Confirm the AWS account, CLI profile, and caller identity.
2. Confirm sufficient VPC, EIP, EKS, and load-balancer quotas in both regions.
3. Record the operator's current public `/32` CIDR for restricted EKS API access.
4. Create or confirm a Grafana Cloud free account and its Loki and Prometheus
   endpoints. Keep credentials outside Git.
5. Decide whether an existing Route 53 domain and ACM certificate are available.
   Otherwise record HTTP as a time-boxed demo trade-off.
6. Configure an AWS Budget alert for the lab.
7. Add `.gitignore` rules for Terraform state, plans, kubeconfigs, environment files,
   downloaded credentials, and generated evidence containing secrets.
8. Record all AI assistance and corrections in `AI-LOG.md` from this point onward.

Exit gate: AWS identity, regions, operator CIDR, Grafana endpoints, and TLS decision
are known and no secret will be committed.

## Phase 1: Build the AWS foundation with Terraform

Implement Terraform before Kubernetes resources because every later phase depends on
cluster, subnet, CIDR, IAM, and security outputs.

1. Pin Terraform, AWS provider, Kubernetes provider, Helm provider, and external
   module versions.
2. Configure aliased AWS providers for C1 `us-east-1` and C2 `us-west-2`.
3. Create non-overlapping VPCs:
   - C1: `10.10.0.0/16`
   - C2: `10.20.0.0/16`
4. Create public and private subnets across at least two availability zones.
5. Create one NAT gateway per VPC as the documented cost/availability trade-off.
6. Create both EKS clusters and managed node groups on private subnets.
7. Configure the VPC CNI add-on with NetworkPolicy enforcement enabled.
8. Restrict the EKS API endpoints to private access or the operator's `/32` CIDR.
9. Create and accept inter-region VPC peering, then add only the required peer-CIDR
   routes to the relevant private route tables.
10. Create explicit C2 private-subnet NACLs with known allow-rule numbers and output
    every associated NACL ID for safe fault injection.
11. Create security groups for the C1 ALB and C2 internal cart NLB. The NLB security
    group must exist before Kubernetes creates the NLB.
12. Create IAM/OIDC or EKS Pod Identity permissions required by AWS Load Balancer
    Controller and telemetry components.
13. Create the WAF WebACL and its minimum managed/rate rules.
14. Create CloudWatch log groups with seven-day retention.
15. Enable one-minute, `REJECT`-only VPC Flow Logs before any fault testing.
16. Output cluster names, regions, VPC/subnet/CIDR IDs, NACL IDs, security-group IDs,
    WAF ARN, and role ARNs.
17. Run `terraform fmt`, `validate`, `plan`, and apply.

The two EKS clusters can provision concurrently inside the same apply. While AWS is
creating them, Phase 2 source preparation can begin.

Exit gate:

- Both kubeconfig contexts work.
- All expected nodes are `Ready` and have no public IPs.
- VPC peering is active with correct routes.
- Flow Logs are enabled.
- Terraform outputs contain every value needed by Kubernetes.

## Phase 2: Pin and prepare Online Boutique

Do not build the application images and do not pull them manually onto the workstation.
EKS nodes will pull the published images when pods start.

1. Clone the official repository using the stable `v0` release line.
2. Record the exact upstream commit and image/chart version used.
3. Vendor only the official `helm-chart/` directory into
   `kubernetes/charts/online-boutique/`, preserving its license and provenance.
4. Pin all application and Redis images to immutable versions or digests.
5. Create `values-c1.yaml`:
   - Enable all required services except `cartService` and `inClusterRedis`.
   - Disable the chart's external frontend `LoadBalancer` Service.
   - Set platform to AWS.
   - Keep the load generator modest.
6. Create `values-c2.yaml`:
   - Enable only `cartService` and `inClusterRedis`.
   - Disable every unrelated application service.
   - Retain the approved bounded Redis resources.
7. Keep AWS-specific ALB/NLB Services, Ingress, NetworkPolicies, and the C1 cart alias
   outside the vendored chart so the upstream source remains recognizable.

Mirroring the images into ECR is deferred. It adds copy time and IAM/ECR work without
improving the debugging demonstration. Revisit it only if upstream image pulling
actually fails.

Exit gate: `helm template` renders separate C1 and C2 manifests containing exactly
the intended workloads, no unintended public Service, and pinned images.

## Phase 3: Install AWS controllers and cluster prerequisites

1. Create an `online-boutique` namespace in both clusters.
2. Install AWS Load Balancer Controller with the Terraform-created IAM role.
3. Confirm VPC CNI network-policy agents are running and enforcement is enabled.
4. Install only other chart prerequisites that are proven necessary.
5. Label both clusters and namespaces consistently for telemetry.

Do not add a service mesh, cert-manager, ExternalDNS, or another controller unless a
locked requirement needs it.

Exit gate: the load-balancer controller and VPC CNI policy agents are healthy in both
clusters.

## Phase 4: Deploy C2 first

C2 must exist before C1 can be wired to its cart endpoint.

1. Install the C2 Online Boutique release using `values-c2.yaml`.
2. Confirm the Redis pod is healthy and `redis-cli ping` returns `PONG`.
3. Confirm `cartservice` resolves `redis-cart:6379` and passes its gRPC health probe.
4. Create a second, internal NLB Service selecting `cartservice` on TCP/7070.
5. Supply the Terraform-created NLB security group in the Service annotations at
   creation time.
6. Wait for the NLB, targets, and private DNS name to become healthy.
7. Test the NLB endpoint from a controlled pod inside C1. The NLB security group
   intentionally accepts TCP/7070 only from the C1 VPC CIDR, so a C2-origin test
   would conflict with the locked security boundary.

Exit gate: C2 cart and Redis are healthy, Redis is private, and the internal NLB has
healthy targets.

## Phase 5: Deploy C1 and connect the regions

1. Create the C1 `cartservice` alias pointing to the C2 internal NLB DNS name.
2. Install the C1 Online Boutique release using `values-c1.yaml`.
3. Confirm frontend and checkout resolve `cartservice:7070` without modifying their
   stock environment variables.
4. Create the C1 ALB Ingress using the Terraform-created WAF ARN.
5. Confirm there is no chart-generated public `frontend-external` Service.
6. Wait for all workloads, the ALB, and ALB targets to become healthy.
7. Test real user behavior:
   - Open the home page.
   - View a product.
   - Add an item to the cart.
   - Complete checkout.
8. Save this as the first healthy cross-region proof.

Exit gate: the user journey succeeds through the WAF/ALB and both frontend and
checkout successfully use C2 cart storage.

## Phase 6: Apply and prove security controls

Apply policies after basic connectivity works so failures have a smaller search area.

1. Apply default-deny policies in both application namespaces.
2. Allow required DNS and same-cluster application dependencies.
3. Permit C1-to-C2 cart access only from `frontend` and `checkoutservice`.
4. Permit Redis TCP/6379 only from `cartservice` in C2.
5. Confirm no worker node, pod, Redis Service, or C2 NLB is publicly accessible.
6. Run a positive request from an authorized caller.
7. Run the same request from an unauthorized C1 test pod and require failure.
8. Retest the full user journey after the policies are active.

Exit gate: positive and negative tests both behave as designed and the application
still works.

## Phase 7: Install observability and real alerts

1. Install Fluent Bit in both clusters.
2. Send selected logs to Grafana Cloud Loki over outbound TLS.
3. Use canonical Loki labels: `cluster`, `namespace`, `service`, `container`, and
   `level`; keep pod and request identifiers as fields.
4. Install Prometheus collection in both clusters and remote-write selected metrics
   to Grafana Cloud.
5. Drop unused/high-cardinality series to stay within the free tier.
6. Add a C1 blackbox TCP probe for the C2 cart NLB on port 7070.
7. Build one dashboard that correlates C1 symptoms, cross-region probe health, C2
   workload health, and Redis health.
8. Configure one-minute alerts for:
   - Cross-region cart probe failure.
   - Upstream application errors or availability degradation.
   - OOM termination and abnormal restart count.
9. Configure and test a real Grafana Cloud email contact point.
10. Connect or display required CloudWatch ALB, NLB, and WAF metrics without delaying
    the core application alerts.

Exit gate: logs and metrics from both clusters are searchable, the dashboard is
populated, and a deliberately triggered test alert reaches email.

## Phase 8: Build and run verification

1. Implement the Python/boto3 configuration checks documented in the root README.
2. Implement authorized and unauthorized in-cluster probes.
3. Run the verifier against the healthy environment.
4. Fix every unexpected public path or false assumption.
5. Save human-readable and JSON baseline results.

Exit gate: the healthy report passes with concrete resource and request evidence.

## Phase 9: Capture the healthy baseline

Before injecting faults, save UTC-stamped evidence for:

- Application and checkout success.
- C1-to-C2 cart request success and latency.
- Unauthorized request failure.
- C1 and C2 pod health.
- Redis health.
- ALB/NLB target health and WAF association.
- Quiet alert state and dashboard baseline.
- Verification output.

Exit gate: every incident graph will have an equivalent healthy comparison.

## Phase 10: Inject, investigate, and restore Fault 1

1. Run the NACL injection script and record its UTC timestamp.
2. Wait for real alerts and email notification.
3. Investigate without reading the injector output as the answer.
4. Classify upstream alerts as symptoms and healthy C2 workloads as exonerating
   evidence.
5. Inspect routes, security groups, NACLs, Flow Logs, and CloudTrail in investigation
   order; record dead ends.
6. Run the verifier during the fault and save its JSON output.
7. Restore only the injected NACL entries.
8. Confirm application, probe, alert, and verification recovery.

Exit gate: the fault and repair scripts are repeatable, and the evidence supports one
unambiguous network root cause.

## Phase 11: Inject, investigate, and restore Fault 2

1. Capture the current `productcatalogservice` memory configuration.
2. Apply the invalid low-memory configuration and record the UTC timestamp.
3. Capture OOMKilled, exit code 137, CrashLoopBackOff, restarts, logs, events, upstream
   effects, alerts, and email.
4. Prove that the C1-to-C2 cart path remains healthy.
5. Restore the exact previous resource configuration.
6. Confirm rollout and alert recovery.

Exit gate: the evidence distinguishes a direct workload-configuration cause from the
cascading network incident.

## Phase 12: Write, rehearse, and preserve

1. Complete one RCA per fault using the required headings.
2. Complete `WRITEUP.md`, including implemented and skipped items with reasons.
3. Complete `AI-LOG.md` with verified AI mistakes and corrections.
4. Create `DEMO.md` as an exact, timed sequence.
5. Rehearse Fault 1 injection, investigation, restoration, and executive explanation.
6. Keep offline copies of critical screenshots and JSON/text evidence.
7. Confirm the environment is healthy before leaving it for the interview.

Exit gate: another engineer can follow the demo runbook without improvising commands.

## Teardown after the interview

1. Export any final evidence.
2. Delete Kubernetes Ingress and `LoadBalancer` Services first.
3. Wait until AWS Load Balancer Controller removes the ALB/NLB resources.
4. Uninstall the remaining Helm releases.
5. Run Terraform destroy and verify that EKS clusters, NAT gateways, load balancers,
   WAF, Elastic IPs, and CloudWatch log groups are gone.
6. Revoke or delete Grafana Cloud access tokens.
7. Confirm the AWS billing dashboard has no unexpected lab resources.
