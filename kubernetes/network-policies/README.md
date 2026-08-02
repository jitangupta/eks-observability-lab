# Phase 6: application NetworkPolicies

Phase 6 adds VPC CNI-enforced default-deny policies to both Online Boutique
namespaces, then opens only the required DNS, same-cluster application, cross-region
cart, frontend ingress, and Redis paths.

## Policy model

### C1

- All Pods are denied ingress and egress by default.
- All Pods may query CoreDNS over UDP/TCP 53.
- Online Boutique Pods may communicate with other Pods in the same namespace.
- Only Pods labeled `app=frontend` or `app=checkoutservice` may initiate TCP/7070
  connections to the C2 private NLB subnet range.
- The frontend accepts TCP/8080 from the C1 public-subnet range used by ALB nodes.

### C2

- All Pods are denied ingress and egress by default.
- All Pods may query CoreDNS over UDP/TCP 53.
- `cartservice` may connect to `redis-cart` on TCP/6379.
- `redis-cart` accepts TCP/6379 only from `cartservice`.
- `cartservice` accepts TCP/7070 from the C2 private-subnet range. TCP NLB IP
  targets have client-IP preservation disabled by default, so the target sees an NLB
  private address rather than the original C1 Pod address. The C1 egress policy and
  NLB security group enforce the caller restriction.

## VPC CNI verification constraint

Amazon VPC CNI does not enforce NetworkPolicy on standalone Pods that lack an owner
reference. `verify.ps1` therefore creates short-lived Deployments for negative tests;
using `kubectl run` would create a false result on this CNI implementation.

References:

- [Amazon EKS VPC CNI NetworkPolicy considerations](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html)
- [AWS NLB target-group client IP behavior](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/edit-target-group-attributes.html)

## Apply

For the target NLB architecture:

```powershell
& .\kubernetes\network-policies\install.ps1
& .\kubernetes\network-policies\verify.ps1
```

While the documented NodePort fallback is active:

```powershell
& .\kubernetes\network-policies\install.ps1 -UseNodePortFallback
& .\kubernetes\network-policies\verify.ps1 -UseNodePortFallback
```

The fallback overlay allows frontend and checkout to use either the selectorless C1
Service address on TCP/7070 or the translated C2 node endpoint on TCP/30770. It does
not authorize any additional caller.

## Exit gate

The verifier requires:

- At least five policies and one VPC CNI `PolicyEndpoint` in each namespace.
- An unauthorized C1 Deployment to fail connecting to `cartservice:7070`.
- An unauthorized C2 Deployment to fail connecting to `redis-cart:6379`.
- All application Deployments to remain rolled out.
- Recent authorized application calls in C2 cartservice logs.
- Redis to return `PONG`.

Afterward, repeat the full frontend product, cart, and checkout journey. Under the
fallback, use the local port-forward documented in `kubernetes/c1/README.md`. Once
ELBv2 activates, rerun the installer without `-UseNodePortFallback`; it removes the
temporary policy before applying the permanent set.
