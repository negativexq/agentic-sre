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
