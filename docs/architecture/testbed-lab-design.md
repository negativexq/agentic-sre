# Testbed lab design (roadmap B2)

Status: **APPROVED** by the owner (2026-09-30), including the six decisions of §10 as recommended.
Nothing here is applied yet. Recreating the lab is destructive: the design is approved, but the
recreation itself is asked again when its prerequisites (roadmap A7 and the B1 implementation) hold. Companion to `testbed-ground-truth-contract.md`
(what a run records) and `connector-boundary-contract.md` (how the engine reads the lab).

## 1. Purpose

Recreate the `agentic-sre` Kind cluster as a **lab**: a world we can inject faults into and whose
truth we record. The lab holds the workload, its dependencies, the existing observability stack,
Chaos Mesh, and (from A7) the Connector. It does **not** hold the control plane (B3).

## 2. Measured state that shapes the design (2026-09-30)

- One Kind node (containerd), Kubernetes 1.37.0, no published ports. The Docker VM has 9.7 GiB;
  the node uses 3.0 GiB (`roadmap.md` §10).
- The existing live suite already reaches the workload through **`kubectl port-forward`** processes
  it supervises (`packages/evals/live/actions.py`, ports 18000, 18001, 18080, 19090). No published
  ports are needed.
- The in-cluster Postgres holds **no tables** and has no persistent volume. Most likely its data was
  lost at the collective restart of 2026-09-26 (every pod's last termination is that instant) and
  the `db-migration` job was not re-run; no such job exists now. The running control plane's watcher
  logs an SQLAlchemy `ProgrammingError` on each cycle, which is consistent with the missing schema
  (the cause was not traced further). Nothing is lost by recreating the lab, and the same failure
  must not recur.
- Chaos Mesh chart **2.8.4** (rendered, not installed): defaults are `runtime: docker` with
  `socketPath: /var/run/docker.sock`, which **would not work** on this containerd node (its socket
  is `/run/containerd/containerd.sock`); three controller replicas, a dashboard and a DNS server are
  on by default; the chart brings 23 CRDs; the daemon is privileged with `hostPID` and hostPath
  mounts and sets no resource requests.

## 3. Composition

| Namespace | Contents | Notes |
|---|---|---|
| `sre-demo` | order-service, payment-service, order-worker, Postgres, Redis, Kafka with the topic reconciler | from the repository manifests |
| `observability` | Prometheus, Loki, Tempo, Grafana, Alertmanager, OTel collector, kube-state-metrics | unchanged |
| `chaos-mesh` | controller, daemon | new (§4) |
| `lab-control` | one small workload with no call edge to `sre-demo`, for the negative control | new |
| `connector` | the Connector (from A7) | later |

The control plane and its own Postgres run outside the lab (B3). The injector and the oracle run on
the host (§6, §7).

## 4. Chaos Mesh

Pinned chart `chaos-mesh` **2.8.4** from `https://charts.chaos-mesh.org`, namespace `chaos-mesh`,
values fixed in the repository:

- `chaosDaemon.runtime=containerd`, `chaosDaemon.socketPath=/run/containerd/containerd.sock`;
- `controllerManager.replicaCount=1`, `dashboard.create=false`, `dnsServer.create=false` (no
  family uses DNS chaos), `enableProfiling=false`.

Requested memory at these values is one controller at 256 Mi and nothing else (the daemon sets
none), about 0.26 GiB. The privileged daemon is acceptable in a lab and nowhere else. Images are
loaded into the node beforehand (`kind load`) so a run never waits on a registry.

The injector uses its own credentials (an administrator kubeconfig on the host) to create chaos
objects and roll workloads. The Connector and the existing read-only `agentic-sre-reader` role stay
read-only; the engine never holds the injector's identity.

## 5. Kind configuration

A single control-plane node, the node image **pinned by digest to the one already present locally**
(`kindest/node:v1.37.0@sha256:a1ed56cf…`, no pull; recorded in `kind-config.yaml`),
and no `extraPortMappings`. Kind's containerd and the Kubernetes version stay as they are today so
that results measured now remain comparable.

## 6. Reaching the workload

The injector supervises `kubectl port-forward` processes with the existing supervisor (it restarts
one that a rollout invalidated) and drives workload traffic through them. A channel that is down
marks the run `INVALID` (`testbed-ground-truth-contract.md` §4.3), it never becomes an empty
observation.

## 7. Oracle

The oracle is a separate host process that probes the workload endpoints through its own
port-forwards and records per-endpoint success and latency at one hertz with the injector's UTC
clock. Per-hop probes and the traffic graph used to check a negative control are specified with the
scenarios (B4); B2 only provides the channels.

## 8. Bring-up procedure and gate

1. Chaos Mesh inputs are recorded (done): the chart archive is vendored in
   `infra/kubernetes/chaos-mesh/` with its digest, the values and the image digests are pinned
   in `pins.yaml`, and `make lab-images` and `make chaos-mesh-install` use them. A rendered
   manifest is deliberately **not** stored: helm generates fresh webhook certificates on every
   render, so it would be unstable and would put private keys in the repository.
2. Delete the Kind cluster `agentic-sre`; create it from the pinned configuration.
3. Load the application and Chaos Mesh images; apply `namespace`, RBAC, dependencies, observability
   and workload manifests; **run the migration as a declared, idempotent step and give Postgres a
   volume that survives a container restart** (as Kafka's now does).
4. Install Chaos Mesh with the pinned values.
5. Gate. Every item must hold before B2 is closed:
   - all pods `Running`; the `orders.created` topic present; the database schema at head;
   - 23 chaos CRDs present; a `NetworkChaos` smoke on `payment-service` shows `Applied` and
     `Recovered` events carrying the experiment UID, and the workload recovers;
   - the alert path fires for a forced fault and reaches Alertmanager;
   - a node-level restart of the Kind container leaves the topic and the schema intact;
   - resource use is recorded and compared with `roadmap.md` §10.

## 9. What is destroyed

The Kind node container and its 6.3 GB volume; every in-cluster object; the Kafka and Postgres data
(both effectively empty or reproducible). Kept: the repository, the locally built images, `.local`,
the unrelated Docker volume `3145b5b7b563`. Rollback is a second recreation from the same
declarative inputs; there is no state to restore.

## 10. Decisions (approved 2026-09-30, all as recommended)

1. Delete and recreate the `agentic-sre` cluster as described (§8, §9).
2. No `extraPortMappings`; the workload and the oracle are reached through supervised
   port-forwards (§5, §6).
3. Chaos Mesh 2.8.4 with the values of §4, including the containerd settings, one controller and
   no dashboard or DNS server.
4. The namespace layout of §3, including `lab-control` for the negative control.
5. Postgres gets a volume that survives a container restart, and the schema migration becomes a
   declared, idempotent bring-up step (§8.3).
6. The Kubernetes and containerd versions stay pinned to today's for comparability (§5).

## 11. Preparation status (2026-09-30)

Done, without touching the running cluster: the Chaos Mesh chart 2.8.4 vendored with its digest, the
pinned values and image digests (`pins.yaml`), the Kind configuration pinned by digest with no
published port, and the `lab-images`, `chaos-mesh-install` and `chaos-mesh-uninstall` targets. The chart
renders offline from the vendored archive and the containerd settings reach the daemon
(`--runtime containerd`, hostPath `/run/containerd`). A test (`test_lab_manifests.py`) keeps these
inputs, and the Kafka topic reconciler of B0, from drifting. Not done: the destructive recreation
itself, the `lab-control` workload and the migration step as a declared bring-up action (all part of
the recreation, which is asked again).

## 12. Bring-up manifests (2026-09-30)

Written and validated against the live API without applying them (a server-side dry run of
`dependencies.yaml`, a strict client-side validation of `lab-control.yaml`):

- **Postgres** keeps its data in an `emptyDir` (it survives a container restart) and has a readiness
  probe. The schema migration is a `migrate` **sidecar** rather than a one-off job: it waits for the
  database, runs `alembic upgrade head` (idempotent) and repeats every minute, so a pod that returns
  empty converges again. The old `db-migration` job stays for the existing `make deploy` flow.
- **`lab-control`** (`infra/kubernetes/lab-control.yaml`): a namespace, a one-replica `isolated-echo`
  workload on the pinned `python` image, a service, and a default-deny `NetworkPolicy` for ingress and
  egress. `make lab-check` fails if `sre-demo` can reach it, so "no path" is checked. The image is in
  `pins.yaml` and loaded by `make lab-images`.
- **`make lab-up`** creates the cluster, builds and loads images, applies everything except the control
  plane, waits for every rollout, installs Chaos Mesh and runs `make lab-check`. It never deletes a
  cluster. **`make lab-check`** checks the pods, the topic, the schema, the 23 CRDs and the isolation; the
  chaos smoke, the alert path and the node restart stay manual and are recorded.

Findings while writing them:

- A check must fail when its tool fails. The first draft of `lab-check` passed its pod check when
  `kubectl` itself could not connect (an empty list satisfied `test -z`); each check now reports a tool
  failure, and a test asserts that. `KUBECTL` selects the context (`make lab-check KUBECTL="kubectl
  --context kind-agentic-sre"`); this machine has no default `kubectl` context.
- Against the running cluster the check passes the pod and topic checks and **fails on the schema**, which
  is the known state: the live database has no tables. Applying `dependencies.yaml` there would repair
  that at once through the new sidecar; that was not done, because the cluster is about to be recreated.

The destructive recreation is the only step left of B2.

## 13. Recreation record (2026-09-30)

The lab was recreated with `make lab-up` from the pinned inputs. Outcome of the gate of section 8:

- All pods `Running`; `orders.created` present; schema at head (migrated by the sidecar); 23 Chaos Mesh
  CRDs; the isolated workload unreachable from `sre-demo`. `make lab-check`: PASS.
- **Chaos smoke.** A 30 s `NetworkChaos` delay on `payment-service` produced `Applied` (02:55:30) and
  `Recovered` (02:55:59) events for one experiment UID, naming the target pod; the request latency from
  `order-service` went from about 1 ms to about 0.6 s and returned to baseline.
- **Alert path.** With load running, a 110 s delay fired its first alert
  (`PaymentDbQueryLatencyHigh`) 30 s after the fault, and by 45 s the chain
  (`OrderDependencyLatencyHigh`, `PaymentRequestLatencyHigh`, `HighRequestLatency`) was active in
  Alertmanager; all cleared after the fault ended. The control plane is not in the lab, so webhook
  delivery was not part of this check.
- **Node restart.** `docker restart` of the node: the API answered after 11 s, both containers of the Kafka
  pod restarted once, a topic that no sidecar declares (`persist-marker`) and a marker row in an
  undeclared table survived, and `make lab-check` passed again.
- **Resources** (after the gate): the node 2.0 GiB of 9.7 GiB and 0.45 CPU; container working sets 1,634
  MiB (`sre-demo` 717, `kube-system` 588, `observability` 268, Chaos Mesh 34, `lab-control` 16). The
  measured Chaos Mesh use is far below its 0.26 GiB request.

Three defects surfaced during the recreation and were fixed before it passed:

1. `uv.lock` was stale after the connector change added `grpcio` and `cryptography` to `pyproject.toml`, so
   every image build (`uv export --locked`) failed. The lock was refreshed inside a container with the
   Dockerfile's `uv` version; the diff is four lines, no version changed.
2. `kind load` cannot import the multi-platform Chaos Mesh images from the local Docker store ("content
   digest not found"). `make lab-images` now has the node pull the pinned tags and refuses any whose
   digest differs from `pins.yaml`.
3. `lab-check` failed a healthy lab because the topic converges on its own a few seconds after Kafka starts.
   The gate now retries for up to two minutes and reports the last failure.

B2 is done. What remains for the testbed is B3 (the control plane outside the lab) and B4 (scenarios).
