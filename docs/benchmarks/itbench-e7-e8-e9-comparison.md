# ITBench E7 → E8 → E9 comparison

Same 35-scenario comparison set. Luna scores use the pinned evaluator and
`gpt-5.6-luna`; local scores use fixed denominator 35.

| Metric | E7 | E8 | E9 |
|---|---:|---:|---:|
| Luna ROOT_CAUSE_ENTITY F1 | 0.171717 | 0.028571 | 0.000000 |
| Local fixed-35 macro F1 | 0.085714 | 0.000000 | 0.000000 |
| Diagnosis coverage | 17/35 | 7/35 | 0/35 |
| Agent calls | 171 | 145 | 293 |
| Input tokens | ~2,244,100 | 918,575 | 1,239,583 |
| Semantic tool executions | 369 | 325 | 124 |
| Wall duration | ~1,156 s | 1,045.0 s | 805.7 s |

E9’s context and semantic-operation infrastructure was compact and passed
offline qualification, but official reliability and RCA quality regressed:
zero official submissions were made. E9 is therefore not Pareto-improved.

## Context and memory

| Measurement | E7 | E8 | E9 |
|---|---:|---:|---:|
| Scenario-105 first-turn context | ~65,135 chars | 10,590 chars | 12,200 chars |
| E9 first-turn min/median/p95/max | — | — | 10,190 / 11,676 / 12,179 / 12,338 |
| E9 later-turn median/p95/max | — | — | 14,027 / 15,901 / 19,476 |
| E9 events/evidence retained | — | — | 1,252 / 525 |

## Verdict

```text
QUALITY_DOWN_EFFICIENCY_DOWN
ITB_E9_COMPLETE_REGRESSED
```

Detailed scenario results are in [itbench-e9-final.md](itbench-e9-final.md).
