# Live smoke preregistration contract repair

## Historical SMOKE-001

`ITB-CONTROL-LIVE-SMOKE-001` remains immutable. Its classification is
`LIVE_SMOKE_PREFLIGHT_FAILED`; it constructed no provider, made zero calls,
and left its dedicated ledger at `8` capacity, `0` used, `8` remaining.

The confirmed root cause was:

```text
PREREGISTRATION_PREFLIGHT_CONTRACT_DRIFT
```

The failed preflight correctly stopped before network. Its artifacts were not
rewritten.

## Canonical contract

Future manifests use the typed `ITBenchLiveSmokeManifestV1` model from
`packages/evals/itbench/live_smoke_contract.py`. The builder and validator share
the same model; historical spellings are not aliases.

Canonical policy fields are:

```text
provider = openai
model = gpt-5.6-luna
reasoning_effort = none
provider_retries = 0
required_budget = 8
max_model_calls = 8
max_agent_turns = 8
max_semantic_actions = 8
max_wall_time_seconds = 180
max_consecutive_rejected_actions = 2
judge_disabled = true
rerun_policy = NEVER
```

The semantic identity field is exactly:

```text
semantic_capability_policy_hash
```

The dataset revision and offline readiness status are required and validated.
The fixture is fixed to the synthetic, non-official `Scenario-999` snapshot
with `ground_truth_loaded = false`.

## Ownership and validation

The builder collects Git/content identity and produces the typed manifest.
Preflight reloads the same type, validates source hashes, dataset, fixture,
model policy, limits, retry/judge/rerun policy, and fresh budget before any
future provider construction. The return value explicitly reports
`provider_constructed = false` for the offline proof.

The provider retry owner remains the eventual runner’s explicit
`OpenAIProvider(max_retry=0)` construction; the manifest and preflight require
the corresponding zero value. There is no judge execution path for this
single-smoke contract.

## Offline proof

The generated manifest round-trips through JSON and passes synthetic future
preflight without credentials or a provider. Negative tests cover missing and
wrong canonical fields, the old capability-field spelling, dataset/readiness
drift, model policy, provider/retry/judge/rerun policy, limit drift, source hash
drift, budget mismatch, and non-fresh budgets.

`SMOKE-002` was not generated as an executable preregistration and was not run.
The correct future sequence is: source repair commit → CI → freeze source SHA
→ generate preregistration without moving HEAD → generate fresh ledger →
preflight → separately approved live execution → commit artifacts afterward.

```text
live agent calls: 0
OpenAI outbound attempts: 0
Luna calls: 0
judge calls: 0
official benchmark scenarios: 0
live smoke scenarios: 0
```
