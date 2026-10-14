# Rule-precondition acquisition v1

Fixed before implementation, without benchmark outcomes. Existing resolver and
budgets remain unchanged. Classification describes a possible acquisition, never
whether a condition is true or will become satisfied.

- DIRECT: an exact authorized query can retrieve the input named by a recorded
  atomic condition. First route: unchanged-configuration NO_LISTING -> history of
  the exact configuration actor, bounded window containing the incident onset.
  This supplies only the missing listing; write metadata and continuous coverage
  remain separate rule conditions. Empty history never proves unchanged state.
- PARTIAL: a query can inspect relevant inputs but cannot establish the named
  condition alone. Missing write metadata and missing caller trace observations
  are partial routes, with exact actor/caller and bounded onset scope.
- UNMAPPED: no registered input route. Recovery, execution proof, independent
  coverage and unknown rule reasons are never guessed into a generic log query.

Direct precondition-bound inputs add an ordinal specificity class above generic
structured decoder inputs, capped per query; multiplying deficits cannot increase
value. Existing conservative decision-impact comparison still applies. No new
query is authorized by this registry. Candidates must retain exact gap binding,
query authorization, identity, time and budget gates. NO_DATA leaves the condition
open. Available routes and remaining execution budget are separate concepts.

Constructed tests must exercise exact rule/reason binding, foreign actor, wrong
window, absent onset, unrelated rule, multiplicity, NO_DATA neutrality and actual
selection of an authorized route. No DEV/TEST/HOLDOUT tuning or run is required.

## Implemented registry

| Existing condition | Acquisition class | Input route |
| --- | --- | --- |
| unchanged-configuration/v1: NO_LISTING | DIRECT | exact configuration history |
| unchanged-configuration/v1: NO_WRITE_RECORD | PARTIAL | listing write metadata |
| dependency-declared-unused/v1: CALLER_NOT_TRACED | PARTIAL | caller spans |
| dependency-declared-unused/v1: CALLS_NOT_TRACED | PARTIAL | caller outbound spans |
| dependency-declared-unused/v1: TRACE_READ_FAILED | PARTIAL | caller trace read |
| continuous coverage, witness execution/effect/recovery, unknown rule/version | UNMAPPED | no registered atomic route |

The registry is intentionally incomplete. UNMAPPED means no registered route;
it does not prove the observation is impossible to obtain. Final candidate IDs
name matching catalogued reads, not a promise that remaining budgets permit them.
Old artifacts retain default empty targets and unmapped classification; no past
route or causal evidence is invented on parsing.

## Validation

Engine 2.22.0: 3,133 non-PostgreSQL tests passed; 25 PostgreSQL tests were
excluded without starting shared services. The new constructed suite contains
13 cases, including history-tool execution with missing/present listings and
remaining OPEN questions. Ruff and mypy passed. No benchmark gain is claimed.
