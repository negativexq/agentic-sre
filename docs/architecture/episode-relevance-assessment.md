# Episode Relevance Assessment v1

Implemented as an offline explanatory projection in `packages/evals/episode_relevance.py`. It does not participate in engine admission, ranking, resolution, frontier answers or epistemic digests. Engine version remains 2.18.0.

## Question and scope

For a MANIFESTATION_ONLY claim with DEPENDENCY_ERRORS, distinguish an aggregate finding's event time from its assigned incident onset and the timestamps of cited source records. An assigned onset is assessment context, not evidence that the fault continued into that episode.

The report preserves the recorded unresolved-rival membership. It never removes a historical claim, changes its admission, labels a dependency healthy, or attributes an old error to a later rollout. Old configuration changes and other initiating mechanisms are outside this version's scope.

## Evidence limits

`BEFORE_EPISODE_ONSET` describes time only. An earlier fault may persist or recur. `AT_OR_AFTER_EPISODE_ONSET` establishes neither membership in a bounded episode nor mechanism identity. Neither status establishes causation.

Optional source observations are indexed by evidence ID with timezone-aware `at` timestamps. Missing, invalid or unzoned times are UNASSESSED. The aggregate finding timestamp is never copied onto its cited source records; aggregated findings may summarize observations at different times. The cited IDs may cover only a sample, so this report does not describe every contributing log or full measurement coverage.

Current episode binding, ongoing/intermittent continuation and recovery remain UNASSESSED in v1. The report states the evidence deficits explicitly. Structural dependency paths and matching assigned onsets do not supply missing request/instance/mechanism identity. Sampled successful calls do not prove global health or recovery. Source records are frozen inputs, not independently authenticated by this utility.

## Use

```sh
.venv/bin/python -m scripts.episode_relevance_analysis diagnoses.json \
  --observations source-observations.json --output episode-report.json
```

The source observations argument is optional. JSON, JSONL and diagnosis export wrappers are supported; wrapper revision/run/engine metadata is retained. Input files cannot be overwritten. No provider, replay, database write or resolver is invoked.

## Before any authority change

A separate contract and discriminating positive evidence are required before deciding admission or materiality. Validate pre-onset persistent faults, intermittent failures with successful baseline calls, old initiating configuration, multiple replicas, missing trace/export data, unknown target UID and simultaneous historical/current faults. Keep frozen HOLDOUT23 historical results unchanged; validate any future decision rule on a new independent holdout.

## Development binding contract

The next evidence shape and counterexamples are specified in [episode-binding-evidence-contract.md](episode-binding-evidence-contract.md). Its separate checker validates explicit frozen assertions for current request relationships and same-fault observations. It does not populate v1's UNASSESSED continuation/recovery fields, authenticate telemetry, or change admission/materiality.
