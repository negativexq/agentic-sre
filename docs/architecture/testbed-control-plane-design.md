# Control plane outside the lab (roadmap B3)

Status: **APPROVED** by the owner (2026-09-30), including the six decisions of §9 as recommended.
Implementation follows; the record is in §10. Companion to
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
   coverage segment); when the pod returns it reattaches. A new pod is a new process and a new epoch, so the
   stream declares `CONNECTOR_RESTART` and the control plane rebuilds from a snapshot. (Resuming a cursor with
   no `Gap` is what a dropped connection of the same process does; that is covered by the transport tests.)
5. Restarting the control plane makes the connector reconnect on its own.
6. The connector's identity cannot write: `can-i` denies create, patch and delete, and denies reading
   secrets, in every watched namespace.
7. The lab can be recreated (`kind delete`, `make lab-up`, `make connector-deploy`) while the control
   plane's database keeps its history.

## 8. Not in scope

Helm, enrollment, certificate rotation and revocation (roadmap A8); more than one connector per control
plane; running the control plane in a container; the stream-mode default (`connector-boundary-contract.md`
§12.9); any change to the existing in-cluster demo manifests.

## 9. Decisions (approved 2026-09-30, all as recommended)

1. The placement of §3: control plane and its own Postgres on the host, the Connector in a `connector`
   namespace of the lab.
2. The Connector gets the **existing read-only rules** plus the same rules in `lab-control`, no more.
3. The Alertmanager override is applied in the lab only; `observability.yaml` is left untouched.
4. Certificates and the webhook token are generated at bring-up under `.local/lab` and never committed;
   only the connector's material enters the cluster.
5. `cp-down` keeps the Postgres volume; deletion is a separate, explicit target.
6. The gate of §7, including deleting the connector pod and recreating the lab while the control plane's
   history survives.

## 10. Implementation record (2026-09-30)

Built: the `connector` image target, `packages/connector/lab.py` (static certificates and the lab's
Alertmanager configuration), the Connector chart with `infra/kubernetes/connector-values.yaml` (connector-install-design.md §A8.5), and the targets `lab-pki`,
`connector-deploy`, `connector-check`, `cp-up`, `cp-down`, `cp-reset`. Tests: 9 for the helpers and the
manifests' promises (no credential committed, read-only rules, `cp-down` keeps the volume, the control plane
starts with `KUBECONFIG=/dev/null`, the demo manifests are untouched).

Gate of section 7, against the live lab:

1. **Session.** The control plane started degraded with no connector, then the connector connected and the
   console showed `Connector connected: lab`. PASS.
2. **No kubeconfig.** With `KUBECONFIG=/dev/null` the control plane's own database held 31 journaled lab
   objects (25 in `sre-demo`, 6 in `lab-control`) and its events, all read through the connector. PASS.
3. **Fault under load.** A 120 s `NetworkChaos` delay under load produced, through Alertmanager, the
   connector's local receiver and the stream, 4 incidents and 4 automatic diagnoses about 66 s after the fault;
   every diagnosis named the injected experiment as its root cause. PASS.
5. **Control plane restart.** After `cp-down` and `cp-up` the connector was back in about 4 s with no action,
   and the incident history was intact (4 incidents, no duplicates). PASS.
6. **Read-only identity.** `make connector-check`: the connector lists pods in `sre-demo` and `lab-control`
   and chaos objects in `chaos-mesh`, and is denied create, patch and delete, reading secrets, and creating
   or deleting chaos objects in all three. PASS.
4. **Connector pod deleted** and 7. **lab recreated while the history survives**: not yet run; both change
   the lab and are asked for separately.

Defects found and fixed while doing it:

- The first `connector-check` demanded `list pods` in `chaos-mesh`, where the Role deliberately allows only
  chaos objects and events; the check was wrong, not the identity. It now asserts what each namespace is meant
  to allow and additionally that the connector cannot inject a fault.
- The console reported Prometheus, Loki and Tempo as "not configured, set the variable" while they were
  configured on the connector, because it looked at its own environment. In remote mode the rows now come from
  the capabilities the connector reports, and read "unavailable" when no connector is connected.
- With no connector the intake logged the same warning every second. It now logs a change of state once.

Observation: after the restart the control plane stored 11 further diagnosis revisions in about a minute
(15 against 4 before), consistent with its re-evaluation loop running on its first cycles; not investigated.

Gate item 4 was run against the live lab (2026-09-30): after the pod was deleted the control plane reported
`unavailable` within 2 s and `connected` again by 4 s; the coverage segment open before the deletion was closed
at its last success (10:56:40) with a `ConnectorUnavailable` failure recorded at 10:56:45, and the new
connector's first heartbeat opened the next segment at 10:56:48, so the alert-channel coverage `W` restarted at
the moment of the loss. The incident history was untouched and the watch loop logged no error. The design text of
item 4 expected a resumed cursor with no `Gap`; that was wrong for a restarted process and is corrected above.

Gate item 7 was run against the live lab (2026-09-30): the lab cluster was deleted and recreated with
`make lab-up` (`lab check: PASS` on the first attempt) and the connector redeployed with `make
connector-deploy` while the control plane kept running. The connector attached in about 3 s with the same
certificate. The control plane's history was identical before and after (4 incidents, 19 diagnoses, 4 alerts,
all four incidents still readable), and its journal grew from 31 to 69 object versions as the new lab was
observed; the journal semantics held (an object whose name returned under a new UID is recorded as an update
with the new UID, objects that did not return as deleted). **All seven gate items pass; B3 is done.**

One more defect found in that run and fixed: while the lab was away the watch loop logged a full traceback
("cluster snapshot failed") every 15 s for about eight minutes (96 lines). The connector being away is an
expected state, so the loop now reports the loss once and its return once, without a traceback; other
failures still log as before. Two logging tests were also made independent of test order (they attach their
own handler and use `setLevel`, since assigning a logger's level does not clear Python's enabled-for cache).
