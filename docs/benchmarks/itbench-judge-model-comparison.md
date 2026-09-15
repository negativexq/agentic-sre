# ITBench judge-model comparison

This report compares scores on immutable E4, E5, and E6 predictions. No agent
was re-executed. The historical evaluator launches omitted `JUDGE_MODEL`, so
the pinned evaluator's `gpt-4-turbo` fallback was used. Those measurements are
preserved and are not relabeled.

The canonical rescoring uses the unchanged pinned evaluator source revision
`14f026fc9cc348c4ecec5ab32714de954c95c1b1` with the repository-owned
`itbench_luna_judge_compat_v1` profile:

- provider: `openai`
- model: `gpt-5.6-luna`
- temperature: `1`
- provider retries: `0`
- evaluator attempts per case: `1`
- fallback model: `NONE`

| execution | historical GPT-4-Turbo mean F1 | Luna mean F1 | absolute delta |
|---|---:|---:|---:|
| E4 | 0.22222 | 0.01905 | -0.20317 |
| E5 | 0.00000 | 0.00000 | 0.00000 |
| E6 | 0.76190 | 0.11765 | -0.64426 |

The Luna result is the canonical external series because all future project
judge work is required to use Luna. The historical GPT-4-Turbo series remains
useful as historical evidence but is not directly comparable as an identical
measurement instrument.

The official evaluator sometimes emits `null` for no-prediction precision/F1
fields. Consequently, raw representation disagreements (null versus numeric
zero) are distinguished from semantic positive-match disagreements in the
machine-readable comparison. See
`itbench-judge-model-comparison.json` for the complete scenario lists.

The first Luna E4 batch consumed its reserved calls but failed to persist its
result because of a relative-path invocation error. The successful E4
recovery batch is the recorded Luna score; no calls were refunded.
