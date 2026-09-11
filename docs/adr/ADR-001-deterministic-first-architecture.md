# ADR-001: Deterministic-first architecture

- Status: Accepted
- Date: 2026-09-12

## Context

Agentic SRE will eventually support probabilistic investigation and
decision-making. The incident lifecycle, telemetry, provenance, and policy
boundaries must therefore be independently testable before an agent runtime is
introduced.

## Decision

`v0.1.0` contains no LLM or agent runtime.

The core SRE system must be independently testable, observable and
deterministic before probabilistic decision-making is introduced.

The release may define contracts for future agent capabilities, but those
contracts do not authorize model calls, agent orchestration, autonomous
remediation, Kubernetes writes, or arbitrary shell execution.

## Consequences

- Core behavior is reproducible and measurable.
- Incident state transitions have one deterministic authority.
- Read-only investigation and evidence provenance can be tested without a
  model provider.
- Future agent milestones must consume these boundaries rather than bypass
  them.
