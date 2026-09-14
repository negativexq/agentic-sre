# Internal vs external evaluation

Agentic SRE keeps two evaluation surfaces deliberately separate.

## Internal A1 evaluation

The internal A1 harness uses 14 frozen live scenarios, a local Kubernetes fault
lifecycle, Prometheus, Alertmanager, OTel, control-plane incident correlation,
and read-only authority checks. It validates product integration, telemetry
causality, state isolation, and safety boundaries. Its historical revisions and
ledgers remain immutable.

## External ITBench-Lite evaluation

The external path uses the pinned IBM Research ITBench-Lite SRE snapshot: 35
immutable third-party scenarios with snapshot evidence and evaluator-only
ground truth. It validates diagnosis against an independently designed public
benchmark without reproducing the internal fault-injection environment.

The external adapter exposes only bounded alerts, metrics, Kubernetes events and
objects, logs, and traces through named read-only tools. Ground truth is loaded
only by the evaluator. Native Agentic-SRE artifacts remain authoritative; an
ITBench-compatible output is a deterministic export. The official
ITBench-Evaluations repository is a secondary evaluator and is not run during
the zero-model adapter qualification.
