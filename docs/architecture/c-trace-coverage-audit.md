# Dependency C: trace coverage and decision authority

2026-10-12. Branch `audit/c-trace-coverage`, baseline `c10bbaf`, engine `2.11.0`.
Audit and proposed contract change only: no engine behavior or version changed.

## Counterexample

Run `.venv/bin/python -m scripts.dependency_coverage_audit` from the repository.
The script uses synthetic unit fixtures: an observed quota rejection provides the
strong claim and complete declared symptom coverage; a dependency of that claim's
affected workload provides a material frontier question. The caller's observed
dependency call is then removed while all other captured spans remain identical.
Both inputs use the same read metadata, hypotheses, execution evidence and onset.
The reference input contains a call; it is not a proof of exhaustive capture.

| Read metadata | Call observed | Call omitted from the sample |
|---|---|---|
| `BEST_EFFORT` | `OPEN / CALLED`, `AMBIGUOUS` | `ANSWERED_DECLARED_UNUSED`, `RESOLVED` |
| `TRUNCATED` | `OPEN / CALLED`, `AMBIGUOUS` | `ANSWERED_DECLARED_UNUSED`, `RESOLVED` |
| `FAILED` | `OPEN / CALLED`, `AMBIGUOUS` | `OPEN / TRACE_READ_FAILED`, `AMBIGUOUS` |

The mechanism-supported claim remains the same. C removes the dependency from
`material_frontier_ids`, permitting the stronger final decision; the epistemic
digest differs. This is a synthetic resolver-level counterexample, not a measured
false resolution in a real run and not an end-to-end testbed injection.

## Stored export census

The same script reads `.local/testbed/**/diagnoses.json` without changing any file
or recomputing any diagnosis. Current exports contain 2,260 diagnoses; 158 contain
an `ANSWERED_DECLARED_UNUSED` answer: 156 `AMBIGUOUS`, 2 `RESOLVED`.
These are exported documents, not the full database/recalculation population of
3,444 diagnoses discussed in the earlier audit. They include different recorded
engine versions. No ground-truth correctness claim follows from these counts.

Two recorded resolved documents use C: `OrderErrorRateHigh` and
`OrderDependencyLatencyHigh` in `dev23-dependency-b/dependency-loss-payment/0`.
Both also carry the withdrawn §31's `ANSWERED_NOT_REACHED` answer. They are not
evidence of a current-engine false resolution. Whether the C answers are justified
cannot be established by this census. The paired recalculation below isolates C's
present effect without reviving §31.
The machine-readable census includes the export paths and answers. Local output:
`.local/diagnostics/c-trace-coverage/audit.json`.

## Completed paired recalculation

All 559 available `testbed_*` databases were read with PostgreSQL
`default_transaction_read_only=on`. Of these, 557 contribute windows; two contain
no `EVIDENCE_GATHERED` event, confirmed by a separate read-only count. Each incident contributes its widest recorded
evidence window, with an event-ID tie breaker. There are 3,444 such windows; this
is not a comparison of every intermediate diagnosis revision. Engine 2.11.0
recalculates frozen manifest/tape inputs, regardless of their historical engine
version. This deliberately uses `ReplaySource` and is not ordinary version-strict
replay, a live query, or an overwrite of any recorded diagnosis.

