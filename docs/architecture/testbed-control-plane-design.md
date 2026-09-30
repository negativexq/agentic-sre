# Control plane outside the lab (roadmap B3)

Status: **PROPOSED** (2026-09-30). Nothing here is implemented. Companion to
`testbed-lab-design.md` (the lab), `connector-boundary-contract.md` (§12 transport, §13 status) and
`testbed-ground-truth-contract.md` (what a run records).

## 1. Purpose

Run the control plane **outside** the lab, on the host, and let the engine see the lab only through a
Connector that runs inside it and dials out. That is the boundary of the product, exercised for real:
the control plane holds no kubeconfig and no lab credential, and a lab that is wiped and recreated takes
no diagnosis history with it.

## 2. What the running lab already provides (measured 2026-09-30)

- A pod in the lab resolves `host.docker.internal` (to `192.168.65.254`), so a Connector in the lab can
  dial a gateway that listens on the host.
- `agentic-sre-reader` (ServiceAccount, Roles and RoleBindings in `sre-demo` and `chaos-mesh`) is an
  existing, tested read-only identity (`make rbac-check`).
- Alertmanager's receiver is configured in the `alertmanager-config` ConfigMap of `observability.yaml`
  and points at the in-cluster control plane, which the lab does not run.
- The gateway, the agent process with its local webhook receiver, and a remote mode of the control plane
  exist and are tested (`connector-boundary-contract.md` §13).

## 3. Placement

| Piece | Where | Notes |
|---|---|---|
| Control plane (`agentic-sre serve`) | host | `SRE_CONNECTOR_MODE=remote`; gateway on `0.0.0.0:8443`; console on `:8080` |
| Control-plane Postgres | host, a container with a named volume | its own database, not the lab's; survives a lab recreation |
| Connector | lab, namespace `connector` | dials `host.docker.internal:8443`; holds every lab credential |
| Injector and oracle | host | unchanged (`testbed-lab-design.md` §6, §7) |

The control plane is started with **no kubeconfig** (an empty `KUBECONFIG`), and every cluster read it
makes arrives through the stream.

## 4. The Connector in the lab

- **Image.** A new `connector` target in `infra/docker/Dockerfile` (the same base as the other
  components, `CMD ["python", "-m", "packages.connector.agent"]`), built and loaded by
  `make build-images` with the others.
- **Identity and access.** A `connector` namespace and ServiceAccount. RoleBindings give it the **existing
  read-only rules** in `sre-demo` and `chaos-mesh`, and the same rules in `lab-control` (so the engine
  sees the negative-control workload as a customer's connector would). It can list and watch, never
  write, and cannot read `Secret` objects (enforced twice: RBAC and the connector's deny list).
- **Backends.** In-cluster Loki, Prometheus, Tempo and Alertmanager through the existing environment
  variables (`SRE_LOKI_URL`, `PROMETHEUS_URL`, `TEMPO_URL`, `SRE_ALERTMANAGER_URL`); watched namespaces
  `sre-demo` and `lab-control`, evidence namespace `chaos-mesh`.
- **Alerts.** A `connector-webhook` Service in front of the agent's local receiver (`:9095/webhook`,
  bearer token). The lab's Alertmanager is pointed at it by an **overriding** `alertmanager-config`
  applied after `observability.yaml` (which stays as it is, so the existing in-cluster demo flow keeps
  working); the token is generated at bring-up and is not committed.

## 5. Certificates and secrets

`make lab-pki` generates, under `.local/lab/pki` (ignored by git, keys `0600`): a CA, the control plane's
server certificate (names `host.docker.internal`, `localhost`, `127.0.0.1`), and one client certificate
for the connector id `lab`. Only the connector's certificate, key and the CA enter the cluster, as the
Secret `connector-tls`; **the server key never does**. The webhook token follows the same rule. Static,
90 days, no rotation (`connector-boundary-contract.md` §12.4); expiry is a hard failure shown in system
status.

## 6. Commands

`make lab-pki`, `make connector-deploy` (Secret, RBAC, Deployment, Service, the Alertmanager override and
its restart), `make cp-up` (Postgres container, migration, the control plane with the remote-mode
environment, log in `.local/lab/cp.log`), `make cp-down` (stops the control plane and its Postgres but
**keeps the volume**; removing it is a separate `make cp-reset`). `make lab-up` does not start the control
plane; `make lab-check` gains the connector checks below.

## 7. Gate

B3 is done when all of these hold and are recorded:

1. The connector session is up and the console's system status shows the Connector connected.
2. With no kubeconfig for the control plane, its database still holds journaled objects from the lab.
3. A forced fault (a chaos delay under load) produces an incident in the control plane's database through
   the stream and an automatic diagnosis is stored.
4. Deleting the connector pod makes the control plane report the connector unavailable (and close the
   coverage segment); when the pod returns it reattaches and resumes its cursor with no `Gap`.
5. Restarting the control plane makes the connector reconnect on its own.
6. The connector's identity cannot write: `can-i` denies create, patch and delete, and denies reading
   secrets, in every watched namespace.
7. The lab can be recreated (`kind delete`, `make lab-up`, `make connector-deploy`) while the control
   plane's database keeps its history.

## 8. Not in scope

Helm, enrollment, certificate rotation and revocation (roadmap A8); more than one connector per control
plane; running the control plane in a container; the stream-mode default (`connector-boundary-contract.md`
§12.9); any change to the existing in-cluster demo manifests.

## 9. Decisions requested

1. The placement of §3: control plane and its own Postgres on the host, the Connector in a `connector`
   namespace of the lab.
2. The Connector gets the **existing read-only rules** plus the same rules in `lab-control`, no more.
3. The Alertmanager override is applied in the lab only; `observability.yaml` is left untouched.
4. Certificates and the webhook token are generated at bring-up under `.local/lab` and never committed;
   only the connector's material enters the cluster.
5. `cp-down` keeps the Postgres volume; deletion is a separate, explicit target.
6. The gate of §7, including deleting the connector pod and recreating the lab while the control plane's
   history survives.
