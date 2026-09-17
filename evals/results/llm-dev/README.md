# LLM investigator on the dev split

Two runs of the optional LLM investigator (`openai / gpt-5.6-luna`, call budget
60) on the 10 dev scenarios, both from clean commits. The dev split is used for
tuning, so these numbers only compare modes; they are not test results.

| Run | Commit | Macro F1 | Verified-only F1 | Model calls | Time |
| --- | --- | ---: | ---: | ---: | ---: |
| Deterministic engine (same code as run 2) | `2ba31d8` | 0.90 | 0.80 | 0 | ~15 s |
| [Run 1](run-1/report.md) | `8600d3b` | 0.70 | 0.60 | 29 | 134 s |
| [Run 2](run-2/report.md) | `2ba31d8` | 0.90 | 0.80 | 12 | 97 s |

Run 1's report shows 136 calls: the runner summed a cumulative counter. The
per-scenario records (2, 4, 6, …, 29) show 29 real calls; this was fixed in
`2ba31d8`.

## Run 1: the model made the answer worse

- In three chaos scenarios the briefing gave each experiment's latest run
  time (18:00), not when the recurring schedule started (16:28). The model
  reasoned that the fault began after the alerts and chose a symptomatic pod.
- In Scenario-7 it replaced a `VERIFIED` ConfigMap change with an
  `UNVERIFIED` pod.
- Two replies were unusable ("invalid JSON"), which ended the investigation
  for those scenarios.
- Every wrong answer the model chose was labelled `UNVERIFIED`; all six
  `VERIFIED` answers were correct.

## Fixes before run 2 (`2ba31d8`)

- Fault findings report when an experiment started and the schedule's active
  span; the kept experiment is the latest one started before onset; failed
  experiments are no longer findings.
- The investigator may replace the ranked answer only with one that verifies
  at least as strongly.
- Unusable output is retried once and its reason is recorded.
- Model calls are counted per incident.

## Run 2: no harm, no measurable gain

The model agreed with the engine's first candidate in all ten scenarios
(one after inspecting it with two tools). No reply was rejected or retried,
and the verification guard was never needed. On this split the engine is
already at its ceiling (the one miss has ground truth that matches no
observable object), so any benefit of the investigator has to be measured on
harder cases.
