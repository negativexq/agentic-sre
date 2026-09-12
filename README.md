# Agentic SRE

Deterministic SRE foundations for a future agentic investigation platform.

[![Version](https://img.shields.io/github/v/tag/negativexq/agentic-sre?sort=semver)](https://github.com/negativexq/agentic-sre/tags)
[![CI](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml/badge.svg)](https://github.com/negativexq/agentic-sre/actions/workflows/checks.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Kubernetes](https://img.shields.io/badge/runtime-kind-326CE5?logo=kubernetes&logoColor=white)](https://kind.sigs.k8s.io/)

> Current release: **`v0.1.1` — Live Observability Hardening**  
> Development target: **`v0.2.0` — Single-Agent Investigation Baseline**

Agentic SRE is built in deliberate milestones. The current releases establish a
measurable incident control plane before probabilistic decision-making is
introduced:

```text
Workload
   ↓
OpenTelemetry → Collector → Prometheus / Loki / Tempo → Grafana
                                      ↓
                                 Alertmanager
                                      ↓
                              Incident Control Plane
                                      ↓
                       Read-only Tools → Evidence Provenance
```

The live pipeline is validated on a local `kind` cluster with a production-like
order/payment workload, PostgreSQL, Redis, and Kafka.

## What is included

- Typed incident, alert, event, evidence, change, policy, action, and
  verification contracts using Pydantic v2.
- A deterministic incident lifecycle state machine with immutable timeline
  events.
- Persistent incident state and audit history through SQLAlchemy 2.x,
  PostgreSQL, and Alembic.
- FastAPI control-plane health, readiness, incident, timeline, alert, evidence,
  and Alertmanager webhook endpoints.
- A deterministic order/payment workload and seeded load generator.
- Live OpenTelemetry metrics, structured logs, and distributed traces.
- Prometheus, Loki, Tempo, Grafana, and Alertmanager running in `kind`.
- Alert deduplication and resolution handling from real Alertmanager payloads.
- Bounded, typed, audited, read-only investigation tools for metrics, logs,
  traces, and Kubernetes state.
- Fail-closed policy evaluation and provenance-backed evidence records.
- A credit-aware single-agent baseline built around a deterministic
  `FakeModelProvider` and explicit opt-in live model smoke.

## Scope boundary

Agentic reasoning is intentionally **not enabled** in `v0.1.1`. This repository
currently has:

```text
Default/CI live API calls  0
Agent framework imports   0
MCP / A2A                 absent
Autonomous remediation    absent
Kubernetes write verbs    0
Arbitrary shell API       absent
```

The project defines future-facing contracts, but no contract authorizes model
calls, agent orchestration, infrastructure writes, or autonomous remediation.
The next milestone may introduce a single investigator only after the live
telemetry and provenance boundaries remain stable.

See [ADR-001](docs/adr/ADR-001-deterministic-first-architecture.md) for the
deterministic-first decision and
[ADR-002](docs/adr/ADR-002-live-observability-prerequisite.md) for the live
observability prerequisite.

## Technology

| Area | Choice |
| --- | --- |
| Language | Python 3.12+ |
| API | FastAPI |
| Contracts | Pydantic v2 |
| Persistence | SQLAlchemy 2.x, PostgreSQL, Alembic |
| Testing | pytest |
| Quality | Ruff, mypy, pre-commit |
| Local runtime | kind, Docker, Kubernetes |
| Telemetry | OpenTelemetry Collector |
| Metrics | Prometheus |
| Logs | Loki |
| Traces | Tempo |
| Dashboards | Grafana |
| Alerts | Alertmanager |
| Messaging | Kafka |
| Cache | Redis |

## Quick start

### Prerequisites

For local development, install:

- Python 3.12+
- Docker
- `kind`
- `kubectl`

Create the virtual environment and install development dependencies:

```shell
make install
```

Run the local quality checks:

```shell
make check
```

`make check` runs Ruff, formatting validation, mypy, and the complete pytest
suite.

### Run the live runtime

Create the cluster and deploy the workload, control plane, dependencies, and
observability stack:

```shell
make cluster-up
make deploy
```

Then run the seeded workload and inspect the runtime:

```shell
make load
make status
make observability-check
make evidence-check
make rbac-check
```

The local cluster can be removed with:

```shell
make cluster-down
```

### Release validation

Run the complete deterministic release gate:

```shell
make release-check
```

This includes static checks, unit and integration tests, live backend checks,
evidence provenance validation, read-only Kubernetes RBAC checks, end-to-end
smoke scenarios, and state-machine coverage enforcement.

## Control-plane API

The FastAPI application exposes:

```text
GET  /health
GET  /ready

GET  /api/v1/incidents
GET  /api/v1/incidents/{incident_id}
GET  /api/v1/incidents/{incident_id}/events
GET  /api/v1/incidents/{incident_id}/alerts
GET  /api/v1/incidents/{incident_id}/evidence

POST /api/v1/webhooks/alertmanager
```

Errors use a typed response containing an error code, message, and correlation
ID. The readiness endpoint reflects database availability.

## Repository layout

```text
apps/control_plane/    FastAPI control plane
packages/contracts/    Pydantic domain contracts
packages/incident/     Lifecycle state machine and alert ingestion
packages/storage/      Database models and repositories
packages/telemetry/    Metrics, logs, tracing, and context propagation
packages/tools/        Bounded read-only investigation tools
packages/evidence/     Provenance-backed evidence service
packages/policy/       Deterministic fail-closed policy boundary
packages/changes/      Normalized infrastructure change records
packages/e2e/          Deterministic smoke harness
workload/              Seeded data and load generator
infra/                 Docker, Kubernetes, and observability manifests
tests/                 Unit, contract, integration, and E2E tests
docs/adr/              Architecture decision records
```

## Release status

### `v0.1.0` — Deterministic SRE Foundation

Established the contracts, lifecycle, persistence, control-plane API,
read-only tool boundaries, evidence provenance, and fail-closed policy
foundation.

### `v0.1.1` — Live Observability Hardening

Validated the workload-to-telemetry-to-incident pipeline against real runtime
backends in `kind`, including alert deduplication, alert resolution, live
investigation, backend failure handling, and provenance checks.

### Development target: `v0.2.0`

The single-agent baseline consumes the existing read-only tools and evidence
contracts. Daily checks and CI use the fake provider and make zero API calls.
Live model execution is available only through explicit commands after the
offline gates pass.

```shell
make agent-check       # local agent tests, live API calls: 0
make agent-smoke       # fake-provider smoke, live API calls: 0
make model-smoke-live  # explicit provider smoke, exactly 1 live call
make agent-smoke-live  # three existing incidents, max 9 live calls
make benchmark-live    # ten mapped incidents, max 30 live calls
make release-check-live # explicit full live gate, max 40 live calls
```

See [ADR-003](docs/adr/ADR-003-credit-aware-single-agent-baseline.md) for the
credit and safety boundary. The current offline benchmark is documented in
[docs/benchmarks/v0.2.0-single-agent.md](docs/benchmarks/v0.2.0-single-agent.md).

`agent-smoke-live` selects the three newest incidents by default. Set
`SRE_LIVE_INCIDENT_IDS` to provide an explicit comma-separated set. The
one-pass `benchmark-live` requires ten ordered IDs in
`SRE_BENCHMARK_INCIDENT_IDS`; both commands perform a worst-case budget
preflight before making a model request.

## Development conventions

Commits follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/)
and remain single-line by default:

```text
feat(incident): implement deterministic lifecycle state machine
```

Before committing a coherent change, run:

```shell
make check
```

## License

MIT. See [`LICENSE`](LICENSE).
