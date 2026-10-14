# Request-pair acquisition in the active investigation graph

Engine **2.19.0** integrates exact log-ID request-pair acquisition into the existing `runtime_traces`
action. The standalone DEV probe is no longer the only execution path. No new root-support,
exclusion/frontier or RESOLVED rule is registered, and the experimental full-log guard remains DEV-only.
The code is implemented on the branch; the shared production/lab control plane has not been deployed
or restarted with it.

## Active path

A dependency-health need can authorize the observed caller workload when its existing dependency-error
finding cites a correlated log for the declared peer. Candidates copy at most eight missing-operation
log evidence IDs into `InvestigationQuery.source_log_ids`. They preserve the existing hypothesis
question/discriminator rather than treating the physical caller target as an initiating claim.

The action passes the existing exact-candidate, authorization and budget gates. `RuntimeTracesTool`
resolves source IDs from visible case evidence, checks caller/peer/window scope, and invokes the durable
provider boundary. IDs absent from the incident view cannot cause a provider lookup. The policy never
supplies arbitrary trace IDs or TraceQL.

The provider reuses valid cached pairs or directly fetches the explicit trace. It retains only validated
client/parent pairs, with namespace/workload, identity, peer, outcome and timing agreement. Only these raw
records enter the existing evidence overlay. Existing normalization and resolver rules run again.

Every read is taped as `tempo_request_pairs`; the descriptor includes the effective query, source IDs,
source-body hash and relevant cache-body hash. Per-log unknown/fetch outcomes are part of the result.
Replay uses those rows without contacting Tempo. Changed input hashes or read order diverge. The tool
revalidates returned pairs and rejects unsupported provider corroboration before admitting evidence.

The action is still `runtime_traces`: no extra budget is created. Maximum eight trace fetches and 32
retained spans per action; default maximum two actions per gap remains unchanged. Existing general
search is preserved when pair support or usable correlated logs is absent. Legacy queries omit the new
field when empty. The strict action wire schema exposes a nullable `source_log_ids` field and accepts
historical local DTOs that omitted it.

## Real Kubernetes DEV evidence, actual graph, strict provider replay

Input is the immutable authoritative D capture from the earlier isolated Kubernetes failed-rollout DEV
experiment (`binding-dev-04edf7a6`). Its explicit Pod/ReplicaSet watch and UID provenance are checked by
the shared capture loader. This measurement creates no new workload or fault. The symptom episode is
still defined by the independent driver, not Prometheus. SQLite and in-memory event publication in the
original workload limit the experiment to the Order–Payment HTTP path.

The controlled seed withholds the 30 current failed CLIENT spans and retains the already-captured
baseline, parent/DB spans, objects, events and logs. The initial leading Payment deployment is plausible
but lacks its strong execution/effect witness; the diagnosis is AMBIGUOUS.

One exact candidate action is selected by a scripted policy. It runs the actual investigation graph:
authorize → observe → tape → normalize/evidence overlay → rebuild → existing resolver.

| Observation | Result |
| --- | --- |
| Initial decision | AMBIGUOUS |
| Final decision | RESOLVED |
| Root | Deployment/payment-service, same actual instance |
| Tool actions | 1 |
| HTTP trace fetch attempts | 8 |
| New raw evidence records | 8 failed CLIENT spans; parents were already visible |
| Provider tape reads | 1 structured batch containing the eight lookups |
| Rejected actions | 0 |
| Replay diagnosis epistemic digest | Identical |
| Replay observations | Identical |
| Replay provider tape | Fully consumed; no provider call |

The observation's scope reports 30 visible eligible logs, eight selected, and 22 not selected for this
read. It explicitly leaves symptom measurement accounting UNASSESSED. The existing mechanism rule needs
sufficient operation evidence and therefore resolves after these eight calls; all-30 accounting is not
introduced as a resolution prerequisite.

A second pass used the application's default `DeterministicIntentPolicy` with no supplied action.
It autonomously selected the request-pair candidate and reached the same Payment RESOLVED result:
four total tool actions, one provider pair batch, eight trace fetches, zero rejected actions. Both the
diagnosis digest and observations matched strict replay. Artifacts: `c-auto/`. Thus the capability works
through the default selector as well as a scripted action. The alternate observation selector still
stopped with NO_PROGRESS in the small fixture check; no ranking/stopping policy was retuned to force
selection. Availability of a targeted candidate does not guarantee selection by every policy.

This does **not** establish an accuracy improvement over the original general Kubernetes query: that
query also resolved this simple case. It establishes that the targeted operation is executable within
the current action budget, carries source provenance, reaches the existing witness/resolver path and
replays exactly. Policy preference across competing cases, independent competing-fault
validation and a new frozen HOLDOUT remain separate work.

Final artifacts: `.local/diagnostics/active-request-pairs-dev-20261014/b/` (`summary.json`, live/replay
results, `provider-tape.sqlite`, raw taped read export and protocol). Pilot `a/` predates the engine-version
bump; use `b/` for the 2.19.0 measurement. The SQLite database is newly created measurement storage and
contains no fabricated production Diagnosis row. This is strict provider-tape plus graph replay over a
frozen source, not `ReplaySource.from_run` of an existing production incident.

## Validation and reproduction

The non-PostgreSQL suite passed **3083 tests**. Additional focused tests passed after the final admission-integrity check. A separate throwaway PostgreSQL container validated the
new structured pair result and source-descriptor tape round trip. Focused tests additionally exercise
full graph replay, changed source/cache divergence, 404/UNKNOWN, foreign namespaces, wrong request IDs,
partial span budget, cache reuse, DTO compatibility and rejection of false provider corroboration.

```sh
TEMPO_URL=http://127.0.0.1:13200 \
.venv/bin/python -m scripts.measure_active_request_pair_investigation \
  --capture .local/diagnostics/k8s-rollout-binding-dev-20261014/d \
  --output .local/diagnostics/active-request-pairs-dev-20261014/new-run \
  --policy deterministic_intent
```

The first pass re-reads historical Tempo data, writes a new local provider tape and preserves the graph
results. The replay pass uses only that tape and the same frozen source. Existing capture files,
HOLDOUT protocols and stored diagnoses are untouched.
