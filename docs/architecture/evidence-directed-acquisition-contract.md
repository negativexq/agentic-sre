# Evidence-directed acquisition v1

These acceptance conditions are fixed before implementation. This change must not
consult DEV/TEST/HOLDOUT outcomes or introduce scenario, service-name or fault-label
exceptions. No benchmark run is part of development of this contract.

1. A research target describes the observation family, required observations and
   existing consumer. It is planning metadata, not a new finding, witness, exclusion
   or resolution rule. Unmapped contracts remain explicitly unmapped.
2. Discovery remains legal without a causal-transition certificate, through its
   bounded unknown slots and registered observation families. A mechanism/relation
   probe describes its actual decoder inputs; it never promises an initiating cause.
3. Adding comparison IDs, duplicating a discriminator or renaming hypothesis IDs
   cannot increase evidence-target specificity. IDs bind scope but are not utility.
4. Comparable evidence inputs are valued equally for a leader and a rival. Existing
   transition certificates retain their preconditions, without a leader-specific bonus.
5. Cached trace provenance contributes to repeat risk from the existing Case input
   index only. Planning must not read a provider or the underlying source. Raw reuse
   is not forbidden: an explicitly request-bound relation reconstruction is distinct
   from repeating a generic trace read.
6. Physical frontier coverage, new raw data, new semantic findings and decision
   changes remain separate observations of progress. NO_DATA is not positive evidence
   and cannot exclude a claim. Existing operational stopping rules/budgets stay fixed.
7. Research deficits retain recorded witness/frontier preconditions. A generic log
   or trace capability does not imply it can satisfy every execution, coverage or
   provenance requirement. Unmapped requirements must be visible, not guessed away.
8. All reads continue through existing exact candidate authorization, namespace,
   identity, cutoff, provider-tape and budget boundaries. Rules interpret acquired
   evidence after the read; planning targets have no decision authority.

Acceptance uses constructed and metamorphic cases: ID/name changes, irrelevant
comparison multiplicity, duplicated metadata, cached reads, new relation binding,
discovery without a certificate, leader/rival symmetry and neutral missing data.
The same evidence must yield the same plain RCA decision before and after planning
projection. Investigation may acquire different evidence and therefore yield a
different decision under existing rules; that is not authority conferred by planning.

Any later DEV measurement is diagnostic, not a reason to add exception tables or
change thresholds. A genuinely new observation/decision rule requires its own
contract and independent validation. This contract does not claim that all existing
witness/frontier requirements have an executable tool route.

## Implementation scope

Engine 2.21.0 implements decoder-input targets and certificate precondition projection,
ordinal specificity without comparison counts, neutral leader/rival values, stable
contracted ties, cached trace repeat risk and recorded rule deficits. It does not
implement an executable acquisition plan for every witness condition. Summary rule
reasons with no atomic mapping remain explicitly without a registered route; generic
observation routes are marked partial. Existing progress fields and operational
stopping behavior remain in force.

## Validation

The non-PostgreSQL suite and focused reruns verified all 3,120 collected tests;
25 PostgreSQL tests were excluded, without starting shared services. The selector
replay and acquisition-contract subset passed 18 tests after the final fixture
correction. Ruff and mypy (`packages/rca`, 74 source files) passed. These checks
establish implementation invariants and replay consistency, not benchmark gains.
