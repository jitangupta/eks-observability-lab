# ADR-002: Use Grafana Cloud for the observability backend

- **Status:** Accepted
- **Date:** 2026-08-02

## Context

The lab must correlate logs and metrics from two EKS clusters, show real alerts, and
remain available for a three-day interview window. The implementation window is only
12 hours.

A self-hosted Loki, Prometheus, Grafana, and Alertmanager deployment would require
additional compute, storage, cross-cluster access, upgrades, credentials, and failure
handling. Operating that platform is not the primary evaluation objective.

## Decision

Use the Grafana Cloud free tier as the shared observability backend:

- Fluent Bit in both clusters sends selected logs to Grafana Cloud Loki.
- Prometheus in both clusters remote-writes selected metrics to Grafana Cloud Metrics.
- Grafana Cloud provides dashboards, alert evaluation, and email contact points.
- AWS-native security and audit evidence remains in CloudWatch and CloudTrail.

No S3 bucket or self-hosted Loki backend will be created for application logs.

## Rationale

The managed backend provides a single cross-region view without adding a new private
telemetry service between C1 and C2. It removes storage and SMTP administration and
preserves the limited implementation time for fault injection, investigation,
evidence capture, and RCAs.

## Consequences

- The deployment depends on outbound HTTPS access and Grafana Cloud availability.
- Log volume and Prometheus active-series cardinality must remain within free-tier
  limits.
- Credentials must be supplied through Kubernetes Secrets and never committed.
- Fluent Bit filters noisy logs before transmission.
- Prometheus metric relabeling drops unused, high-cardinality series.
- This is a deliberate interview-lab decision; a production platform would reassess
  retention, data residency, support, and service ownership requirements.

