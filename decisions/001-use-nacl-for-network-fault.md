# ADR-001: Use a NACL for the network fault

- **Status:** Accepted
- **Date:** 2026-08-02

## Context

Fault 1 must interrupt the established C1-to-C2 cart-service connection during a live
demonstration and produce a clear network-configuration root cause.

The original plan removed a C2 Network Load Balancer security-group ingress rule.
Security groups are stateful and use connection tracking. Changing a rule does not
reliably interrupt an already tracked connection. Online Boutique uses long-lived
gRPC connections, so the planned security-group change could leave the application
working and fail to trigger the expected alerts.

## Decision

Fault 1 will add a high-precedence, stateless Network ACL `DENY` rule for the C1 VPC
CIDR on TCP/7070 to every NACL associated with the C2 internal NLB subnets.

The injector will:

- Reserve and validate a known rule number before changing anything.
- Apply the deny to every NLB subnet NACL so the failure is not availability-zone
  dependent.
- Print the UTC timestamp and affected resource IDs.
- Verify that the intended request fails while C2 pods and NLB targets remain healthy.

The restore operation will remove only the exact injected rule and verify recovery.

## Rationale

Network ACLs are stateless. A matching deny applies to existing and new traffic,
making the fault deterministic enough for a live demonstration. It retains the
desired incident signature: healthy C2 workloads, failing upstream C1 services,
rejected network traffic, and a configuration change outside the application.

## Consequences

- VPC Flow Logs can corroborate the fault with `REJECT` records.
- CloudTrail Event History can identify the API action, actor, and timestamp.
- The injection script must handle multiple subnet NACLs safely and idempotently.
- Security groups remain part of the permanent least-privilege design; they are simply
  not used as the live fault mechanism.

