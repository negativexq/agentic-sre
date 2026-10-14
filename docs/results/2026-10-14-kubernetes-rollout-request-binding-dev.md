# Real Kubernetes DEV: rollout mechanism and scoped request accounting

The current branch was built under separate image tags and exercised in an owned namespace. The successful authoritative capture is **D**, namespace `binding-dev-04edf7a6`. Its immutable Kubernetes/OTLP observations were then evaluated offline by the existing engine and the unregistered DEV scope guard. The shared control plane, connector, `sre-demo` workloads and HOLDOUT records were not changed. All experiment namespaces and owned forwards were cleaned up.

This is one successful DEV scenario, not a new HOLDOUT or production rollout. The HTTP workload uses the real Order/Payment application, middleware and gateway code. Pod-local SQLite and an in-memory publisher replace non-HTTP dependencies. Kafka/Redis roles are outside this experiment.

## Observations

- Ten baseline POST `/orders` requests returned 201.
- A Payment deployment template update set `FAULT_PAYMENT_DELAY_MS` to a nonnumeric value. The new revision could not start.
- Exact ownership and instance UIDs, ReplicaSet/Pod ADDED events, old Pod DELETE, and new Pod BackOff were captured from Kubernetes.
- Thirty subsequent driver requests returned 500.
- Loki supplied 60 error logs: 30 dependency refusals and 30 parent HTTP errors. The dependency group contains 30 `ConnectionRefusedError` records with explicit request/client/parent identities.
- Tempo supplied a full reference of 150 spans. The independently initiated 40 requests were found in the remote reference. This is bounded DEV reference coverage, not global tracing completeness.

The symptom episode is **driver-defined**, not a Prometheus alert. Its declared rule latches on the first observed POST response >=500 after ten successful baseline requests; onset is that response's completion time. Driver observation starts with the first baseline request. Only this declared driver channel is supplied as alert coverage. No production alarm-rate sufficiency is claimed.

## Paired result

All profiles share the same Kubernetes history, events, dependency logs, symptom episode and already-captured pre-change baseline. Only the available spans differ. The common pre-change baseline has 60 spans; acquisition of that reference baseline is not part of the targeted-read cost below.

| Span profile | Current logs accounted by DEV guard | Existing resolver | Existing resolver with experimental scope guard |
| --- | --- | --- | --- |
| Failed CLIENT spans withheld | 0 / 30 | AMBIGUOUS | AMBIGUOUS |
| Actual bounded Kubernetes target query | 8 / 30 | RESOLVED | AMBIGUOUS |
| Exact log-ID client/parent retrieval | 30 / 30 | RESOLVED | RESOLVED |
| Full reference | 30 / 30 | RESOLVED | RESOLVED |

Every leading root above is the Payment deployment. Without failed CLIENT evidence it has plausible support but no strong mechanism witness. The bounded target query already supplies enough calls to fire the existing failed-rollout mechanism rule. Its eight selected traces provide 24 current spans; combined with the common baseline the profile has 84 spans. However, the stricter experimental accounting guard leaves the Service manifestation unexplained for 22 uncaptured operations.

Targeted retrieval uses 30 HTTP trace fetches in four bounded reads and retains 60 current client/parent spans. Together with the common baseline it has 120 spans. All 30 log members bind to operations cited by the existing strong witness; no aggregate member remains unaccounted. Its guarded root/support/decision matches the full-reference result, which has 150 spans.

**There is no additional production RESOLVED gain over the existing bounded query in this scenario.** The result demonstrates that complete scoped accounting can be satisfied with targeted positive evidence, and that withholding the failed operations prevents strong mechanism support. It does not justify a new mandatory all-log condition in the production resolver. Sufficiency, witness scope, and complete measurement accounting remain separate concepts.

## Derived negative controls

The following are controlled edits of the D frozen input, not additional live incidents or independent test cases:

| Controlled missing/mismatched evidence | Existing resolver | Experimental guard |
| --- | --- | --- |
| Old Pod DELETE withheld | AMBIGUOUS | AMBIGUOUS |
| Pre-change baseline withheld | AMBIGUOUS | AMBIGUOUS |
| One log's request identity mismatched | RESOLVED | AMBIGUOUS |
| BackOff event bound to another Pod UID | AMBIGUOUS | AMBIGUOUS |

For every same-input guarded/unguarded pair, the root, supported claims, strong claims, leading witnesses, frontier answers and hypothesis inventory remain unchanged. All five recorded DEV acceptance checks pass. These controls do not estimate False RESOLVED Rate or replace genuinely independent competing-fault validation.

