# Secure API-authenticated demo overlay

The base kind manifests remain unauthenticated so the local demo works without
secret setup. This overlay configures the same operator-provided bearer token
for both the control plane and Alertmanager, whose Secret must be namespaced
separately because the workloads run in `sre-demo` and `observability`.

1. Replace `REPLACE_BEFORE_APPLY` in `api-secrets.yaml` in both Secret objects
   with the same value, without committing the edit.
2. Apply the overlay after the normal base resources:

```bash
kubectl apply -k infra/kubernetes/secure-api-auth
```

Alertmanager 0.28.1 reads the bearer credential through its supported
`http_config.authorization.credentials_file` setting. The checked-in base
configuration remains open; this overlay is the explicit secured deployment
path.
