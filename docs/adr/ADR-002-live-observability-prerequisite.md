# ADR-002: Live observability is a prerequisite for agentic investigation

- Status: Accepted
- Date: 2026-09-12

## Context

The future investigator must consume incident signals from real runtime
backends. Contract-only telemetry does not prove that metrics, logs, traces,
alerts, and provenance can be correlated during an incident.

## Decision

`v0.1.1` validates the complete workload-to-incident pipeline on kind:

```text
workload → OpenTelemetry → Collector → Prometheus/Loki/Tempo
         → Alertmanager → Control Plane → read-only tools → evidence
```

Agent reasoning will not be introduced until this live pipeline and its
failure behavior are verified. This release contains no LLM, agent runtime,
MCP, A2A, remediation executor, or Kubernetes write capability.

## Consequences

- The investigation boundary is tested against real backend APIs.
- Eventual consistency and backend outages are explicit test cases.
- Future agent milestones inherit measured telemetry and provenance rather
  than inventing observations.
