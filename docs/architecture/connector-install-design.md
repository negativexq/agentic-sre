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

## 6. A8.1 implemented (2026-10-09)

`packages/connector/preflight.py` (the checks) and `packages/connector/ctl.py` (`python -m packages.connector.ctl
preflight [--json] [--as USER]`). The wire op `preflight` runs the same checks in the deployed Connector, keeps its
`backends` shape with `reachable` now measured, and adds `checks`. `--as` asks the Kubernetes API through
impersonation, so an operator can check the Connector's ServiceAccount from a workstation.

Two things the tests found and the implementation handles:
- TLS 1.3 finishes the client's handshake before the server has judged the client certificate, so a handshake alone
  proves nothing; the check reads once after it.
- The gRPC server refuses a foreign client certificate by closing the connection **without an alert**. A close right
  after the handshake is therefore a refusal; a server that waits or sends its first HTTP/2 frame has accepted.

Verification: 15 unit tests, one of them against the real gRPC gateway (a trusted identity accepted, a foreign one
refused); the full suite passes. Against the lab, as `system:serviceaccount:connector:connector` with temporary
port-forwards: **12 of 12 checks ok** (every read in `sre-demo`, `lab-control`, `chaos-mesh`; no write and no Secret
access; the four backends; the mTLS handshake, certificate valid 81 more days). Broken on purpose (the `default`
ServiceAccount, an Alertmanager on a closed port): the missing reads and the unreachable backend named, exit 1.

Not covered yet: an identity off the control plane's allow-list passes the TLS check (the allow-list is enforced when
the session opens); A8.2's registry makes it checkable.

## 7. Amendment to decisions 1 and 2, found before building A8.2 (approved by the owner 2026-10-09, both as recommended)

Both were approved as recommended; neither can be built as written with the stack in use.

**Decision 1 (enroll on the existing gRPC port).** The port requires a client certificate. For a Connector that has
none yet, client authentication would have to become optional on that port. Measured with grpcio: with
`require_client_auth=False` the server does not *request* a client certificate at all, so a Connector presenting a
valid one arrives with no identity. One port cannot serve both enrollment and mutual TLS. Options:
- **(a) recommended:** a second port for enrollment only: server-authenticated TLS, one method (`Enroll`), nothing
  else served there. The customer still opens no inbound port (the Connector dials both); the control plane exposes two.
- (b) one port without TLS-level client authentication, Connector identity checked by the application: weakens §12's
  mutual TLS. Not recommended.

**Decision 2 (pin the CA by its hash in the token).** Pinning by hash needs the Connector to see the control plane's CA
before it trusts it. A gRPC channel cannot be opened without a trusted root, and Python 3.12's standard library cannot
read a server's certificate chain, only its leaf. Options:
- **(a) recommended:** the token **carries the CA certificate itself**, `<id>.<secret>.<CA, base64url DER>` (about 700
  characters for the P-256 CA). The Connector trusts exactly that CA, so the token is still the only thing handed to
  the customer, which was the point of the decision.
- (b) ship the CA file with the chart values beside a short token.

Everything else of §A8.2 stands: one-time, one hour, stored hashed, bound to the id, the key generated on the
Connector, the certificate signed with `connector:<id>`, the registry in the database.

## 8. A8.2 implemented (2026-10-09)

- `packages/connector/enrollment.py`: the token `<id>.<secret>.<CA>` (§7), the `Registry` (create, enroll,
  `is_allowed`), the `EnrollmentServer` (its own port, server-authenticated TLS, one method) and the Connector's
  `enroll()`, which trusts only the token's CA and checks that the certificate names its id and its own key.
- `packages/connector/pki.py`: `new_key_and_request` (on the Connector) and `sign_request` (on the control plane,
  taking only the public key; the identity is the id the token was bound to).
- The registry in the database (`connectors`, migration `0032_connectors`, `apps/control_plane/connector_registry.py`);
  the gateway admits an identity on the fixed allow-list **or** active in the registry.
