# M14 — Investigation Metrics and Frozen Baseline

## Run identity

| Field | Value |
| --- | --- |
| Date | 2026-09-23 |
| Evaluated commit | `fe48cc1768ff66735db6ea843a437dbbac58fcaf` |
| Environment | Local repository; ITBench-Lite pinned dataset; clean worktree at prediction time |
| Metric version | `m14.v1` |
| Evaluation class | CLASS 2 — frozen evaluation |
| Model configuration | `gpt-6-luna`, existing bounded `LLMInvestigationPolicy` / Responses API |
| Frozen set | ITBench-Lite TEST, 25 scenario IDs from the sealed manifest |
| Model-call budget | 1 per scenario; 25 total |
| Actual provider calls | 25 (25 scenarios; budget reached exactly) |
| Tool-call budget | 8 per scenario; 24 executed backend reads in total |

The separate CLASS 1 compatibility smoke used one call on Scenario-4 and is recorded in the local roadmap ledger. It is not included in this CLASS 2 baseline.

## Commands

```bash
SRE_LLM_ENABLED=true .venv/bin/agentic-sre investigation-eval \
  --split test --confirm-test \
  --out .local/eval/m14/gpt6-luna \
  --dataset .local/itbench-lite \
  --max-turns 6 --max-model-calls 1 --max-tool-calls 8 \
  --llm --model gpt-6-luna --llm-selection action \
  --api-usage-class 'CLASS 2' --max-total-model-calls 25
.venv/bin/agentic-sre grade-investigation-eval \
  --out .local/eval/m14/gpt6-luna --dataset .local/itbench-lite
```

The prediction command ran on the clean evaluated commit. It wrote all scenario predictions and the prediction seal before the grader command was invoked. The manifest records `ground_truth_read_during_prediction=false`; the grader verifies the prediction seal before loading labels and writes a separately sealed evaluation result. The local run directory is ignored under `.local/` and is retained for reproducibility.

Prediction manifest SHA-256: `629c863d2fb9a25d4afda34d19787e140335882cbd37eec62ab67240e3f0aece`.

Prediction seal file SHA-256: `c06623f8c2655f521e6427296aacb7a38d1455efbf74b2f7d203395e40e69688`.

Evaluation result SHA-256: `fe5a0843276b0de1722cdc9af081dd9bb4a2965bf89d4c473876dd4ef8a049ba`.

## Results

| Metric | GPT-6 Luna baseline | Deterministic M14 comparison |
| --- | ---: | ---: |
| Scenarios | 25 | 25 |
| Model calls | 25 | 0 |
| Tool calls | 24 | 150 |
| Unique observations | 24 | 150 |
| Duplicate reads | 9 (37.5%) | 30 (20.0%) |
| Useful calls | 5 (20.8%) | 150 (100.0%) |
| Novel evidence refs | 7 | 1,367 |
| Calls with novel evidence | 5 (20.8%) | 34 (22.7%) |
| Decision-relevant calls | 5 (20.8%) | 34 (22.7%) |
| Hypothesis changes | 5 | 32 |
| Alternative eliminations | 0 | 1 |
| Gap-state changes | 5 | 34 |
| Resolution transitions | 0 | 1 |
| Outcomes: RECOVERY / STABLE_CORRECT / STABLE_WRONG / HARM | 4 / 2 / 19 / 0 | 8 / 2 / 15 / 0 |
| Invalid/rejected actions | 1 / 1 | 0 / 0 |
| Out-of-policy executions | 0 | 0 |
| Writes / secret access | 0 / 0 | 0 / 0 |
| AMBIGUOUS → RESOLVED | 0 | 0 |
| Wrong RESOLVED actor | 0 | 0 |

The single invalid action was deterministically rejected; its audit records backend status `NOT_EXECUTED`. It is counted in the invalid/rejected metrics and did not become an out-of-policy execution. The CLASS 2 run reached its declared provider-call budget exactly, so the investigation stopped after at most one selected action per scenario. This constrained baseline does not establish that GPT-6 Luna improves over the deterministic policy: its duplicate, useful-call, and decision-relevant rates were worse in this frozen run, while it recorded four recoveries and no harm.

## M14 hard gates

| Gate | Result | Evidence |
| --- | --- | --- |
| G14.1 Versioned metric definitions | PASS | `m14.v1` schema and formal definitions in `packages/evals/investigation_metrics.py` |
| G14.2 Deterministic metric derivation | PASS | Focused metric tests and sealed deterministic/GPT-6 evaluation artifacts |
| G14.3 Historical evaluation remains interpretable | PASS | Historical GPT-5.6 Luna files and claims were not changed or relabeled |
| G14.4 Per-turn attribution | PASS | Persisted action audit; metric unit tests; per-scenario metrics in prediction artifacts |
| G14.5 Zero ground-truth leakage | PASS | Prediction manifest records no label reads; grader verifies prediction seal before loading labels |
| G14.6 Safety violations zero | PASS | Out-of-policy execution, writes, secret access and harm are all zero; one rejected action is separately recorded with `NOT_EXECUTED` backend status |
| G14.7 Sealed current GPT-6 Luna baseline | PASS | Frozen TEST set, evaluated SHA, 25-call cap, 25 actual calls, prediction and evaluation seals |
| G14.8 RCA and agent metrics separated | PASS | Runtime `m14.v1` metrics are produced during prediction; correctness/recovery/harm are post-seal grader outputs |
| G14.9 CI accounting/metric invariant tests | PASS | Focused M14 tests and `make check` (797 passed) |

## Validation executed

- Focused M14 metrics and evaluation tests passed (combined six-test run); focused Ruff, format and mypy checks passed.
- `make check` passed at the evaluated implementation state: Ruff, formatting, mypy over 203 source files, and pytest (797 passed; one existing Starlette deprecation warning).
- Pre-commit checks passed on the implementation commits.
- Deterministic CLASS 0 comparison and CLASS 2 GPT-6 Luna prediction/grading were run on the same 25 frozen TEST scenario IDs.
- The provider compatibility smoke is separately sealed and used one call. CLASS 2 used exactly its predeclared 25 calls; no scenario was rerun.

## Limitations

- One provider action per scenario is intentionally narrow and led to 24 backend reads from 25 model calls because one selected action was rejected before execution.
- There were 19 STABLE_WRONG outcomes, no resolution transitions, and no AMBIGUOUS → RESOLVED transitions. This is a measured limitation; no thresholds or labels were changed.
- The deterministic and LLM modes have different action budgets (the deterministic comparison has up to six turns and 150 reads); this baseline is descriptive and is not a budget-normalized causal claim about model contribution.
- The evaluation measures the bounded ITBench-Lite TEST split, not live Kubernetes behavior or runtime telemetry reliability.

## Reproduction

Use the two commands above with the pinned `.local/itbench-lite` dataset. The evaluator refuses to overwrite an existing sealed output; preserve the original run and use a new output directory for any separately budgeted future evaluation. The grader must run only after the prediction command has created `seal.json`.
