# Installing a Connector: preflight, enrollment, rotation, Helm (roadmap A8)

Status: **approved by the owner (2026-10-09), every decision of §4 as recommended.** Slices land one PR each. Builds on the connector boundary contract §12 (gRPC over mTLS,
the Connector dials out) and replaces its §12.4 "static certificates, first version".

## 1. Where it stands

- Transport, identity by URI SAN `connector:<id>`, one session per id, reconnect without a `Gap`: done (A7).
- Certificates: a CA, a server certificate and one leaf per connector, made by hand with `packages/connector/pki.py`,
  90 days, **no rotation, no revocation**; the allow-list is an environment variable (`SRE_CONNECTOR_ALLOWED`).
- `preflight` (wire op) answers *configured*, never *reachable* (`"reachable": null`).
- Installation exists only for the lab: a hand-written manifest (`infra/kubernetes/connector.yaml`) and Makefile steps
  that copy certificates into a Secret. A customer cannot install the Connector without us.

## 2. Goal

A customer installs the Connector into their cluster with one Helm command and a one-time token, sees before and after
the install whether every backend is reachable and every permission is right, and never handles a certificate. The
control plane can see, rotate and disable each Connector. Nothing weakens §12: the Connector still dials out, the
customer opens no port, private keys never leave the host that owns them.

## 3. Slices (each its own PR, in order)

### A8.1 `connectorctl preflight`: real probing

A command in the Connector image (`python -m packages.connector.ctl preflight`), runnable before the install (from a
workstation with a kubeconfig) and inside the running pod; the wire op `preflight` returns the same result, so the
console's Connections screen shows it. Each check has a bounded timeout and reports `ok`, `failed` with the reason, or
`not configured`; the command exits non-zero if any configured check fails, and prints a table or JSON.

| Check | How |
|---|---|
| Kubernetes reachable | `GET /version` |
| Read permissions | a `SelfSubjectAccessReview` for `get`, `list`, `watch` on every resource the Connector reads, per watched namespace (chaos kinds only in the evidence namespaces) |
| **No more than read** | the same review must *deny* `create`/`update`/`delete` on those resources and `get` on `secrets` in the watched namespaces; an allowed one is a failure ("broader than read-only") |
| Alertmanager | `GET /api/v2/status` with the configured token |
| Prometheus | `GET /-/ready`, then the instant query `up` |
| Loki | `GET /ready`, then a label-names query bounded to 5 minutes |
| Tempo | `GET /ready`, then a search bounded to 5 minutes |
| Control plane | a TLS handshake to the endpoint with the Connector's identity: the server certificate chains to the pinned CA, the name matches, the client certificate is accepted, and its expiry |

### A8.2 Connector registry and enrollment

- **Registry.** The allow-list moves from the environment into the control plane's database: one row per Connector id
  with its status (`pending`, `active`, `disabled`), certificate serial and expiry, last session. `SRE_CONNECTOR_ALLOWED`
  keeps working for the lab and is merged with the registry.
- **Token.** An operator creates a Connector (`agentic-sre connector create <id>`; the console's Connect Cluster flow,
  D4, uses the same API). It returns a **one-time enrollment token**: `<id>.<secret>.<sha256 of the CA>`, valid 1 hour,
  stored only as a hash, bound to that id, consumed on first use.
- **Enrollment.** On first start the Connector generates its private key locally, opens a TLS connection that
  authenticates the server only, checks the server's CA against the hash in the token (so no CA file has to be shipped
  to the customer), and calls `Enroll(token, CSR)`. The control plane checks the token (unused, unexpired, bound to
  the id), signs the CSR with SAN `connector:<id>` and 90 days, marks the token used and the row `active`, and
  returns the certificate and the CA. From then on the Connector connects over mTLS as today.

### A8.3 Rotation and revocation

- **Rotation.** When two thirds of its certificate's validity have passed, the Connector sends a new CSR over its
  existing authenticated session (`Renew`), installs the new certificate atomically and reconnects; the old one stays
  valid until it expires. A failed renewal is retried with backoff and shown in system status.
- **Revocation.** Disabling a Connector id in the registry refuses its next handshake and closes its live session. No
  CRL: the control plane is the only relying party and checks the registry on every handshake.
- **Expiry visibility.** System status warns 14 days before expiry and fails at expiry (as today).

### A8.4 Helm chart

`charts/agentic-sre-connector`: namespace, ServiceAccount, read-only Roles generated per watched namespace from the
values (workload kinds; chaos kinds only in the evidence namespaces; never Secrets of those namespaces), the Deployment
running the agent, the enrollment token from a value or an existing Secret, and the local webhook Service with its
bearer token. A values schema, `helm lint` and `helm template` checks in CI, and a `NOTES.txt` that tells the operator
to run `preflight`.

### A8.5 The lab adopts the chart

The lab installs its Connector with the chart and enrolls with a token instead of `make connector-deploy` copying
certificates; `infra/kubernetes/connector.yaml` is removed. This is a lab change, so a full `HOLDOUT` follows
(testbed-scenarios-design §23).

## 4. Decisions for the owner

1. **Enrollment channel.** Recommended: an `Enroll` method on the existing gRPC port, server-authenticated only (no
   client certificate yet), so the control plane still exposes one port. Alternative: a separate HTTPS endpoint.
2. **Trust bootstrap.** Recommended: pin the control plane's CA by its hash inside the token (as `kubeadm join` does),
   so nothing else is copied to the customer. Alternative: ship the CA file with the chart values.
3. **Where the Connector keeps its key and certificate in the cluster.** They must survive a pod restart, and the token
   is single-use.
   - Recommended: a Kubernetes Secret in the Connector's **own** namespace, with RBAC limited by `resourceNames` to that
     one Secret (`get`, `update`). The "never reads Secrets" rule stays for every watched namespace.
   - Alternative: a PersistentVolumeClaim (no Secret permission at all, but it needs a storage class and pins the pod).
4. **Registry in the database**, the environment allow-list kept for the lab (§A8.2). Recommended.
5. **Rotation at two thirds of validity over the live session; revocation by disabling the id, no CRL** (§A8.3).
   Recommended.
6. **The lab dogfoods the chart** (§A8.5), with a full `HOLDOUT` after it. Recommended.

## 5. Validation

Each slice has unit tests. In addition:
- **A8.1:** `preflight` run against the lab, then against a deliberately broken one (a wrong token, a missing Role, an
  extra `create` permission), each failure reported by name.
- **A8.2–A8.3:** enrollment over gRPC on loopback (token reuse, expiry, a wrong id and a CA-hash mismatch all
  refused); a rotation forced with a 10-minute certificate in the lab, the session staying up; a revocation closing the
  session.
- **A8.4:** `helm lint`, `helm template`, a values-schema test, and the RBAC rendered compared with the read-only rule.

Not part of A8: several connectors per control plane in one console view, a remote multi-tenant control plane (A9),
and the console's Connect Cluster screens (D4, which builds on A8.2's API).
