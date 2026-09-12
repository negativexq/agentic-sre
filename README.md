# Agentic SRE

Release: `v0.1.1 — Live Observability Hardening`

Agentic SRE is being built in deliberate milestones. Release `v0.1.1` is the
live observability hardening release on top of the `v0.1.0` deterministic SRE
foundation.

Agentic reasoning is intentionally not enabled in `v0.1.1`.

This release establishes the deterministic workload, incident lifecycle,
observability, provenance, policy and investigation tool boundaries required
before introducing an LLM. Live kind validation covers OpenTelemetry
Collector, Prometheus, Loki, Tempo, Grafana, Alertmanager, incident
ingestion, bounded investigation tools, and evidence provenance.

## Development

The project requires Python 3.12+.

```shell
make install
make check
```

`make check` runs Ruff, mypy, and pytest in that order.

## Local runtime

The deterministic workload can be run on kind with PostgreSQL, Redis, and
Kafka:

```shell
make cluster-up
make deploy
make load
make status
make observability-check
make evidence-check
```

`make deploy` builds and loads the local images, applies the manifests, and
runs the Alembic migration Job before the workload smoke load.

The release gate is:

```shell
make release-check
```

## Scope boundary

The deterministic foundation contains no LLM calls, LangGraph, agent
framework, prompts, embeddings, vector database, agent memory, A2A, autonomous
remediation, Kubernetes writes, or arbitrary shell execution API.

See [ADR-001](docs/adr/ADR-001-deterministic-first-architecture.md) for the
architecture decision. See [ADR-002](docs/adr/ADR-002-live-observability-prerequisite.md)
for the live observability prerequisite.

Live runtime invariant:

```text
LLM calls                    0
agent framework imports      0
Kubernetes write verbs       0
autonomous remediation       absent
```
