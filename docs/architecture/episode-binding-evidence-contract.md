# Episode binding evidence contract — development v1

2026-10-14. Proposed evidence shape with an executable development checker; **no M21 admission or materiality change**. Existing engine version 2.18.0 and HOLDOUT23 remain unchanged. This contract is not an approved production authority rule or an independent holdout result.

## Why this contract is separate

The episode relevance report distinguishes old observations from assigned episode context. Old dependency errors can be persistent, intermittent, unrelated to the current fault, or part of a simultaneous fault. Age, structural adjacency and a few successful requests cannot decide among these cases.

The next evidence collection task is to connect a failing dependency operation to a failing symptom operation, then separately ask whether an older observation identifies the same fault. These relations do not establish causal origin, exclusivity or uninterrupted persistence. A child operation may fail without causing the parent's failure; both may be affected by a third cause.

## Narrow positive relations

| Relation | Required recorded assertions | Result and limits |
|---|---|---|
| Current request relation | Distinct failed dependency and symptom operations; same request ID; explicit direct parent-operation link; expected caller and target identities; same incident; both event times in an explicit bounded episode; source-read provenance | `request_relation=RECORDED`; says these recorded failures share a direct request relationship. Does not say the dependency caused the symptom. |
| Same fault at two observations | Current request relation above; a distinct failed historical dependency attempt before episode start; same caller and target; exact same target UID, mechanism fingerprint and fault-generation ID | `same_fault_at_two_observations=RECORDED`; says the supplied records assert the same fault identity at two endpoints. Does not prove uninterrupted continuation across the gap. |

The second relation can use a record captured under a prior incident: the fault identity may outlive an incident record. Both current operations must belong to the current incident. A fault-generation ID must distinguish different faults on the same instance; an instance UID alone is insufficient.

Missing or mismatching facts produce UNASSESSED plus concrete deficits, never an exclusion. Invalid schema or absent provenance fails input validation. The checker requires timezone-aware event times and a valid interval. It does not replace event time with capture/arrival time or assigned onset. Direct-parent binding is intentionally narrow; nested calls and async messages require their own validated relationship chain and are UNASSESSED here.

## Source and trust boundary

`packages/evals/episode_binding.py` checks **consistency of supplied frozen assertions**. It does not fetch or authenticate evidence, verify an ID against a source database, or derive a mechanism/fault identity from log text. The output explicitly carries `EXPLANATORY_ONLY`. The CLI labels it `DEVELOPMENT_CONTRACT_CHECK`, with source authenticity `NOT_VALIDATED`.

The proposed dependency-attempt schema is not today's `LogRecord`. Current caller logs lack request/operation, target UID and fault-generation identity. No adapter invents those fields from Service selection, ownership, proximity, message similarity, or a successful baseline. A synthetic passing bundle proves the contract can be checked, not that real telemetry establishes this relation.

Before production use, each asserted identity/link must be verified against frozen source facts; the source namespace and incident/run boundary must be retained; fault-generation and mechanism assertions need mechanism-specific authority and recorded provenance. This checker alone cannot grant such authority. Neither a successful validation nor an absent bundle changes hypothesis membership, support, frontier, ranking, witnesses, digests or RESOLVED.

## Counterexamples and required behavior

| Case | Expected behavior |
|---|---|
| Failure starts before onset and recurs with exact fault identity | Preserve positive endpoint recurrence; leave continuous persistence and origin UNASSESSED. |
| Intermittent fault with successful calls between failures | Success does not prove recovery or remove the recurring fault. |
| Old config change causes new failures | Initiating config remains outside the manifestation report's scope; no age cutoff. |
| Same Service name, different replica or unknown UID | Current request relationship may be recorded; same-fault relation remains UNASSESSED. |
| New rollout fault alongside an older fault | Different fault generations must remain separate; do not attribute old errors to the new rollout. |
| Missing trace or request/parent link | UNASSESSED with missing-link deficit; never infer non-occurrence. |
| Dependency fails but parent succeeds | This failed-symptom contract is not met; no causal-origin conclusion. |
| Same request label but different target/caller/incident | UNASSESSED with identity mismatch. |
| Timestamp outside bounded episode or unzoned | UNASSESSED; no clock correction inferred. |

Executable counterexamples are in `tests/unit/evals/test_episode_binding.py`; old initiating config protection is also exercised by `test_episode_relevance.py`. These are development tests, not demonstrations of production authenticity or the ability to safely dismiss historical rivals.

## Use

```sh
.venv/bin/python -m scripts.episode_binding_analysis frozen-bundles.json \
  --output binding-contract-report.json
```

Input is a single `EpisodeBindingBundle` object or a list. The output cannot overwrite input. All bundles are validated before output is written. No database, provider, planner or resolver is invoked. Empty optional identities cannot yield a positive recurrence result.

## Next gate toward decision authority

1. Record request/operation and dependency target identity at source; retain event and capture time separately. Prefer existing trustworthy correlation IDs over invented adapter labels.
2. Choose one mechanism for which fault-generation identity can actually be verified; define its authority from source facts. UID and error type are not enough.
3. Evaluate real frozen development cases, including persistent, intermittent, competing, truncated and unknown-identity cases. Measure unsupported-positive links as well as missing coverage.
4. Separately specify any admission/materiality policy: an unverified continuation is not proof the dependency is irrelevant. Keep historical context recoverable and reconsider it when new evidence appears.
5. Freeze that rule and acceptance criteria before a new independent holdout. Report false exclusion and false RESOLVED separately. No decision-authority rule is included in this delivery.
