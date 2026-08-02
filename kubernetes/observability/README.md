# Phase 7 observability

This directory installs the minimum telemetry path required by the incident lab:

- Fluent Bit `0.57.9` in both clusters, sending selected Pod logs to Grafana Cloud
  Loki over TLS.
- Prometheus chart `29.20.1` in both clusters, remote-writing an allowlist of
  incident metrics to Grafana Cloud Metrics.
- A C1 blackbox exporter that probes the cross-region cart endpoint on TCP/7070
  and the in-cluster frontend over HTTP.
- A narrowly scoped NetworkPolicy that lets only the C1 blackbox Pod reach the
  frontend health target.

## Grafana Cloud values for this stack

The non-secret values are intentionally committed so the installation is repeatable:

| Setting | Value |
| --- | --- |
| Stack | `epicspider2262` |
| Grafana URL | `https://epicspider2262.grafana.net` |
| Prometheus instance ID | `3417368` |
| Prometheus remote-write URL | `https://prometheus-prod-43-prod-ap-south-1.grafana.net/api/prom/push` |
| Loki instance ID | `1704359` |
| Loki push URL | `https://logs-prod-028.grafana.net/loki/api/v1/push` |
| Hosted region | `aws / ap-south-1` |

The only secret needed for collector installation is one stack-scoped Grafana Cloud
access-policy token with `metrics:write` and `logs:write`. Give it a short expiry.
Never put the token in a values file, shell history, Terraform state, or Git.

## Install

The PowerShell installer obtains cluster names from Terraform, prompts for the token
with hidden input, creates the same opaque Kubernetes Secret in each cluster, and
installs the pinned charts:

```powershell
& .\kubernetes\observability\install.ps1
& .\kubernetes\observability\verify.ps1
```

For non-interactive use, set `GRAFANA_CLOUD_TOKEN` only for the current process and
remove it immediately afterward. The interactive prompt is preferred.

If Grafana reports `401 Unauthorized` or `invalid token`, create a new token under
the same access policy and rerun the installer. Enter the token value shown once by
Grafana (normally beginning with `glc_`), without quotes or a `Bearer` prefix. The
installer restarts both collectors after updating the Secret so the replacement
credential is loaded immediately.

## What is sent

Fluent Bit keeps logs only from `online-boutique` and `observability`. Loki stream
labels are limited to `cluster`, `namespace`, `service`, `container`, and `level`.
Pod name is structured metadata, while request IDs and other application fields stay
inside the JSON log record.

Prometheus remote-write keeps only availability, blackbox timing, Kubernetes state,
restart/OOM, container CPU/memory, and basic node-memory metrics. Scraped data is
retained locally for two hours and is not stored on a persistent volume.

## Grafana Explore checks

After `verify.ps1` succeeds, wait about two minutes and run:

```promql
probe_success{cluster="c1"}
```

```promql
kube_pod_container_status_ready{namespace="online-boutique"}
```

```logql
{cluster=~"c1|c2", namespace="online-boutique"}
```

Both cluster labels must appear before dashboard and alert provisioning begins.

## Incident dashboard

Import `grafana/incident-dashboard.json` into the Grafana stack. The dashboard uses
the stack's hosted Prometheus and Loki data sources directly, defaults to UTC and a
one-hour evidence window, and covers dependency health/latency, workload readiness,
restarts/OOMs, CPU/memory, and application logs for both clusters.

The `Application errors detected` alert intentionally excludes the Online Boutique
load generator's expected rejection of `visa_electron` cards. Without this narrow
line filter, normal synthetic checkout traffic repeatedly creates false incidents:

```logql
sum by (cluster, service) (
  count_over_time(
    {cluster=~"c1|c2",namespace="online-boutique",level=~"error|fatal"}
      != "visa_electron credit cards"
    [5m]
  )
)
```

Do not broaden the exclusion to all payment or checkout errors; those remain useful
application-availability symptoms during a real incident.

## Remaining account-side requirements

Phase 7 still needs one Grafana service account with a short-lived token for
dashboard/alert API provisioning, plus the destination email address for the contact
point. The ingest access-policy token cannot manage dashboards or contact points and
must not be broadened for that purpose.