- Control plane, remote mode: with `SRE_CONNECTOR_CA_KEY` the registry is built; with `SRE_CONNECTOR_ENROLL_LISTEN`
  as well, the enrollment port opens beside the gateway. Without them nothing changes (the lab runs as before).
- Operator: `agentic-sre connector create <id>` prints the token; `agentic-sre connector list`.
- Connector: on first start, with no certificate and `SRE_CONNECTOR_ENROLLMENT_TOKEN` set, it enrolls at
  `SRE_CONNECTOR_ENROLL_ENDPOINT` and writes its key (owner-only), certificate and CA where the session reads them.

Verification: 11 tests, over real gRPC on loopback: the one-time token (reuse, expiry, a wrong secret, an unknown id,
another CA, all refused), the certificate carrying only the token's identity whatever the request asked, a full
enrollment followed by a mutual-TLS session, a never-enrolled identity refused by the session port, a Connector
refusing a control plane that is not the token's CA, the first start writing an owner-only key and not enrolling
twice, and the registry surviving a control-plane restart in the database. The lab is not switched over here; that is
A8.5, with the chart.

## 9. A8.3 implemented (2026-10-09)

- **Rotation.** The session port serves `connector.v1.Session/Renew` over mutual TLS: the caller's own identity, if
  allowed and active in the registry, gets a certificate for a new key. The Connector checks hourly
  (`SRE_CONNECTOR_RENEW_CHECK_SECONDS`) and renews once two thirds of the validity have passed, writes the key
  (owner-only) and certificate atomically, and reopens its session with them at once; a failure is retried within
  five minutes and never stops the Connector. An identity only on the fixed allow-list has no renewal (its certificate
  is static, as before).
- **Revocation.** `agentic-sre connector disable <id>` marks it disabled: the next handshake is refused and the
  gateway's sweep closes the live session within `sweep_interval` (10 s); renewal is refused as well.
- **Expiry visibility.** The gateway records the certificate each connected Connector presented; the system status
  degrades the Connector row 14 days before expiry and names the days left.

Verification: 6 tests, over real gRPC on loopback (renewal of an active Connector only; a due certificate renewed and
the session reopened with it, files written owner-only; a revoked Connector's live session closed and every
reconnection and renewal refused; no renewal on the fixed allow-list; the recorded expiry; the status degrading 14 days
ahead). Full suite passes. The lab adopts all of it with the chart (A8.5).

## 10. A8.4 implemented (2026-10-09)

`charts/agentic-sre-connector`: a ServiceAccount; per watched namespace a Role and RoleBinding with `get`, `list`,
`watch` on the workload kinds and events (chaos kinds in the evidence namespaces, and beside the workloads unless
`watch.chaosBesideWorkloads=false`), never Secrets there and no ClusterRole; in the release namespace one Role on the
credentials Secret only, by `resourceNames`, `get` and `update`. The Deployment (one replica, `Recreate`) runs the
agent as a non-root user with a read-only root filesystem, no privilege escalation and every capability dropped; the
session's files are an in-memory volume, restored on start from the credentials Secret (`helm.sh/resource-policy:
keep`) and written back after an enrollment or a renewal (`packages/connector/credentials.py`). The enrollment token
and the webhook token come from values or existing Secrets (the webhook token is kept across upgrades). A values
schema refuses a missing endpoint, a malformed token or namespace, and an empty namespace list; `NOTES.txt` points to
`preflight`.

Verification: `helm lint` and `helm template` (`make chart-lint`, now a CI step); 5 tests on the rendered chart
(read-only RBAC in every watched namespace and never Secrets there, the one credentials Secret by name, the optional
chaos rule, the pod's security settings, the schema's refusals) and 3 on the credential store; the rendered chart
accepted by the lab's API server in a server-side dry run (15 objects, nothing created). Not installed in the lab yet:
that is A8.5.
