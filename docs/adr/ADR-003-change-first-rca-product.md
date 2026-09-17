# ADR-003: Change-first RCA product

- Status: Accepted
- Date: 2026-09-17
- Supersedes: the single-agent investigation baseline (v0.2.0) and the E1–E11
  ITBench experiment series, preserved in the `archive/experiments-2026-09` tag

## Context

The v0.2.0 agent chose read-only tools freely and was asked to prove a cause
before answering. On ITBench-Lite it scored macro F1 0.0: it followed
background platform alerts, never saw the evidence it requested in later
iterations, and stopped without an answer in most scenarios. Snapshot data
showed that most incidents are explained by a small number of observable
changes (a ConfigMap edit, a rollout, an injected fault) that the agent never
looked at, because change history was reported as unavailable.

The repository had also grown into a series of per-experiment runtimes,
scripts, and qualification documents that were hard to change safely.

## Decision

Agentic SRE is a product that answers one question well: *what changed or
broke, and what is the evidence?*

1. A deterministic engine (`packages/rca`) extracts signals first: object
   version diffs from a change journal, fault experiments, restrictive
   policies, dependency connection errors, and warning events. It links them to
   alerting components through a derived topology (ownership, selectors,
   configuration references, and service calls declared in environment
   variables) and ranks candidates with explainable scores.
2. Every incident gets an answer. Deterministic rules label it `VERIFIED`,
   `LIKELY`, or `UNVERIFIED`; the label is shown and scored separately.
3. An optional LLM investigator reviews the ranking with read-only tools and
   may pick another candidate. It cannot create evidence or verify itself.
   Live model calls require explicit opt-in and a call budget.
4. Remediation is proposed, never executed.
5. One runtime serves the CLI, the control plane, and benchmarks. Benchmarks
   are regression checks with a pre-registered dev/test split, not the goal.

## Consequences

- The engine runs offline in about half a second per incident and is fully
  unit tested; model quality is an add-on, not a dependency.
- The control plane needs read access to objects and events (never Secrets)
  and keeps a journal of object versions to see changes.
- Rules are generic Kubernetes reasoning, but they were designed after
  studying failures on the same benchmark; `evals/README.md` discloses this.
- New experiments must reuse this runtime and add a measured change, not a
  new module or script family.
