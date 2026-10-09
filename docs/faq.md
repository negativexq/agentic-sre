# FAQ

Moved from the README, unchanged.

## FAQ

### What is Agentic SRE?

It is a Kubernetes incident investigator that gathers bounded read-only
evidence and produces an auditable, deterministic diagnosis.

### How does Agentic SRE perform root-cause analysis?

It reconstructs changes and symptoms at an incident cutoff, forms causal
hypotheses from typed Findings, identifies information gaps, and performs legal
bounded reads when evidence is insufficient. Each observation is normalized and
the hypotheses are rebuilt before deterministic verification and resolution.

### Does Agentic SRE require an LLM?

No. The measured path used zero model calls. An optional policy may choose a
legal bounded observation, but the model cannot create evidence or judge the
root cause.

### What telemetry can Agentic SRE investigate?

It can use Kubernetes object history and Events, Alertmanager context, captured
Loki logs, traces, Prometheus signals, and configured snapshots. Coverage
depends on the sources available for an incident.

### Can Agentic SRE modify my Kubernetes cluster?

No autonomous cluster writes are available. Investigation is read-only and
remediation is returned as a proposal for an operator to review and execute.

### How accurate is Agentic SRE?

Against ITBench-Lite ground truth the frozen architecture of its time reached
**17/22 (77.3%)** on TEST25, which was blind when frozen, and **26/31 (83.9%)**
across all 35 scenarios, with zero model calls (historical). The last measured
engine (2.2.2) scores **18/31** exactly, and 20/31 on an evidence-backed equivalence track
reported beside it ([C11](architecture/c11-itbench-equivalence.md)). Denominators exclude four scenarios whose published labels match
nothing in their own snapshots. Since 2026-09-28 those 35 are development data,
so these figures are regression evidence, not a generalization estimate; the
held-out measurement is the own testbed: across six fault families every injected
cause was named in 35 of 36 runs with no false strong authority, a small result
reported with its limits in [Measured performance](results/measured-performance.md#instrumented-testbed). Historically, over all 35 scenarios, `VERIFIED`
predictions were 13/16 correct against ground truth.

### What makes it different from an AI SRE agent?

The investigator and the judge are separate. A bounded policy can select a
legal read, while deterministic normalization, hypothesis rebuilding,
verification, and root-cause resolution remain authoritative. This makes the
evidence path inspectable and keeps an LLM from turning a plausible answer into
an unverified diagnosis.

### Is Agentic SRE production-ready?

It is suitable for controlled evaluation and read-only incident-assistance
workflows, with a real Kind lifecycle gate and a frozen benchmark that was blind
when it was frozen (and is development data now). It is
not a universal replacement for an experienced SRE: query coverage,
authentication, deployment high availability, bounded budgets, and captured
telemetry impose real limits.
