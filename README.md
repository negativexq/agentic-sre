# Agentic SRE — Deterministic Root Cause Analysis for Kubernetes Incidents

Agentic SRE is an evidence-driven root-cause analysis engine for Kubernetes
incidents. It performs bounded, read-only investigation over changes, events,
logs, traces, dependencies, and topology, then rebuilds hypotheses and makes
the final root-cause judgment deterministically. An LLM is optional; it never
owns the diagnosis.

[![CI](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml/badge.svg)](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

### At a glance

- **[84% exact root-cause accuracy](evals/results/v1.1.2/README.md)** — 26/31 against ITBench-Lite ground truth, over every scenario whose published label is matchable.
- **0 model calls** — the measured benchmark path is fully deterministic.
- **Bounded investigation** — the frozen TEST25 run used six validated physical reads per incident, one read at a time.
- **Evidence-backed RCA** — observations become normalized Findings before they can change a diagnosis.
- **Read-only by design** — the investigator cannot mutate the cluster or execute remediation.

## Measured root-cause performance

The current frozen architecture was evaluated against ITBench-Lite ground
truth with exact canonical entity comparison. The blind TEST25 result is the
primary public measurement; the development split is shown separately so
development evidence is not confused with holdout evidence.

Four of the 35 published labels cannot be matched by any prediction: their
entity filters match nothing in the scenario's own snapshot (Scenario-29, 23,
38, 105; Scenario-38's filter is not even a valid expression). Those four are
excluded from the accuracy denominator and reported separately.

| Evaluation | Exact root-cause accuracy | Raw | Model calls |
| --- | ---: | ---: | ---: |
| DEV10 development split | 9/9 (100%) | 9/10 | 0 |
| **Blind TEST25 holdout** | **[17/22 (77.3%)](evals/results/v1.1.2/README.md)** | 17/25 | **0** |
| **Combined 35 scenarios** | **26/31 (83.9%)** | 26/35 | **0** |

Confidence against ground truth on the 31 scoreable scenarios: `VERIFIED`
predictions were 13/16 correct and `LIKELY` predictions 13/19.

These accuracy figures are measured on the deterministic full-source path. The
bounded investigation path is measured separately, by how often it reaches the
same answer as the full-source path (21/25 on TEST25); its own ground-truth
accuracy has not been established.

On the frozen TEST25 run, every scenario used six validated physical reads:
150 reads across 25 incidents. The run produced 2,226 new evidence references and 244
normalized Finding emissions. The detailed [frozen benchmark report](evals/results/v1.1.2/README.md)
contains the dataset revision, manifest hash, prediction-freeze procedure, and
full aggregate metrics.

## What is Agentic SRE?

Agentic SRE is a Kubernetes incident investigation and SRE root-cause
analysis system. It starts from an alert and an observation cutoff, identifies
candidate causal actors, explains how they can reach the affected workload, and
tests unresolved questions with a bounded set of legal read-only observations.

The investigator gathers evidence; it does not decide the root cause. Every
new observation returns through the same deterministic path:

```text
observation → EvidenceStore → normalization → Finding
            → hypothesis rebuild → verification → resolution
```

This separation makes an investigation auditable. A diagnosis includes the
selected root entity, confidence, resolution state, evidence, and causal path;
it does not rely on an opaque model answer.

## Why Agentic SRE?

### Evidence before answers

The engine does not ask a model to guess what caused an incident. It acquires
bounded evidence, normalizes that evidence into typed Findings, and rebuilds
the deterministic diagnosis.

### Deterministic judgment

The RCA engine owns verification, confidence, resolution, and root-cause
selection. An optional policy can choose among already-legal observation
actions, but it cannot create evidence, Findings, hypotheses, or a root cause.

### Bounded autonomous investigation

Investigation is a controlled state machine with explicit turn, tool, wall-time,
per-gap, invalid-action, and no-progress limits. It selects one validated read
at a time from a legal observation surface.

### Causal topology

The engine distinguishes structural connectivity from directional causal paths.
Ownership, configuration use, declared dependencies, policies, fault targets,
scaling relationships, and workload topology are interpreted as explicit
relations rather than generic graph proximity.

### Reproducibility and safety

The deterministic path produces repeatable trajectories for the same snapshot
and configuration. Kubernetes access is read-only, remediation is proposed
but never executed, and `NO_DATA` is neutral rather than evidence for a theory.

## How does Agentic SRE find a root cause?

```mermaid
flowchart TD
    A[Alert or incident] --> B[Initial observation view]
    B --> J1[Deterministic RCA]
    J1 --> H[Hypotheses and information gaps]

    subgraph I[Investigator: bounded evidence acquisition]
        H --> P[Select one legal read]
        P --> V[Validate scope and budget]
        V --> R[Execute one read-only observation]
    end

    R --> E[EvidenceStore]
    E --> N[Normalize observations into Findings]
    N --> J2[Rebuild hypotheses]
    J2 --> Q[Verify and resolve deterministically]
    Q -->|remaining gap| P
    Q --> O[Root cause, confidence, causal path, proposal]
```

The runtime is orchestrated as a bounded LangGraph state machine:

```text
assess → select action → validate → execute one read
       → check novelty → normalize → rebuild hypotheses
       → check progress → repeat or finalize
```

LangGraph coordinates this state machine; it does not determine the root
cause. The same RCA and normalization code is used for initial observations and
new investigation evidence.

## What it investigates

Supported evidence depends on the configured observation sources, but the
engine understands these Kubernetes incident classes and signals:

- Deployment, ReplicaSet, Pod, ConfigMap, image, environment, and scale changes.
- Kubernetes Events, including warning events, failed scheduling, quota failures,
  container failures, and HPA metric failures.
- Dependency errors from bounded log observations and declared workload calls.
- Runtime traces and captured Loki observations when those sources are present.
- Network policy changes, resource pressure, quota/LimitRange behavior, and
  traffic changes when the corresponding observation data is available.
- Chaos Mesh faults and their targeted workloads.
- Ownership, selectors, configuration references, service dependencies, HPA
  relationships, and other topology needed to build a causal path.

Captured logs are replayable evidence, not an unbounded historical log archive.
The engine reports when a signal is missing instead of treating missing data as
proof against a hypothesis.

## Example diagnosis

The offline demo produces a diagnosis in this form:

```text
Root cause   shop/Deployment/payment
Confidence   VERIFIED
Resolution   RESOLVED

Causal path
  Deployment/payment --serves--> Service/payment
  Service/payment --dependency_of--> Deployment/checkout

Evidence
  Deployment/payment changed FAULT_DELAY_MS from 0 to 2500
  diagnostic alerts began after the rollout

Proposed remediation
  kubectl rollout undo deployment/payment -n shop
  [proposed only; not executed]
```

## Quick start

For the deterministic offline demo:

```bash
make install
make demo
```

The report is written to `.local/demo/diagnosis.html`. The CLI also supports a
JSON diagnosis with `agentic-sre demo --json`.

For a local live Kubernetes validation with Kind, Docker, and kubectl:

```bash
make cluster-up
make deploy
make load                  # use another terminal to generate steady traffic
make inject-bad-rollout
make ui                    # http://localhost:8080
make recover
```

Use `make rbac-check` to verify that the control-plane service account can
observe the cluster without reading Secrets or writing workloads. The complete
real-cluster lifecycle gate is `make e2e-kind`.

## Architecture and trust boundaries

Agentic SRE has four practical layers:

1. **RCA engine** — deterministic signals, causal hypotheses, verification,
   confidence, resolution, and remediation proposals.
2. **Investigation runtime** — bounded evidence acquisition through validated,
   read-only observation tools.
3. **Observation sources** — Kubernetes object versions and Events, Prometheus
   and Alertmanager context, Loki logs, traces, and configured snapshot data.
4. **Control plane** — incident lifecycle, persistence, API, CLI, and HTML/UI
   reporting.

The trust boundary is explicit:

- Kubernetes observation is read-only; Secrets are deliberately not read.
- There is no arbitrary shell execution or autonomous cluster write capability.
- Remediation text is proposed for an operator and is never executed.
- Only in-scope, allowlisted observation capabilities can run.
- Invalid actions, duplicate reads, tool errors, `NO_DATA`, and exhausted
  budgets terminate safely with the current deterministic diagnosis.
- A diagnosis changes only after typed evidence is normalized into Findings and
  the hypotheses are rebuilt.

The built-in deployment is currently a single control-plane process/replica.
Read endpoints are unauthenticated by default and can expose operationally
sensitive incident and log-derived data, so deployment authentication and
network controls remain an operator responsibility.

## Real Kubernetes validation

The Kind lifecycle validation exercises the product against a real cluster:

```text
healthy workload
  → injected Deployment rollout failure
  → Prometheus alert
  → Alertmanager incident
  → persisted observations and RCA
  → proposed rollback
  → A → B → A object journal
  → stable resolved diagnosis replay
```

This validates the incident path, observation persistence, replay, and safety
boundary. It is complementary to the ITBench-Lite benchmark and is not a claim
that every supported fault class has a live-cluster end-to-end scenario.

## Benchmark methodology and reproducibility

The benchmark uses the pinned ITBench-Lite revision
`d0916b08ba421ce5e672e9ad68aa947d938dfef0` and manifest SHA256
`08a5e56dbfa604c59eed8282683d7b3ec224cd7db9303f90618dafd436423eac`.

DEV10 is the development split used while building the frozen architecture.
TEST25 was held out until that architecture was frozen. For TEST25, all 25
bounded predictions were persisted and hashed before any FULL_SOURCE diagnosis
was opened. Grading then compared exact canonical entities by scenario ID.
The run used the deterministic policy and zero model calls.

**Exact root-cause accuracy** means that the predicted canonical root entity
matches the scenario's published ITBench-Lite ground-truth entity. A
same-workload or nearby entity does not count as a match. Separately, the
bounded prediction agreed with the FULL_SOURCE deterministic diagnosis on
21/25 TEST25 scenarios; that agreement measures information loss under a
bounded read budget, not correctness.

The [public benchmark report](evals/results/v1.1.2/README.md) records the
commit, dataset identity, prediction artifact hash, frozen configuration, and
aggregate results. Historical runs remain available under
[`evals/results/`](evals/results/) for reproducibility; they are not the main
product claim.

## FAQ

### What is Agentic SRE?

Agentic SRE is a Kubernetes root-cause analysis engine for automated incident
investigation. It combines deterministic causal reasoning with bounded,
read-only evidence acquisition and produces an auditable diagnosis.

### How does Agentic SRE perform root-cause analysis?

It reconstructs changes and symptoms at an incident cutoff, forms causal
hypotheses from typed Findings, identifies information gaps, and performs legal
bounded reads when evidence is insufficient. Each observation is normalized and
the hypotheses are rebuilt before deterministic verification and resolution.

### Does Agentic SRE require an LLM?

No. The default and measured benchmark path is deterministic and used zero
model calls. An optional LLM policy can choose among bounded semantic
observation choices, but the model cannot create evidence or own the final
root-cause judgment.

### What telemetry can Agentic SRE investigate?

It can use Kubernetes object history and Events, Alertmanager incident context,
captured Loki logs, runtime traces, Prometheus-derived signals, and configured
snapshot observations. Actual coverage depends on which sources were captured;
the system does not pretend that an unavailable query surface contains data.

### Can Agentic SRE modify my Kubernetes cluster?

No autonomous cluster writes are available. Investigation is read-only and
remediation is returned as a proposal for an operator to review and execute.

### How accurate is Agentic SRE?

Against ITBench-Lite ground truth it reached **17/22 (77.3%)** on the blind
TEST25 holdout and **26/31 (83.9%)** across all 35 scenarios, with zero model
calls. Denominators exclude four scenarios whose published labels match
nothing in their own snapshots. This is a measured 35-scenario benchmark
result, not a universal accuracy guarantee; `VERIFIED` predictions were 13/16
correct against ground truth.

### What makes it different from an AI SRE agent?

The investigator and the judge are separate. A bounded policy can select a
legal read, while deterministic normalization, hypothesis rebuilding,
verification, and root-cause resolution remain authoritative. This makes the
evidence path inspectable and keeps an LLM from turning a plausible answer into
an unverified diagnosis.

### Is Agentic SRE production-ready?

It is suitable for controlled evaluation and read-only incident-assistance
workflows, with a real Kind lifecycle gate and a frozen blind benchmark. It is
not a universal replacement for an experienced SRE: query coverage,
authentication, deployment high availability, bounded budgets, and captured
telemetry impose real limits.

## Current limitations

- Bounded query windows and captured telemetry can miss older or unavailable
  decisive evidence.
- Captured Loki data is not a complete historical log archive.
- Investigation has fixed turn, read, wall-time, and per-gap budgets.
- Some diagnoses depend on the configured read APIs and their authentication;
  built-in read endpoints are unauthenticated by default.
- The supported deployment is single-process/single-replica rather than HA.
- The system is evidence-driven RCA, not formal causal inference.
- There is no autonomous remediation, arbitrary shell execution, or cluster write
  tool.
- Current generalization evidence is the frozen 25-scenario blind TEST25 run;
  larger and more diverse production datasets are still needed.

## Repository structure

```text
packages/rca          deterministic RCA engine, topology, ranking, reports
packages/storage      incident, observation, and journal persistence
apps/control_plane    API, Alertmanager webhook, UI, and live diagnosis
apps/cli              agentic-sre CLI and benchmark entrypoints
packages/evals        ITBench-Lite integration and grading
evals                 benchmark splits, methodology, and public results
infra                 Docker, Kind, Kubernetes, and observability manifests
tests                 unit, integration, and release regression coverage
```

## Documentation

- [Architecture details](docs/architecture.md)
- [Evaluation methodology](evals/README.md)
- [Frozen benchmark report](evals/results/v1.1.2/README.md)
- [Architecture decision records](docs/adr/)
- [Release documentation](docs/releases/)
