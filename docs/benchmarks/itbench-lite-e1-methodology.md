# ITB-E1 methodology record

ITB-E1 is preserved as an invalid integration attempt, not as a model-quality
benchmark result. Scenarios 1–9 reached the old A1 decision validator before
any outbound request and were recorded as `INVALID_DECISION`; Scenario-11
then exposed an external snapshot-tool runtime failure. The old runner also
used the internal A1 workload/resource ontology for an external Kubernetes
benchmark. ITB-E2 introduces an independent ITBench-native protocol and keeps
the E1 checkpoints and six charged calls immutable.
