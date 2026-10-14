# Evidence-directed research integration v1

The development branch now gives existing investigation reads an explicit research
question. The purpose is to acquire missing discriminating evidence. It does not
reward or require RESOLVED, and it does not change resolver conditions.

## Delivered

- `investigation/research.py` projects existing information gaps and exact authorized
  physical candidates into questions, scope, possible outcomes and bounded reads.
- Action-policy context carries this information. The bounded prompt explicitly treats
  rival support, leader contradiction and persistent uncertainty as valid research results.
- Every authorized action audit retains its question and exact candidate read. Actual
  evidence, findings and decision consequences stay in the existing audit fields.
- `InvestigationResult.remaining_research_questions` retains all current gap records,
  with separate availability for unavailable tools, observed gaps, missing eligible
  candidates, attempted reads and exhausted per-gap budgets.
- Deterministic ranking, authorization, provider boundaries, budgets, normalizers,
  witnesses and resolution rules remain as before. No explanatory population-completeness
  condition is added. No missing-data observation can exonerate Kafka/Redis through this layer.

Engine 2.20.0 versions the action-policy context and prompt change; historical engine
versions, HOLDOUT artifacts and previous DEV measurements are unchanged. This is branch
code, not a deployment of the shared control plane.

## Validation and limits

The non-PostgreSQL regression run passed **3103 tests**, with 25 PostgreSQL tests
deselected and one existing Starlette deprecation warning. Subsequent focused checks
cover the final added per-gap budget assertion and version pins.

Tests exercise all existing gap dimensions; supporting a rival and contradicting a
leader; exact scope/query retention; unauthorized namespace/target and missing
discriminator rejection; unavailable/observed/attempted/budget distinctions; and no
mutation of the diagnosis or epistemic digest by projection.

An actual default deterministic-intent run is compared with the same run with research
metadata disabled: actions, tool count, ledger, observations, stop reason and diagnosis
digest match. Existing graph cases additionally verify useful supporting/contradicting
evidence while remaining AMBIGUOUS, neutral NO_DATA and unavailable-tool reporting.
The targeted request-pair provider-tape replay compares both decision digest and
research records.

No new HOLDOUT or live workload experiment was run for this change. There is no measured
accuracy, RESOLVED-rate or acquisition-efficiency gain. The deterministic selector already
uses discrimination, duplication and acquisition-cost metadata; this change makes its
questions explicit rather than retuning it. LLM selection quality under the enriched
context remains a separate evaluation.

The next experiment should compare evidence acquisition over several frozen DEV failure
families: which recorded questions gain discriminating evidence, which remain unknown,
why they remain unknown, and the associated read/byte/time cost. RESOLVED is an observed
consequence, not the objective of that experiment.
