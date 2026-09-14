# Internal vs external evaluation

The internal A1 evaluation uses 14 frozen live scenarios to test real fault
activation, telemetry integration, control-plane lifecycle, authority
containment, and platform safety.

The external ITBench-Lite evaluation uses 35 immutable IBM Research SRE
snapshots to test diagnosis and generalization against third-party scenarios.
It uses an ITBench-native Kubernetes entity contract while preserving
Agentic-SRE's single investigator, bounded read-only tools, runtime-owned
evidence, strict decisions, and no-remediation policy.

The official ITBench reference tooling uses semantic operations such as alert
summary, context/entity analysis, topology analysis, metric analysis, and
trace analysis. This adapter follows those concepts without adding shell,
filesystem browsing, arbitrary Python, arbitrary SQL, or arbitrary PromQL to
the model authority boundary.
