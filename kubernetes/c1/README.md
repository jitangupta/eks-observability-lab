# Phase 5: C1 application and cross-region cart alias

Phase 5 deploys the user-facing Online Boutique services in C1, connects the stock
`cartservice:7070` name to the private C2 Network Load Balancer, and creates the only
intended public application entry point: a WAF-associated Application Load Balancer.

## Phase 4 gate

Do not run the installer until the `cartservice-internal` Service in C2 has an AWS
NLB hostname and healthy targets. `install.ps1` checks for that hostname before it
creates any C1 resource. A temporary NodePort or a node IP does not satisfy this gate
and is deliberately not accepted as an alias target.

## Installed resources

- Helm release `online-boutique` in the C1 `online-boutique` namespace, using
  `values-c1.yaml`.
- An `ExternalName` Service named `cartservice` whose target is the C2 internal NLB
  hostname. The unmodified frontend and checkout Deployments continue using their
  stock `cartservice:7070` environment value.
- An `alb` Ingress named `frontend`, forwarding HTTP/80 to the private frontend
  Service with IP targets.

The chart's `frontend-external` LoadBalancer Service remains disabled. The installer
checks both the rendered chart and the live namespace for that unexpected Service.

## Dynamic Terraform and Kubernetes inputs

The installer reads both cluster context names, C1 public subnet IDs, the C1 ALB
security-group ID, and the WAF WebACL ARN from current Terraform outputs. It reads
the C2 NLB hostname from the live `cartservice-internal` Service. Generated resource
IDs and AWS DNS names are not committed.

The custom ALB security group is attached at Ingress creation. Controller management
of backend rules is disabled because Terraform already permits this security group
to reach the C1 nodes. The WAF WebACL is attached with the AWS Load Balancer
Controller's WAFv2 annotation.

## HTTP trade-off

This lab currently renders an HTTP listener because no ACM certificate or validated
domain is configured in Terraform. WAF still protects the HTTP listener, but this is
not equivalent to production TLS. Add a validated ACM certificate and HTTPS listener
before treating the endpoint as production-like.

## Prerequisites

1. Complete the Phase 4 exit gate, including healthy NLB targets and a successful
   controlled C1-to-C2 TCP/7070 test.
2. Ensure both kubeconfig context aliases match the Terraform cluster names.
3. Ensure AWS Load Balancer Controller is healthy in C1.
4. Make `terraform`, `kubectl`, and `helm` available on `PATH`.

## Install or recreate

From the repository root in Windows PowerShell:

```powershell
& .\kubernetes\c1\install.ps1
```

The Helm deployment is atomic. The installer applies the cart alias first, waits for
the Helm release, confirms `frontend-external` is absent, applies the Ingress, and
waits up to ten minutes for its ALB hostname.

## Exit-gate verification

Set the two contexts from Terraform outputs, then verify the stock cart address and
the alias:

```powershell
$clusters = terraform "-chdir=.\terraform" output -json clusters | ConvertFrom-Json
$c1 = $clusters.c1.name
$c2 = $clusters.c2.name

kubectl --context $c1 -n online-boutique get service cartservice -o wide
kubectl --context $c1 -n online-boutique get deployment frontend checkoutservice
kubectl --context $c1 -n online-boutique get deployment frontend -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='CART_SERVICE_ADDR')].value}{'`n'}"
kubectl --context $c1 -n online-boutique get deployment checkoutservice -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='CART_SERVICE_ADDR')].value}{'`n'}"
kubectl --context $c1 -n online-boutique get service frontend-external --ignore-not-found
kubectl --context $c1 -n online-boutique get ingress frontend
kubectl --context $c2 -n online-boutique get service cartservice-internal
```

Confirm the AWS Load Balancer Controller reports healthy ALB targets and that the
WAF WebACL from `terraform output -json security` is associated with the ALB. Then
open the installer-reported URL and complete the healthy proof:

1. Load the home page.
2. Open a product.
3. Add it to the cart.
4. Complete checkout.
5. Save UTC-stamped evidence of the successful journey and relevant C1/C2 workload
   state.

Before destroying Terraform, delete the `frontend` Ingress and wait for its ALB to
disappear. This prevents an orphaned load balancer or security-group dependency from
blocking destroy.

## Time-boxed NodePort fallback

If AWS rejects `CreateLoadBalancer` at the account level, deleting and recreating
the NLB Service cannot clear that restriction. The controller will submit the same
ELBv2 call again and receive the same rejection.

### New-account activation context

The operator confirmed that this AWS account was approximately four hours old when
ELBv2 began returning `OperationNotPermitted`. AWS documents that new-account
activation can sometimes take up to 24 hours, and AWS expert-reviewed re:Post
guidance for this exact load-balancer error recommends waiting 24–48 hours for a new
account before retrying:

- [Getting started with an AWS account](https://docs.aws.amazon.com/accounts/latest/reference/getting-started.html)
- [AWS re:Post: account does not support creating load balancers](https://repost.aws/questions/QUUtCU_U6aSjeqQ1JMFrnwBg/this-aws-account-currently-does-not-support-creating-load-balancers-for-more-information-please-contact-aws-support)

Account age is therefore the leading explanation for this lab's restriction, but
the 24–48-hour window is not a guaranteed ELB activation SLA. Keep the AWS Support
case open and retry the existing `cartservice-internal` Service after the account is
at least 24 hours old. Escalate the case if the restriction remains after 48 hours.

To continue application, policy, telemetry, and fault-development work while AWS
Support handles the restriction, this repository includes an explicitly opt-in
fallback:

```powershell
& .\kubernetes\c1\install-nodeport-fallback.ps1 -AcknowledgeTemporaryFallback
```

The fallback reads the single Ready C2 node's current private IP and verifies that
the separately created `cartservice-nodeport` still uses the approved TCP/30770. It
creates a selectorless C1 `cartservice` Service and a manually managed EndpointSlice,
so callers retain the stock `cartservice:7070` address while kube-proxy forwards to
the C2 node and NodePort.

This path is private and uses the already tested VPC-peering route and temporary
security-group rule, but it is not load balanced, survives neither node replacement
nor scaling, and bypasses NLB health checks. It does not satisfy the Phase 4 or Phase
5 exit gate.

Because the same AWS account restriction also prevents ALB creation, verify the C1
frontend locally without exposing a new public path:

```powershell
$clusters = terraform "-chdir=.\terraform" output -json clusters | ConvertFrom-Json
$c1 = $clusters.c1.name
kubectl --context $c1 -n online-boutique port-forward service/frontend 8080:80
```

Open `http://localhost:8080` and run the product, cart, and checkout journey. Keep
this evidence labeled as workaround validation rather than the healthy ALB/NLB
baseline.

When AWS enables ELBv2, remove the fallback before running the target installer:

```powershell
$clusters = terraform "-chdir=.\terraform" output -json clusters | ConvertFrom-Json
$c1 = $clusters.c1.name
kubectl --context $c1 -n online-boutique delete endpointslice cartservice-nodeport-fallback
kubectl --context $c1 -n online-boutique delete service cartservice
& .\kubernetes\c1\install.ps1
```

After the NLB-backed user journey passes, also delete the C2
`cartservice-nodeport` Service and revoke the temporary TCP/30770 C2 node
security-group rule described in `AI-LOG.md`.
