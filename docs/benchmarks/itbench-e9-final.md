# ITBench E9 final report

## A. E8 historical closure

E8 remains immutable. Its frozen result was local fixed-35 macro F1 `0`, Luna
`ROOT_CAUSE_ENTITY` F1 `0.02857142857142857`, diagnosis coverage `7/35`, and
21 `MODEL_DECISION_INVALID` outcomes. E8 prediction artifacts were not
regenerated or rewritten during E9. The E8 forensic record is preserved in
[itbench-e8-invalid-forensics.md](itbench-e8-invalid-forensics.md).

## B. E9 architecture and changed implementation

E9 kept the single Luna agent (`gpt-5.6-luna`, reasoning `none`) and moved
workflow ownership into the harness:

- Protocol V5: one bounded action, short rationale, runtime-owned state and
  provenance, and scenario-local `C###` entity handles.
- Soft FSM: `OBSERVE`, `HYPOTHESIZE`, `VERIFY`, `REVISE`, `CONCLUDE`.
- Safe action rejection with bounded recovery; security, leakage, corruption,
  and infrastructure failures remain fatal.
- Append-only event log with deterministic CaseState replay.
- Context V4 planner with alert digest, candidate shortlist, relevant topology,
  phase-specific actions, and bounded evidence.
- Semantic operations for incident overview, alerts, topology, changes,
  events, entity context, metrics, traces, replica comparison, and temporal
  verification.
- Immutable snapshot indexes/caches and separate semantic-action/backend-read
  accounting.

No model upgrade, reasoning upgrade, multi-agent system, vector database,
write/remediation tool, or cross-case memory was introduced.

Relevant implementation is in `packages/evals/itbench/e9_*.py`, the provider
adapter in `packages/provider/openai.py`, the runner in
`scripts/itbench_e9_execute.py`, the checkpointed judge in
`scripts/itbench_e9_judge.py`, and tests in
`tests/unit/itbench/test_e9_harness.py`.

## C. Qualification and smoke

Zero-network qualification passed for all 35 snapshots with zero OpenAI calls,
zero judge calls, and no ground truth in runtime inference. Internal A0–A6
ablations passed as structural engineering proxies only; no official judge
score was used for selection.

`ITB-E9-SMOKE-003` passed with real Luna transport, 12/12 smoke calls, 7
semantic actions, 7 evidence items, 3 rejected actions recovered, preserved
state/query history, artifact reload, and a valid `SUBMIT` terminal. Historical
Smoke-001 and Smoke-002 remain immutable.

## D. Frozen identity

| Field | Value |
|---|---|
| Execution / experiment | `ITB-E9` / `itbench-lite-sre-external-eval-v9` |
| Runtime source | `96c3168e1f4990794032abf38a0de5c139f2b5fc` |
| Dataset | `d0916b08ba421ce5e672e9ad68aa947d938dfef0` |
| SRE | `v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7` |
| Prompt | `itbench_sre_investigator_v7`, SHA `d191632d07e49b8cce9609c9396b57a573be35eddbb53d0b9fb0ebc03c9ba2c7` |
| Protocol | `itbench_investigation_decision_v5`, SHA `1ea2625a72e3f0872d0cf575e59770fb15b649d5505f5902f0cfeeebb0acb9be` |
| Context | `itbench_external_context_v4`, SHA `25b8de4ccf6e7d5630b1d1d02d25dd427fca5588` |
| FSM / memory | `fbe87ef449f1a4fd454194efebed9b5b8daded1f` / `955b776df0bd716f09730326405f48553fcec8bd` |
| Semantic registry | `52ce785190912757752e8ff7966ee0d3c03c96ba` |
| Candidate retrieval | `85b1ff7d2f0c551e9782f5d48de0ef954cfc538a` |
| Model / reasoning | `gpt-5.6-luna` / `none` |
| Limits | 12 model steps, 24 semantic actions, 12 turns, 240 seconds, max 2 consecutive rejections |
| Judge | OpenAI Luna; evaluator `14f026fc9cc348c4ecec5ab32714de954c95c1b1`; compat `itbench_luna_judge_compat_v1`; temperature 1; retries 0; one attempt/case |

The official order was the frozen 35-scenario order from E7/E8, exactly once.

## E. Official 35×1 results

