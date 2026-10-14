# Execution/effect input acquisition v1

Fixed before implementation from causal_closure.py rule contracts, without new
benchmark outcomes. No RCA rule, threshold, capture proof or budget changes.

Versioned NOT_FIRED rollout/failed-rollout/quota records can bind existing
exact-authorized candidates to concrete constituent inputs. These routes are
PARTIAL: composite execution/effect witness absence does not tell us that every
constituent is absent. The visible Case input index is consulted without source
or provider reads to suppress input targets whose relevant fact is already present.

Rollout inputs: exact actor UID; ReplicaSet/Pod creation and owner UID; failed
Pod's Warning with involved UID; caller/callee spans at the exact executing Pod
against the existing pre-change baseline. Quota input: FailedCreate at the actual
incident subject naming the quota and matching the origin time/provenance. History
and event probes must cover origin-to-onset; effect probes must also cover the
rule's baseline. Membership scopes an investigation; it does not prove ownership.

The investigation projects input goals from recorded rule failures into scoped
planning gaps. Only the positive claim's known actor, exact observed members and
explicit incident subjects can be authorized. Unknown children remain unroutable;
no namespace enumeration or invented targets are authorized. Generated gaps carry
input goals only and are outside the RCA decision digest. Normal action gates
and provider/budget boundaries remain mandatory. Independent
coverage, recovery and changed-field consumption remain unmapped.

Concrete registered rule inputs use capped ordinal specificity 3, including
PARTIAL constituent probes; the value concerns input specificity, not resolution
probability. Existing conservative decision-impact comparison still applies.
Duplicated requirements and leadership cannot increase the value. Known input
suppression never excludes a claim or establishes absence/completeness.

Acceptance: real candidate construction and selector route binding, missing versus
visible input, exact hypothesis/target/time/version, closed support, baseline
scope, neutral empty query, unchanged plain RCA digest and no planning source read.

## Implementation scope

Engine 2.23.0 refreshes the input goals before assessment and on finalization.
A positive change-onset-path/v2 witness is required for the scoped execution
origin; generic discovery remains valid without it. Root families currently
registered: rollout execution, failed rollout, quota rejection. All constituent
routes are PARTIAL. Strong, explained or eliminated claims are not probed.

The Deployment trace scope includes old/new revisions for the existing baseline;
identity attributes must still satisfy the provider and rule contracts. No complete
telemetry coverage is inferred from a sample. Input goals can extend trace start
only within the existing provider window/cutoff. Unseen children and unsupported
families keep no executable route.

The original frontier measurement's capture-channel gate is specific to 2.22.0.
It must not mask newer execution-input behavior; the historical runner now rejects
other engine versions. No benchmark was run or used to retune the engine here.

Effect/baseline probes retain the bounded full-context trace mode. Error-log-bound
request pairs cannot retrieve the required healthy baseline, so automatic pair
conversion applies only to ordinary dependency queries. This does not weaken pair
validation or alter existing request-pair investigations.

## Validation

The non-PostgreSQL suite passed 3,146 tests before the final trace-mode and frozen
measurement guards. The final execution-input, request-pair, selector-replay and
measurement subset passed 35 tests, including 12 new execution-input cases. Ruff
and mypy passed. Shared services were not started; no benchmark/DEV/HOLDOUT run
was used to develop or tune this change. These are invariant and integration
checks, not evidence of a RESOLVED-rate gain.