## Capture integrity and preliminary attempts

D uses raw Kubernetes watch API streams starting from recorded list resourceVersions for Pods and ReplicaSets. Only explicit watch ADDED/DELETED events supply their lifecycle authority. Polling bodies supply snapshots/status and never authorize deletion by absence. Analysis rejects watch errors and lifecycle records without a matching watched kind, UID, transition and timestamp. Exact source references are retained locally; this is not content authentication or a production provider read tape.

Earlier artifacts remain intact and do not count as authoritative success:

- A used a bootstrap ConfigMap and immediate readiness checks. It exposed genuine additional explanation/frontier questions in that setup. Its poll-derived tombstones are not accepted for causal verification.
- B moved bootstrap code into the image and delayed initial readiness, but still used poll-derived tombstones. It is not accepted as causal verification.
- C attempted explicit watches with a kubectl flag unsupported by the local client. The capture rejected the failed watches and cleaned up its namespace. No causal result is admitted.
- D uses the raw watch API instead, preserving explicit lifecycle records. It retains all events, including the failed revision's readiness warning. No D evidence was removed to obtain RESOLVED.

The environment changes between A/B/D are DEV harness revisions, not paired RCA rule comparisons. Only profiles/controls within D provide the paired evidence comparison reported above.

## Adapting targeted reads to the active flow

The next implementation should add a bounded request-pair capability to the existing investigation and durable provider boundary:

1. Derive a concrete missing-operation need from a recorded decision blocker or an explanatory deficit. Preserve which purpose it serves. Do not add a decision blocker merely because an explanatory relation is unknown.
2. Select already-recorded log evidence references in the incident/revision scope. Resolve trace/client/parent identities from those records; the policy should not invent trace IDs or issue arbitrary Tempo queries.
3. Reuse available span pairs first. Fetch only missing pairs, deduplicating trace IDs. Start with one bounded read; budget additional reads explicitly.
4. Persist the request descriptor, source log references, incident/episode/cutoff, identities, effective limits, response references and failures through the provider tape. Replay must reproduce the exact read outcome, including 404, truncation and budget exhaustion.
5. Validate returned peer/caller, request identity, direct parent, timestamps, outcomes and provenance. Add the accepted observations through the existing evidence overlay and rerun existing RCA rules. Pair retrieval itself grants no root or exclusion authority.
6. Report which recorded condition or explanation deficit changed. Unavailable pairs remain unknown; known relationships do not establish causal origin or global dependency health.

Default production policy currently permits two tool calls per gap. The four-read DEV helper must not be smuggled through as a free single action or bypass that policy. Total HTTP/byte/time cost must remain explicit. Full 30-member accounting is an explanatory result of this DEV experiment, not a new production RESOLVED prerequisite. First integrate the read capability without changing resolver conditions or registering the DEV accounting guard.

## Artifacts and reproduction

Primary capture: `.local/diagnostics/k8s-rollout-binding-dev-20261014/d/`. Final analysis: `d-analysis-final/summary.json`, per-profile diagnoses/hypotheses, derived-control reports, raw targeted responses and bounded-query diagnostics. No `binding-dev-*` namespaces remain.

Images: base `agentic-sre/binding-dev:85fb474`; bootstrap image `agentic-sre/binding-dev:85fb474-bootstrap-v1`. The latter adds the script's `BOOTSTRAP` payload at `/dev-bootstrap/bootstrap.py` to the base image. Actual pod image IDs are preserved in raw Kubernetes bodies. New image tags were loaded into Kind; existing workload tags were not overwritten.

```sh
.venv/bin/python -m scripts.kubernetes_rollout_binding_dev \
  --image agentic-sre/binding-dev:85fb474-bootstrap-v1 \
  --output .local/diagnostics/k8s-rollout-binding-dev-20261014/new-capture

.venv/bin/python -m scripts.kubernetes_rollout_binding_analysis \
  --capture .local/diagnostics/k8s-rollout-binding-dev-20261014/d \
  --output .local/diagnostics/k8s-rollout-binding-dev-20261014/new-analysis
```

The analysis re-reads historical Tempo data; it is not a fully offline production replay. Analysis creates frozen in-memory source variants and never writes stored production diagnoses. Capture creates and deletes only its owned namespace, verifies its UID at cleanup, and leaves its evidence files for review.