| Scenario | Terminal | Calls | Input tokens | Semantic actions | Predicted | TP | F1 |
|---|---|---:|---:|---:|---:|---:|---:|
| Scenario-1 | PROTOCOL_STALLED | 10 | 43738 | 4 | 0 | 0 | 0 |
| Scenario-2 | MODEL_STEP_LIMIT | 12 | 53473 | 4 | 0 | 0 | 0 |
| Scenario-4 | PROTOCOL_STALLED | 7 | 27436 | 3 | 0 | 0 | 0 |
| Scenario-5 | PROTOCOL_STALLED | 7 | 29213 | 3 | 0 | 0 | 0 |
| Scenario-6 | PROTOCOL_STALLED | 10 | 42352 | 4 | 0 | 0 | 0 |
| Scenario-7 | PROTOCOL_STALLED | 9 | 38634 | 3 | 0 | 0 | 0 |
| Scenario-8 | PROTOCOL_STALLED | 10 | 41068 | 4 | 0 | 0 | 0 |
| Scenario-9 | PROTOCOL_STALLED | 10 | 41056 | 4 | 0 | 0 | 0 |
| Scenario-11 | PROTOCOL_STALLED | 10 | 36989 | 4 | 0 | 0 | 0 |
| Scenario-12 | PROTOCOL_STALLED | 8 | 30175 | 3 | 0 | 0 | 0 |
| Scenario-13 | PROTOCOL_STALLED | 6 | 25099 | 3 | 0 | 0 | 0 |
| Scenario-14 | PROTOCOL_STALLED | 9 | 37266 | 4 | 0 | 0 | 0 |
| Scenario-15 | PROTOCOL_STALLED | 8 | 34852 | 4 | 0 | 0 | 0 |
| Scenario-16 | PROTOCOL_STALLED | 11 | 49699 | 4 | 0 | 0 | 0 |
| Scenario-17 | PROTOCOL_STALLED | 9 | 36936 | 3 | 0 | 0 | 0 |
| Scenario-18 | PROTOCOL_STALLED | 7 | 29634 | 3 | 0 | 0 | 0 |
| Scenario-19 | PROTOCOL_STALLED | 8 | 35210 | 4 | 0 | 0 | 0 |
| Scenario-20 | PROTOCOL_STALLED | 7 | 28249 | 3 | 0 | 0 | 0 |
| Scenario-21 | PROTOCOL_STALLED | 8 | 33674 | 3 | 0 | 0 | 0 |
| Scenario-22 | PROTOCOL_STALLED | 5 | 19454 | 1 | 0 | 0 | 0 |
| Scenario-23 | PROTOCOL_STALLED | 9 | 39809 | 4 | 0 | 0 | 0 |
| Scenario-24 | PROTOCOL_STALLED | 10 | 45330 | 4 | 0 | 0 | 0 |
| Scenario-25 | PROTOCOL_STALLED | 6 | 23677 | 2 | 0 | 0 | 0 |
| Scenario-29 | PROTOCOL_STALLED | 8 | 34828 | 4 | 0 | 0 | 0 |
| Scenario-31 | PROTOCOL_STALLED | 6 | 23318 | 3 | 0 | 0 | 0 |
| Scenario-33 | MODEL_STEP_LIMIT | 12 | 56930 | 9 | 0 | 0 | 0 |
| Scenario-34 | PROTOCOL_STALLED | 8 | 35155 | 4 | 0 | 0 | 0 |
| Scenario-35 | PROTOCOL_STALLED | 7 | 29286 | 3 | 0 | 0 | 0 |
| Scenario-38 | PROTOCOL_STALLED | 9 | 35013 | 4 | 0 | 0 | 0 |
| Scenario-80 | PROTOCOL_STALLED | 7 | 29779 | 3 | 0 | 0 | 0 |
| Scenario-81 | PROTOCOL_STALLED | 7 | 29381 | 3 | 0 | 0 | 0 |
| Scenario-83 | PROTOCOL_STALLED | 8 | 35612 | 4 | 0 | 0 | 0 |
| Scenario-91 | PROTOCOL_STALLED | 6 | 23743 | 2 | 0 | 0 | 0 |
| Scenario-102 | STOP | 12 | 54109 | 4 | 0 | 0 | 0 |
| Scenario-105 | PROTOCOL_STALLED | 7 | 29406 | 3 | 0 | 0 | 0 |

