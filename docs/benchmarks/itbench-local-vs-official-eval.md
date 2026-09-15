# ITBench local versus official evaluation

The pinned official evaluator revision is `14f026fc9cc348c4ecec5ab32714de954c95c1b1`.
Its implementation is not present in this checkout and the recorded external
revision was not available during this iteration. Consequently no official
judge calls were made and the three frozen prediction sets were not rerun.

The repository's corrected deterministic scorer remains the only available
score for E4, E5, and E6. It is a cross-check/regression metric, not an
official ITBench score. The per-scenario local grades are preserved in the
historical result artifacts and were not rewritten.

| execution | local deterministic | official ROOT_CAUSE_ENTITY |
|---|---:|---|
| ITB-E4 | available in frozen result | NOT RUN |
| ITB-E5 | available in frozen result | NOT RUN |
| ITB-E6 | available in frozen result | NOT RUN |

No local-versus-official disagreement table can be produced without the
pinned evaluator implementation. This is an evaluator availability blocker,
not evidence that the local scorer is authoritative.
