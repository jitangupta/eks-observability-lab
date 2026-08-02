# Online Boutique upstream provenance

- Repository: <https://github.com/GoogleCloudPlatform/microservices-demo>
- Requested ref: `refs/tags/v0`
- Annotated tag object: `025e96934e13b95935d63de02f274fb50cf5c689`
- Resolved commit: `5f4ccc7d1c4312c72e97cba777c4f6a586026e59`
- Upstream release represented by the ref: `v0.10.4`
- Helm chart version: `0.10.4`
- Helm chart app version: `v0.10.4`
- Vendored on: `2026-08-02`

The upstream `helm-chart/` directory was copied without modification to
`kubernetes/charts/online-boutique/`. Regional overrides and AWS-specific resources
are intentionally kept outside that directory so upstream provenance remains clear.

The upstream Apache License 2.0 text is copied to
`kubernetes/online-boutique/LICENSE`.

Application images use the versioned `v0.10.4` release tag. The upstream chart at
this commit already pins the Redis and BusyBox helper images by digest.