All 35 predictions were atomically persisted/reloaded and graded only after
prediction freeze. Reruns: zero.

## F. Reliability and quality

| Terminal | Count |
|---|---:|
| SUBMIT | 0 |
| STOP | 1 |
| PROTOCOL_STALLED | 32 |
| MODEL_STEP_LIMIT | 2 |
| SEMANTIC_ACTION_LIMIT | 0 |
| WALL_TIME_LIMIT | 0 |

Diagnosis coverage was `0/35`. Local fixed-35 metrics were macro P/R/F1
`0/0/0`, micro P/R/F1 `0/0/0`, and entity hits `0/35`. There were 167 action
rejections and 59 recovered rejections. No case reached a final submission.

## G. Official Luna judge

The checkpointed evaluator made 35 one-scenario Luna calls, persisted each
result atomically, and aggregated offline:

```text
ROOT_CAUSE_ENTITY mean F1 = 0.0
denominator = 35
judge = gpt-5.6-luna
provider = openai
evaluator = 14f026fc9cc348c4ecec5ab32714de954c95c1b1
compatibility = itbench_luna_judge_compat_v1
temperature = 1
provider retries = 0
attempts per case = 1
judge calls = 35
```

## H. Efficiency and context

| Metric | E9 |
|---|---:|
| Agent calls | 293 |
| Calls/scenario | 8.37 |
| Input/output tokens | 1,239,583 / 25,578 |
| Semantic actions requested/executed | 124 / 124 |
| Provider latency | 638,781 ms |
| Wall duration | 805,748 ms |
| Non-provider duration | 166,967 ms |
| Source scans / records scanned | 4,081 / 1,929,660 |
| Cache hits / misses | 365 / 36 |
| Events / evidence retained | 1,252 / 525 |

First-turn context across 35 scenarios: min `10,190`, median `11,676`, p95
`12,179`, max `12,338` chars. Later turns: median `14,027`, p95 `15,901`,
max `19,476`. Scenario-105 first turn was `12,200` chars versus E7’s
approximately `65,135` chars. Since no diagnosis was submitted, final-citation
count was zero; this does not mean the 525 produced evidence items were absent.

## I. E7 → E8 → E9

| Metric | E7 | E8 | E9 |
|---|---:|---:|---:|
| Luna official RCA F1 | 0.171717 | 0.028571 | 0.000000 |
| Local fixed-35 macro F1 | 0.085714 | 0.000000 | 0.000000 |
| Diagnosis coverage | 17/35 | 7/35 | 0/35 |
| Agent calls | 171 | 145 | 293 |
| Input tokens | ~2,244,100 | 918,575 | 1,239,583 |
| Semantic executions | 369 | 325 | 124 |
| Wall duration | ~1,156 s | 1,045.0 s | 805.7 s |

E9 reduced context and semantic-operation volume, but valid completion fell to
zero and agent calls increased over E8. The Pareto verdict is:

```text
QUALITY_DOWN_EFFICIENCY_DOWN
```

The remaining official failures are primarily model–harness interaction:
trajectories did not progress from observation to a supported hypothesis and
then to conclusion before bounded rejection/step limits. This is a frozen
post-hoc diagnosis, not a reason to rerun or tune E9.

## J. Safety, validity, and Git

```text
GT exposure during inference = 0
cross-scenario evidence = 0
fabricated evidence accepted = 0
writes/remediation = 0
shell = 0
arbitrary filesystem access = 0
SQL = 0
arbitrary PromQL = 0
```

The official run is valid: exact 35×1 order, one trial, zero reruns, durable
scenario checkpoints, separate agent/judge ledgers, and no artifact or ledger
corruption. E9 was not an untouched held-out generalization experiment.

Relevant pushed commits are `6494d9f`, `41dfb3e`, `96c3168`, `4b442bf`,
`817d100`, and `80915ba`. Unrelated A1 worktree changes were not staged. The
final report/result commit and final CI run are the last delivery actions.

## K. Final classification

```text
ITB_E9_COMPLETE_REGRESSED
READY_FOR_ITBENCH_E9_FINAL_RESULT_REVIEW
```

No E10 was started.