For each built case the baseline is compared with a scoped audit override that
changes only C's `ANSWERED_DECLARED_UNUSED` into `OPEN /
TRACE_COVERAGE_NOT_PROVEN`. A, D1, positive `CALLED` observations, missing-input
behavior, witness rules, and all other conditions stay unchanged. Every changed
case is calculated a third time after removing the override: the baseline is
identical again. This is isolation of C's negative closure effect, not a full
implementation of the proposed contract.

| Measurement | All windows | Recent era (HOLDOUT19–22, DEV19–23) |
|---|---:|---:|
| Windows | 3,444 | 488 |
| Errors | 0 | 0 |
| Diagnoses with a C negative answer | 163 | 163 |
| C answers withheld | 200 | 200 |
| Digest changes | 163 | 163 |
| Limiting-frontier changes | 143 | 143 |
| Resolution changes | 0 | 0 |
| Root, support, strong witness, display or tier changes | 0 | 0 |
| `RESOLVED` before / after | 54 / 54 | 54 / 54 |

All 200 C answers occur in diagnoses already `AMBIGUOUS`. The final all-era
resolutions are 2,651 `AMBIGUOUS`, 739 `INSUFFICIENT_EVIDENCE`, 54 `RESOLVED` on
both sides. Twenty diagnoses change answers/digests without changing the limiting
frontier; not every answered material question limits an already supported claim.
The recent population is 426 `AMBIGUOUS`, 8 `INSUFFICIENT_EVIDENCE`, 54 `RESOLVED`.
These outcome totals differ from a split by presence of strong witnesses: a
diagnosis without a strong witness may still be `AMBIGUOUS`.

The closed dependencies are Redis (163 answers) and Kafka (37 answers). For the
37 Kafka diagnoses (16 databases), a second check searched the same incident's
stored spans with event time no later than onset and observation time no later
than the frozen cutoff, using the caller's declared hosts. There were zero
attributed calls, zero errors. This is absence in retained data only: it does not
prove exhaustive instrumentation or source capture, and does not justify C's
general authority. No later evidence was fed to the resolver.

**Finding:** C has an unsafe authority boundary demonstrated by the synthetic
counterexample, but is not responsible for any current `RESOLVED` in this stored
population. Removing its authority would preserve all 54 resolved outcomes while
making relevant unanswered questions explicit. The witness/instrumentation and
alert-scope gaps remain the measured obstacles to increasing resolution.

Reproduction (local testbed PostgreSQL and stored inputs required):

```sh
.venv/bin/python -m scripts.dependency_coverage_recalculation --output .local/diagnostics/c-trace-coverage/paired --workers 3
.venv/bin/python -m scripts.dependency_coverage_summary .local/diagnostics/c-trace-coverage/paired
.venv/bin/python -m scripts.dependency_coverage_presence .local/diagnostics/c-trace-coverage/paired
```

Local results: `paired/provenance.json`, `rows.jsonl`, `summary.json`,
`analysis.json` and `kafka_presence.json` under the audit output directory.
The report includes per-window differences; no correctness or generalization
estimate is inferred from unchanged resolution counts.

## Proposed contract boundary (not yet implemented)

This proposal supersedes §29 C's permission to infer non-use from sampled reads;
it does not change §29 A, D1, the withdrawn §31, or §32.

- A positively observed, attributed dependency call retains `OPEN / CALLED`.
- `BEST_EFFORT`, `TRUNCATED`, `FAILED`, unknown completeness strings, absent or
  empty read records, and records that do not establish coverage of the caller
  and relevant interval supply no negative closure authority.
- No existing string is reinterpreted as `COMPLETE`. Neither transport delivery
  nor a successful Tempo search proves that all requests and spans were captured.
- With no observed call and no positive exhaustive-coverage proof, C stays
  `OPEN / TRACE_COVERAGE_NOT_PROVEN`. Positive call evidence remains in the audit;
  known instrumentation/read failures may retain their specific reasons.
- The current system has no such exhaustive-coverage proof. Therefore C produces
  no negative closure under the proposed behavior. A future capture design needs
  its own contract and independent validation before restoring that authority.
- A partial A answer (`NOT_ON_EVERY_CLAIM_PATH`), its affected claims and evidence
  remain intact. A C failure cannot overwrite it; full positive A answers remain
  valid. Additional coverage information belongs in a separate audit record.

## Expected impact and acceptance before implementation

Some diagnoses may change from `RESOLVED` to `AMBIGUOUS`. Digests and frontier
answers can change even where final resolution does not. C's change should not
alter root selection, hypothesis support, execution witnesses, or symptom scope;
any such change must be explained rather than silently accepted.

Before an engine edit, preserve this baseline and define paired recalculation of
every available frozen database input. Normal replay remains version-strict;
recorded diagnoses must not be overwritten or silently relabeled. A behavior
change requires an engine version change and a versioned C rule outcome.

Verification must cover observed calls under each completeness value, missing and
empty read lists, wrong callers, incomplete time ranges, unknown strings, partial
and complete A answers, and the synthetic final-resolution boundary above.
Report frontier/digest changes, retained roots and witnesses, every lost/new
`RESOLVED`, ground-truth availability, and errors separately. Census counts alone
are not a migration measurement.

§30/HOLDOUT23 remains on frozen engine `2.11.0` at `818baf9`; the instrumentation
measurement and complete-capture feasibility are separate work. No new dependency
closure rule is introduced by this proposal.
