# ITBench judge-model audit

## Policy

The repository policy is explicit and fail-closed:

| purpose | provider | permitted model | reasoning |
|---|---|---|---|
| AGENT | OpenAI | `gpt-5.6-luna` | `none` |
| JUDGE | OpenAI | `gpt-5.6-luna` | not applicable |
| SMOKE | OpenAI | `gpt-5.6-luna` | `none` |

No third-party model default is accepted.  `JUDGE_MODEL` must be present and
must equal `gpt-5.6-luna` before the pinned evaluator process is started.

## Historical official evaluator

The following scores were produced by the pinned evaluator revision
`14f026fc9cc348c4ecec5ab32714de954c95c1b1`:

| Execution | Official ROOT_CAUSE_ENTITY mean F1 | Judge model | Provider | Evidence | Classification |
|---|---:|---|---|---|---|
| E4 | 0.22222 | `gpt-4-turbo` | OpenAI-compatible | repository evaluation record; upstream initialization log; omitted `JUDGE_MODEL` | `VERIFIED_NON_LUNA_JUDGE` |
| E5 | 0.00000 | `gpt-4-turbo` | OpenAI-compatible | same pinned command path and upstream default; no `JUDGE_MODEL` override | `VERIFIED_NON_LUNA_JUDGE` |
| E6 | 0.76190 | `gpt-4-turbo` | OpenAI-compatible | same pinned command path and upstream default; no `JUDGE_MODEL` override | `VERIFIED_NON_LUNA_JUDGE` |

The scores are not rewritten.  The repository report
`docs/benchmarks/itbench-local-vs-official-eval.md` records the historical
judge as `gpt-4-turbo`.  The pinned upstream source at evaluator revision
`14f026fc9cc348c4ecec5ab32714de954c95c1b1` contains:

```python
return os.environ.get("JUDGE_MODEL", "gpt-4-turbo")
```

The source file hash observed during the audit was
`cedb06761fb1d7397782032deef254430acb8665ec90abf3423fd602e86f8499`.
The historical launch omitted `JUDGE_MODEL`; the upstream initialization
reported `gpt-4-turbo`.  Therefore GPT-4 Turbo use is **CONFIRMED** for E4,
E5, and E6 official evaluation.  These are retained as
`HISTORICAL_NON_STANDARD_JUDGE`, not relabeled as Luna measurements.

The historical JSON result files do not durably contain a judge-model field;
that absence is itself recorded here rather than filled with an assumption.
Historical agent artifacts do contain `gpt-5.6-luna`, which identifies the
agent execution and does not identify the separate evaluator judge.

## Accounting reconstruction

| Execution | Agent calls | Judge calls | Agent token totals | Judge token totals | Models |
|---|---:|---:|---|---|---|
| E4 | 133 charged in the E4 agent ledger | not durably recorded | present in per-trial artifacts | NOT DURABLY RECORDED | agent Luna; judge GPT-4 Turbo |
| E5 | 74 charged in the E5 agent ledger | not durably recorded | present in per-trial artifacts | NOT DURABLY RECORDED | agent Luna; judge GPT-4 Turbo |
| E6 | 180 charged in the E6 agent ledger | not durably recorded | present in per-trial artifacts | NOT DURABLY RECORDED | agent Luna; judge GPT-4 Turbo |

Agent and judge accounting were not merged.  The new persistent judge ledger
`.local/itbench-judge-luna-budget.json` is separate from E1–E7 agent ledgers.
Its initial cap is 141 calls: one future qualification case, up to 35 cases
for each of E4/E5/E6 Luna rescoring, and up to 35 E7 evaluator cases.  It is
currently unused.

## Future launch order

The first paid operation after credit recovery is exactly one frozen
historical-case Luna judge smoke.  Only after the returned metadata and the
artifact identity confirm Luna may historical rescoring proceed.  The guarded
launcher requires an explicit per-operation cap and reserves its ledger before
starting the upstream evaluator.  Missing or non-Luna `JUDGE_MODEL` fails
before evaluator import/client construction with
`JUDGE_MODEL_NOT_EXPLICITLY_APPROVED`.

API keys and authorization headers are never persisted; only the safe endpoint
hostname is allowed in metadata.
