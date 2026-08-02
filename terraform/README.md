# Terraform

Terraform implementation is intentionally deferred to its dedicated session. That
session must use `../architecture/architecture.md`, `../decisions/`, and
`../IMPLEMENTATION_PLAN.md` as its contract.

The preferred basic layout is:

```text
terraform/
|-- versions.tf
|-- providers.tf
|-- variables.tf
|-- main.tf
|-- networking.tf
|-- eks.tf
|-- security.tf
|-- observability.tf
|-- outputs.tf
|-- terraform.tfvars.example
`-- README.md
```

Keep this as one readable root configuration unless provider aliasing or dependency
ordering makes a small module unavoidable. Use aliased AWS providers for
`us-east-1` and `us-west-2` and pin all provider and external module versions.

The implementation must output every identifier required by Kubernetes, verification,
and fault-injection sessions. Secrets, kubeconfig files, Terraform state, Grafana
credentials, and personal IP values must not be committed.
