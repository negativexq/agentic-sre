# Agentic SRE

Evidence-grounded incident investigation with real observability, bounded LLM
reasoning, read-only tools, provenance, and reproducible fault benchmarks.

[![Version](https://img.shields.io/github/v/tag/negativexq/agentic-sre?sort=semver)](https://github.com/negativexq/agentic-sre/tags)
[![CI](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml/badge.svg)](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Kubernetes](https://img.shields.io/badge/runtime-kind-326CE5?logo=kubernetes&logoColor=white)](https://kind.sigs.k8s.io/)

> Current release: **`v0.2.0` — Single-Agent Investigation Baseline**

Agentic SRE turns a real production-shaped failure into a persistent incident,
then lets one bounded investigator select read-only evidence and submit a
structured root-cause hypothesis. The runtime, not the model, owns authority,
budgets, evidence provenance, and termination.

## Current architecture

```text
Order / Payment / Worker
          │
          ▼
    OpenTelemetry
          │
          ▼
Prometheus · Loki · Tempo
          │
          ▼
     Alertmanager
          │
          ▼
 Persistent Incident Control Plane
          │
          ▼
 Deterministic Investigation Runtime
          │
          ▼
   Single LLM Investigator
          │
          ▼
   Typed Read-Only Tool Registry
   │       │       │       │
 Metrics  Logs   Traces  Kubernetes
                              │
                           Changes
          │
          ▼
 Provenance-Backed Evidence
          │
          ▼
 Structured RCA Hypothesis / STOP
```

The model-driven boundary is deliberately narrow: the investigator chooses
what evidence to inspect and when to conclude. Deterministic code validates and
executes every request.

## What v0.2.0 actually does

The released baseline includes:

- one bounded investigator using a fixed GPT-5.6 Luna evaluation configuration;
- a three-model-call maximum and eight-actual-tool-execution maximum per incident;
- typed decision functions for tool requests, hypotheses, and STOP outcomes;
- 17 bounded read-only tools across metrics, logs, traces, Kubernetes, and changes;
- cross-turn evidence and progress state with duplicate request suppression;
- authoritative incident observation windows and backend-specific time encoding;
- persistent historical change records;
- deterministic argument, budget, provenance, and safety validation; and
- a real fault → telemetry → Alertmanager → Incident benchmark harness.

GPT-5.6 Luna describes the released benchmark configuration; it is not a claim
that the platform requires one model implementation.

## v0.2.0 frozen live benchmark

| Metric | Result |
| --- | ---: |
| Frozen scenarios | 10 |
| Completion | 90% |
| Service accuracy | 40% |
| Mechanism accuracy | 80% |
| Trigger accuracy | 0% |
| Composite RCA | 50% |
| Valid evidence references | 90% |
| Model calls / incident | 3.00 |
| Tool executions / incident | 6.60 |
| Fabricated evidence | 0 |
| Cross-incident evidence | 0 |
| Provider/runtime failures | 0 |

Each frozen scenario ran exactly once against a real fault → telemetry →
Alertmanager → Incident chain. No LLM judge was used and no failed scenario was
rerun. The single-pass methodology bounds paid API usage and establishes a
baseline; it does not estimate model variance.

The measured baseline identifies mechanisms more reliably than causal service
or trigger attribution. This is intentionally preserved as the comparison
baseline for later investigation architectures.

See the [live benchmark report](docs/benchmarks/v0.2.0-single-agent.md),
[machine-readable benchmark](docs/benchmarks/v0.2.0-single-agent-live.json), and
[zero-LLM harness qualification](docs/benchmarks/v0.2.0-harness-qualification.json).

## Why this is not an agent demo

The model chooses what to inspect. It does not control:

- tool implementations or backend query syntax;
- query bounds or authoritative observation windows;
- evidence creation or incident ownership;
- execution budgets or policy decisions;
- Kubernetes permissions; or
- infrastructure mutation and remediation.

The runtime owns typed arguments, the tool allowlist, bounded execution,
provenance, validation, termination, and read-only enforcement. The model never
creates Evidence records and cannot turn telemetry text into instructions.

## Safety boundary

v0.2.0 includes live LLM investigation, but authority remains intentionally
narrow.

| Capability | v0.2.0 |
| --- | --- |
| Single investigator | Yes |
| Read-only metrics, logs, and traces | Yes |
| Read-only Kubernetes | Yes |
| Historical change evidence | Yes |
| Infrastructure writes | No |
| Kubernetes write verbs | No |
| Arbitrary shell | No |
| Autonomous remediation | No |
| Multi-agent orchestration | No |
| MCP | No |
| A2A | No |

## Evidence surface

The released registry contains 17 tools. Their descriptions and strict argument
contracts are generated from the same registered definitions used for runtime
validation.

### Metrics

`service_error_rate`, `service_latency`, `db_connection_pressure`,
`db_query_latency`, `kafka_consumer_lag`

### Logs

`service_logs`, `service_error_logs`

### Traces

`slow_traces`, `trace_detail`

### Kubernetes

`kubernetes_pods`, `kubernetes_deployment`, `kubernetes_events`,
`kubernetes_rollout_history`, `kubernetes_container_restarts`,
`kubernetes_resource_state`

### Change intelligence

`recent_deployment_changes`, `recent_configuration_changes`

Tool execution is normalized into evidence with incident ownership,
source/backend, tool identity, canonical arguments, effective observation
window, temporal mode, and collection time. Hypothesis evidence IDs are checked
before acceptance. In the frozen live benchmark, fabricated evidence and
cross-incident evidence were both `0`.

Evidence may be classified as `FIXED_WINDOW`, `CURRENT_STATE`,
`HISTORICAL_EVENT`, or `HISTORICAL_CHANGE`. Resolved incidents use their
authoritative incident observation window rather than an unrelated current-time
query; wire timestamp units are adapted separately for Prometheus, Loki, and
Tempo.

## Real fault benchmark harness

Benchmark fixtures do not construct Incident objects directly. Each trial uses
the real control-plane path:

```text
controlled fault
→ workload effect
→ real telemetry
→ Prometheus alert
→ Alertmanager webhook
→ persistent Incident
→ investigation
→ cleanup and recovery
```

Trials are sequential so current-state evidence remains observable while the
fault is under investigation. The harness qualification completed all ten
lifecycles with zero model calls: **10/10 PASS**. Benchmark setup mutations are
controlled test authority and are never exposed through the investigation
registry.

| Scenario | Frozen fixture |
| --- | --- |
| V020-001 | `payment_error_spike` |
| V020-002 | `order_error_spike` |
| V020-003 | `payment_dependency_latency` |
| V020-004 | `order_latency_spike` |
| V020-005 | `payment_db_pool_pressure` |
| V020-006 | `order_db_query_latency` |
| V020-007 | `order_worker_lag` |
| V020-008 | `order_worker_failure` |
| V020-009 | `payment_pod_crash` |
| V020-010 | `payment_config_change` |

## Quick start

### Prerequisites

Install Python 3.12+, Docker, `kind`, and `kubectl`.

```shell
make install
make check
```

### Run the local runtime

```shell
make cluster-up
make deploy
make observability-check
make evidence-check
make rbac-check
```

Use `make load` and `make status` to stimulate and inspect the local runtime.
Remove the cluster with `make cluster-down`.

### Optional live model evaluation

Live model commands are explicit, paid operations. They require an API key and
the shared budget ledger; direct release commands fail closed without that
ledger. The canonical release benchmark creates its own real incidents through
the harness and runs once per frozen scenario. Review the [harness methodology](docs/benchmarks/v0.2.0-harness-methodology.md)
and the [live benchmark report](docs/benchmarks/v0.2.0-single-agent.md) before
running any live evaluation.

### Release validation

```shell
make release-check
make release-check-live
```

`make release-check` runs offline and local deterministic engineering gates.
`make release-check-live` validates the committed live-release evidence,
qualification, benchmark, safety, accounting, and configuration artifacts. It
does not rerun the LLM benchmark or make a new model call.

## Repository layout

```text
apps/control_plane/    FastAPI incident control plane
packages/contracts/    Pydantic domain contracts
packages/incident/     Lifecycle state machine and ingestion
packages/investigation/Decision protocol, context, registry, runtime
packages/provider/     Fake/live provider boundaries and budget accounting
packages/tools/        Bounded read-only tools and live backends
packages/evidence/     Provenance-backed evidence service
packages/changes/      Historical change records
packages/evals/        Frozen dataset, fixtures, harness, graders
packages/telemetry/    Metrics, logs, tracing, context propagation
packages/storage/      SQLAlchemy models and repositories
infra/                 Kubernetes and observability manifests
tests/                 Unit, contract, integration, and E2E tests
docs/adr/              Architecture decision records
```

## Evidence and design docs

- [ADR-001: deterministic-first architecture](docs/adr/ADR-001-deterministic-first-architecture.md)
- [ADR-002: live observability prerequisite](docs/adr/ADR-002-live-observability-prerequisite.md)
- [ADR-003: credit-aware single-agent baseline](docs/adr/ADR-003-credit-aware-single-agent-baseline.md)
- [ADR-004: bounded investigation protocol](docs/adr/ADR-004-bounded-investigation-protocol.md)
- [v0.2.0 harness methodology](docs/benchmarks/v0.2.0-harness-methodology.md)
- [v0.2.0 harness qualification](docs/benchmarks/v0.2.0-harness-qualification.json)
- [v0.2.0 live benchmark](docs/benchmarks/v0.2.0-single-agent.md)
- [v0.2.0 release evidence](docs/benchmarks/v0.2.0-release-evidence.json)

## Release history

### `v0.1.0` — Deterministic SRE Foundation

Established the contracts, lifecycle, persistence, control-plane API,
read-only boundaries, evidence provenance, and fail-closed policy foundation.

### `v0.1.1` — Live Observability Hardening

Validated the workload-to-telemetry-to-incident pipeline against real local
backends, including alert lifecycle, backend failures, and provenance checks.

### `v0.2.0` — Single-Agent Investigation Baseline — CURRENT

Adds the bounded single investigator, 17 read-only tools, real-fault benchmark
harness, deterministic decision protocol, and the frozen ten-scenario live
baseline documented above.

### `v0.3.0` — Evidence-Grounded Multi-Agent Investigation — NEXT / planned

Future architecture work only. Multi-agent investigation is not implemented in
this release.

## Development conventions

Commits follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/).
Before a code or documentation commit, run:

```shell
make check
```

## License

MIT. See [`LICENSE`](LICENSE).
