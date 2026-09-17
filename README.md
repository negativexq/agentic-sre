# Agentic SRE

Deterministic-first root-cause analysis for Kubernetes incidents. Agentic SRE
starts with the incident symptoms, reconstructs what changed, connects
candidate causes to those symptoms, verifies the strongest explanation, and
shows the evidence and causal path. It proposes reversible remediation; the
control plane never modifies the cluster.

The model is optional. Evidence collection, temporal boundaries, ranking,
verification, replay, and safety constraints remain deterministic system
behavior.

[![CI](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml/badge.svg)](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

## What it does

- Change-first RCA for Kubernetes alerts, rollouts, policies, faults, and dependency failures.
- Append-only object observations, replayable Kubernetes Events, and bounded persisted Loki error observations.
- Explicit incident observation cutoffs and deterministic resolved-incident replay.
- Relation-aware directional causal traversal rather than generic undirected proximity.
- Deterministic confidence and verification: `VERIFIED`, `LIKELY`, or `UNVERIFIED`.
- Causal paths visible in JSON/API output, the CLI, and the HTML report.
- Real Kind lifecycle validation through Prometheus, Alertmanager, and the control plane.
- Optional bounded LLM investigation around deterministic candidates.
- Remediation proposals only; no cluster writes or autonomous repair.

## Measured results

The frozen ITBench-Lite SRE snapshot benchmark is regression evidence, not a
claim of pristine unseen generalization. Prior exposure to parts of the test
data is documented in [`evals/README.md`](evals/README.md).

| Run | Split | Scenarios | Macro F1 | Top-1 | Top-3 | Model calls |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| **Engine v1.0.1** | Test | 25 | **0.680** | **68%** | **80%** | **0** |
| Engine v1.0.0 | Test | 25 | 0.680 | 68% | 80% | 0 |
| Engine v0.7.0 | Dev | 10 | 0.900 | 90% | 90% | 0 |
| Engine v0.5.0 + LLM investigator | Test | 25 | 0.640 | 64% | 80% | 44 |

Confidence is separate from benchmark correctness: 23/25 predictions were
labelled `VERIFIED`, 16 of those were correct, giving 92.0% VERIFIED coverage
and 69.6% VERIFIED accuracy. v1.0.1 hardening changed lifecycle, replay,
causal-traversal, and release infrastructure without changing the v1.0.0
root-cause or confidence predictions.

The latest hardening validation passed 175 tests and the real Kind release
gate. The stored [v1.0.1 result](evals/results/v1.0.1/README.md) records the
tagged release SHA.

## Real Kubernetes validation

The canonical release gate runs a real incident lifecycle in a fresh Kind
cluster:

```text
healthy payment-service
  → baseline snapshot
  → real bad Deployment rollout
  → Prometheus alert
  → Alertmanager webhook
  → incident and persisted observations
  → deterministic RCA: sre-demo/Deployment/payment-service VERIFIED
  → rollback
  → A → B → A object journal
  → stable resolved diagnosis replay
```

The scenario also exercises Kubernetes Event persistence. It does not claim
that every supported fault type has a real-cluster E2E scenario.

Run the full gate with:

```bash
make e2e-kind
```

## How it works

```mermaid
flowchart LR
  K8s[Kubernetes API<br/>read-only] --> O[Persisted observations]
  Loki[Loki] --> O
  AM[Prometheus / Alertmanager] --> CP[Control plane]
  CP --> O
  O --> E[Deterministic RCA engine]
  E --> T[Directional causal topology]
  T --> R[Ranking + verification]
  R -. optional review .-> L[Bounded LLM investigator]
  R --> D[Diagnosis<br/>evidence + causal path + proposal]
  L --> D
```

1. The control plane creates and freezes incident episodes from Alertmanager events.
2. Object, Event, and bounded log observations are normalized with explicit observation times.
3. The engine extracts deterministic signals from changes, failures, policies, dependencies, and Events.
4. Directional topology links a candidate cause to the alerting symptom.
5. Ranking proposes candidates; deterministic verification assigns confidence.
6. The optional investigator may review bounded candidates with read-only tools. It is not authoritative.

More detail is in [`docs/architecture.md`](docs/architecture.md) and
[ADR-003](docs/adr/ADR-003-change-first-rca-product.md).

## Example diagnosis

The offline demo produces a diagnosis like:

```text
Root cause   shop/Deployment/payment
Confidence   VERIFIED

Causal path
  Deployment/payment --serves--> Service/payment
  Service/payment --dependency_of--> Deployment/checkout

Evidence
  Deployment/payment changed FAULT_DELAY_MS from 0 to 2500
  the diagnostic alerts began after the rollout

Proposed remediation
  kubectl rollout undo deployment/payment -n shop
  [not executed]
```

## Quick start

Offline:

```bash
make install
make demo
```

The demo writes `.local/demo/diagnosis.html`. For a live local cluster,
install Docker, Kind, and kubectl, then run:

```bash
make cluster-up
make deploy
make load                  # use another terminal; generates steady traffic
make inject-bad-rollout
make ui                    # http://localhost:8080
make recover
```

`make rbac-check` verifies that the control-plane service account can observe
the cluster but cannot read Secrets or write workloads.

## Causal reasoning

Structural connectivity answers “what is connected?”; causal traversal asks
“could a change or failure here propagate to that symptom?”. The engine uses
explicit direction for relationships such as:

```text
Deployment → ReplicaSet → Pod
ConfigMap → consuming workload
backend dependency → caller
NetworkPolicy → selected Pod
Chaos fault → target
HPA → controlled workload
```

Unknown relations are structural-only until explicitly allowlisted. Shared
ConfigMaps and broad NetworkPolicies do not causally bridge sibling workloads.
Causal paths retain structured entities and relation labels and are rendered
in the CLI and HTML report. This is relation-aware causal traversal, not formal
causal inference.

## Evidence and replay

- **Objects:** append-only versions preserve `CREATED`, `UPDATED`, and `DELETED` lifecycle evidence, including A → B → A rollback history. A partial Kubernetes listing cannot fabricate deletion tombstones.
- **Events:** observed Kubernetes Event versions remain in the journal; replay uses the latest visible logical Event state at the incident cutoff, so coalesced versions are not double-counted.
- **Logs:** bounded error observations captured from Loki are persisted for replay. This is captured-observation replay, not a complete historical log archive.
- **Cutoffs:** open diagnosis uses a coherent snapshot boundary; resolved diagnosis freezes at incident resolution. Evidence observed later cannot leak backward into a resolved incident.

Alert fingerprints identify an alert shape, while `(fingerprint, starts_at)`
identifies one occurrence. A firing alert after resolution creates a new
incident episode; concurrent duplicate delivery of one occurrence is
database-idempotent.

## Optional LLM investigator

The LLM investigator is off by default and bounded by a call budget. It reviews
deterministically generated candidates through read-only tools; it cannot
invent candidates, create evidence, or assign final confidence. The
deterministic engine remains the default and source of truth.

The measured v0.5.0 bounded LLM run did not improve the deterministic engine on
ITBench-Lite, so the LLM remains optional and replaceable. It is not used by
the published deterministic benchmark or the Kind release gate.

## Safety and trust boundaries

- Kubernetes access is read-only; Secrets are deliberately not read.
- Remediation is proposal text and is never executed by the control plane.
- Write API endpoints can use the shared `SRE_API_TOKEN` bearer token.
- Built-in read endpoints are unauthenticated and may expose operationally sensitive incident, evidence, topology, and log-derived data.
- There is no per-user identity, rate limiting, or built-in read authentication.
- The supported writer model is one control-plane process / one replica; the journal lock is not multi-replica coordination.

GitHub Actions runs `check`, `images`, and `kind-e2e` on pushes.

## Reproducibility and benchmark methodology

The tested Python dependency graph is captured in [`uv.lock`](uv.lock), and CI
and Docker builds use the locked environment. The ITBench-Lite setup uses a
pinned snapshot revision. Frozen test prediction requires a clean tagged
commit and never reads ground truth; prediction files are sealed before
grading. `seal.json` protects the prediction manifest and prediction files;
`report-seal.json` separately protects derived grading reports.

```bash
make itbench-setup     # downloads the pinned ITBench-Lite snapshots
make itbench-index
make eval-dev
make eval-test         # release-only: clean tagged commit
```

To re-grade the stored v1.0.1 run:

```bash
agentic-sre grade --out evals/results/v1.0.1/test
```

Do not repeatedly run the frozen test while tuning. The immutable `v1.0.1`
tag remains at the release-code commit; its published result records that
tagged SHA in the current release result directory.

## Repository structure

```text
packages/rca          deterministic RCA engine, topology, ranking, reports
packages/storage      incident, observation, and journal persistence
apps/control_plane    API, Alertmanager webhook, UI, and live diagnosis
apps/cli              agentic-sre CLI and benchmark entrypoints
packages/evals        ITBench integration and grading
evals                 splits, methods, and sealed results
infra                 Docker, Kind, Kubernetes, and observability manifests
tests                 unit, integration, and release regression coverage
```

## Current limitations

- RCA quality depends on evidence that is observable and captured by the configured sources.
- Bounded captured Loki observations are not a complete historical log archive.
- The built-in deployment is single-process/single-replica and is not production HA.
- Read endpoints are unauthenticated by default; the shared bearer token has no per-user identity or RBAC.
- There is no autonomous remediation, arbitrary write tool, or formal causal-inference guarantee.
- The deterministic benchmark is frozen regression evidence with prior-exposure caveats; generalization to unseen incidents is not established.
- The value of the optional LLM investigator on harder, messier live incidents remains unproven.
