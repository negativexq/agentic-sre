# Rollout/refusal resolution DEV proof and shared-evidence scope guard

2026-10-14. Development experiment only. Engine remains 2.18.0; no production rule, deployment, live Kubernetes injection, original diagnosis rewrite or HOLDOUT change is included.

## Result: positive current evidence already unlocks the existing resolver

A frozen, hand-authored development fixture goes through the real `build_case` and `diagnose_case`. Its journal has a UID-bound Payment Deployment image change, an owned new ReplicaSet/Pod, exact-UID Pod failure and old-revision removal. Baseline calls succeed; current dependency calls refuse, with matching caller SERVER 5xx spans and structured logs. A recorded sole-ready Order Pod supplies existing declared-witness coverage.

With the current failed CLIENT operations omitted, the resolver returns AMBIGUOUS: one supported Payment leader, no strong mechanism witness and two unresolved manifestation claims. Adding exactly those four current CLIENT observations makes the existing failed-rollout witness fire and the existing shared-evidence rule close the manifestations. The result is RESOLVED on the same Payment Deployment. Hypotheses, findings, candidate ranking and supported claim set remain identical between these inputs. No new RCA rule or fabricated support record causes this transition.

This is an engine-level frozen-fixture evidence transition, **not** an actual Kubernetes rollout/holdout or a measured live RESOLVED gain. The earlier real OTLP transport experiment established that request identities can survive collection; its loopback spans are not transplanted into this fixture or represented as UID-bound Kubernetes evidence.

## Counterexample uncovered by the same experiment

`dependency_findings` summarizes all named errors with `at=max(event times)` and cites only the first four IDs while retaining the full error count. When a historical refusal is combined with new errors, that aggregate can be grouped with the current image-change claim. Its old log ID is copied into both the strong source and Service manifestation. §24 then sees all **cited** local facts shared and closes the Service claim.

In this fixture the historical log predates the rollout and is not one of the operations in its witness. Copying its ID into both claims cannot establish that the later rollout accounts for it. This demonstrates an explanation/aggregation scope problem; it does **not** establish that Payment is the wrong root cause or that every old observation should block resolution. Historical relevance and materiality still need their own contract.

## Narrow DEV guard

`packages/evals/rollout_refusal_accounting.py` evaluates only Service MANIFESTATION_ONLY claims with local DEPENDENCY_ERRORS. It requires one existing strong supported source and checks:

- The source's already-fired failed-rollout witness, exact actor instance and episode context.
- A declared caller→Service relationship, the Service selecting the witnessed executing Pod, and that Pod's workload matching the source actor.
- The producer's complete **frozen log group**, reconstructed with its existing matching rules and checked against the aggregate count/cited IDs. First-four citations alone are insufficient.
- ConnectionRefusedError specifically; each log's client span must already belong to the existing strong witness.
- Corroborated trace/client/parent/request identity, parent 5xx, error/peer/method agreement and nested timestamps inside the recorded execution interval.

Unaccounted old/unknown/uncaptured logs, other local findings, an independently supported target, competing supported sources or missing mechanism/operation scope prevent eligibility. Timeouts are outside this proposed guard's narrow scope; rejection is not proof they are unrelated to rollout. No global health, recovery, continuous-fault end, changed-field consumption or sole-cause claim is created.

The helper is evaluated on frozen/in-memory case sources, with source authenticity unverified. It is not an authenticated database/production adapter or a complete same-fault/materiality test. It grants no new authority.

## Paired counterfactual results

`scripts/rollout_resolution_dev_proof.py` temporarily filters §24 Service-claim explanation reuse using the audit result inside a bounded patch context. It restores the resolver function on exit; the fixture and hypotheses are unchanged. Only the existing shared-explanation consequence can be withheld. No new claim, root support, witness or frontier answer is injected.

| Frozen fixture | Production 2.18.0 | DEV guard |
|---|---|---|
| Current operation evidence missing | AMBIGUOUS | AMBIGUOUS |
| Complete current refusals | RESOLVED | RESOLVED |
| Historical refusal mixed into current aggregate | RESOLVED | AMBIGUOUS |
| Failures already observed in baseline | AMBIGUOUS | AMBIGUOUS |
| Another supported initiating change | AMBIGUOUS | AMBIGUOUS |
| A log's request identity does not match | RESOLVED | AMBIGUOUS |
| Timeout, outside proposed scope | RESOLVED | AMBIGUOUS |

Within each fixed-input production/guard pair, the root, supported and strong sets, leader witnesses, hypothesis inventory and frontier answers remain unchanged. The guard's only effect is withholding a Service claim's positive explanation. Tests also cover a missing aggregate member and an unbound fifth log omitted from the first-four citation sample.

The competing-change case has a second **supported possible** cause; it is not a separately execution-verified second root cause. Repeated baseline failures likewise do not prove uninterrupted persistence. These controls preserve uncertainty without inventing stronger ground truth.

## Run and artifacts

```sh
.venv/bin/python -m scripts.rollout_resolution_dev_proof \
  --output .local/diagnostics/rollout-resolution-dev/new-proof.json
```

Fixture: `tests/unit/evals/fixtures/current_refusal_rollout.json` (explicit FROZEN_DEVELOPMENT_FIXTURE_NOT_LIVE_KUBERNETES marker).

Recorded output: `.local/diagnostics/rollout-resolution-dev/proof-v2.json`. It labels every row FROZEN_DEVELOPMENT_FIXTURE_COUNTERFACTUAL and records all paired invariants. No replay version gate is weakened or old HOLDOUT run recalculated.

## Gate before production changes

The main engineering finding is that current positive execution evidence can already produce RESOLVED. Do not add a new permissive explanation rule to inflate that count. First specify aggregate observation/episode scope and how §24 may account for facts represented more than once. Preserve historical facts as context; do not equate an unverified old continuation with either exclusion or a newly verified rival.

The current guard is intentionally conservative and would withhold previously allowed closures when IDs or full log-group bindings are absent. It has not been calibrated against the product's bounded trace budget and must not be installed as a mandatory rule based on this fixture. Validate missing identity, sampled/partial captures, genuine execution-verified competing causes and old initiating faults on independent development data; freeze acceptance conditions before a new HOLDOUT. Actual root-cause correctness, false RESOLVED/exclusion and throughput/coverage tradeoffs remain unmeasured here.
