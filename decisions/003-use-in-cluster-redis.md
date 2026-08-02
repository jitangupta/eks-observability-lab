# ADR-003: Use an in-cluster Redis pod for cart storage

- **Status:** Accepted
- **Date:** 2026-08-02

## Context

Online Boutique `cartservice` requires a Redis-compatible datastore. The datastore is
not the subject of the assignment, the environment exists for only three days, and
introducing ElastiCache would add provisioning time, networking, cost, and another
managed-service dependency.

The upstream Online Boutique chart already supports an in-cluster Redis deployment
and configures `cartservice` to use `redis-cart:6379`.

## Decision

Run one in-cluster Redis pod beside `cartservice` in C2.

- Use the upstream Redis deployment as the baseline rather than replacing it with a
  new Valkey configuration during the exercise.
- Pin the selected Redis image to an explicit version or digest; do not deploy the
  mutable `redis:alpine` tag unchanged.
- Retain the upstream bounded resources initially: 70m CPU and 200Mi memory requested,
  with 125m CPU and 256Mi memory limits.
- Use `emptyDir` storage. Cart data is intentionally ephemeral for this lab.
- Expose Redis only through a `ClusterIP` Service on TCP/6379.
- Permit access only from `cartservice` through a C2 NetworkPolicy.
- Do not expose Redis through the internal NLB or any public endpoint.

## Rationale

This is the smallest implementation that preserves the real cart dependency and keeps
Redis colocated with its only client. It avoids cross-region Redis traffic and makes
the C1-to-C2 boundary a single gRPC application dependency.

Redis and Valkey both implement the required protocol, but changing the upstream
datastore image provides no interview value and creates compatibility risk. A Valkey
migration can be described as a production option rather than performed in the lab.

## Consequences

- Restarting the Redis pod clears existing carts. This is accepted for a disposable
  demonstration environment.
- The Redis deployment is not highly available and is not suitable for production.
- A production design would evaluate ElastiCache/MemoryDB or a highly available
  operator-managed Redis/Valkey deployment with authentication, TLS, backups, and
  persistence.
- Redis health must be included in the healthy baseline so it is not confused with
  the cross-region network fault.

