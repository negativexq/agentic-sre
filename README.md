# Agentic SRE

Agentic SRE is being built in deliberate milestones. Release `v0.1.0` is a
deterministic SRE foundation: workload, telemetry, alerting, incident
lifecycle, provenance, bounded read-only investigation tools, and a
fail-closed policy boundary.

Agentic reasoning is intentionally not enabled in `v0.1.0`.

This release establishes the deterministic workload, incident lifecycle,
observability, provenance, policy and investigation tool boundaries required
before introducing an LLM.

## Development

The project requires Python 3.12+.

```shell
make install
make check
```

`make check` runs Ruff, mypy, and pytest in that order.

## Scope boundary

The deterministic foundation contains no LLM calls, LangGraph, agent
framework, prompts, embeddings, vector database, agent memory, A2A, autonomous
remediation, Kubernetes writes, or arbitrary shell execution API.

See [ADR-001](docs/adr/ADR-001-deterministic-first-architecture.md) for the
architecture decision.
