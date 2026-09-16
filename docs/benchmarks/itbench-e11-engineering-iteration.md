# E11 offline engineering iteration

This artifact records the provider-free engineering work around the same
single-agent configuration planned for E11. No Luna or judge was run, and the
historical E10 artifacts remain immutable.

## Baseline

The frozen E10 run completed all 35 scenarios: 16 `SUBMIT`, 19 `STOP`, local
fixed-35 macro F1 `0`, and micro F1 `0`. The frozen E7 retrieval baseline was
Recall@1 `8/35`, Recall@3 `11/35`, Recall@5 `12/35`, and Recall@10 `12/35`.
These numbers are historical observations, not targets encoded in runtime.

## Evidence correctness

Logs now use structured severity/error fields and bounded message patterns;
`error=false` and field names alone are not failures. Trace status accepts the
observed OTel forms `UNSET/OK/ERROR` and `0/1/2`, while unknown values remain
unknown. Metric series include normalized labels, are timestamp ordered, deduplicated,
and distinguish counters from gauges/histograms. Workload resource summaries
include container and init-container requests/limits. Replica comparison uses
owner/label relationships and reports unavailable for singletons. Change
analysis remains unavailable without trustworthy history.

## Runtime architecture

`E11InvestigationRuntime` is provider-injected. Fake and future OpenAI providers
enter the same request/response boundary, protocol validation, controlled
semantic execution, E9 event memory, E11 evidence memory, support gating, and
bounded termination path. OBSERVE is available before HYPOTHESIZE; VERIFY may
investigate any currently visible runtime-owned handle with an available
operation. Evidence assessments are deterministic and `NO_DATA` cannot be
used as support. Ranking revisions are recorded after semantic evidence while
handles remain stable.

## Qualification and boundary

The full snapshot runtime canary was completed as 35/35 runs. It used the
repository-owned E11 runtime with `FakeModelProvider`, produced 420 simulated
model responses, made zero real provider invocations, and accessed GT zero
times. The scripted canary reached the bounded `MODEL_STEP_LIMIT` terminal in
each scenario; this qualifies construction, parsing, semantic execution,
memory, evidence, and termination plumbing, not RCA quality.

The canary is also available as:

```text
.venv/bin/python scripts/itbench_e11_execute.py runtime-canary --dataset-root .local/itbench-lite
```

It is provider-free. Its fake responses qualify control-plane completeness,
not RCA quality. Retrieval outputs must be frozen before any post-hoc GT
evaluation. The future E11 model policy remains `openai / gpt-5.6-luna`,
reasoning `none`, retries `0`; no alternative model, router, or fallback is
introduced.

The clean retrieval qualification is recorded in
`itbench-e11-retrieval-qualification-v2.{json,md}`. B1 is the selected default
and preserves the current R1 gate (`R@1=.257`, `R@3=.400`, `R@5=.600`,
`R@10=.686`). Corrected telemetry is not the default because B2 regresses to
`.057/.200/.257/.314`; it remains available for semantic investigation and
future independent qualification. `E11 LIVE RUN = NOT RUN`.
