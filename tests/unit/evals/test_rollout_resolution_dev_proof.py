"""Engine-level DEV evidence transition and explanation-scope counterexamples."""

from copy import deepcopy

import pytest
from scripts.rollout_resolution_dev_proof import CASES, evaluate, source, variant

from packages.evals.rollout_refusal_accounting import assess_rollout_refusal_accounting
from packages.rca.engine import RCA_ENGINE_VERSION, build_case, diagnose_case


def test_current_operation_evidence_completes_existing_resolution_conditions() -> None:
    before = build_case(variant("missing_current_operations"))
    after = build_case(source())
    incomplete, complete = diagnose_case(before), diagnose_case(after)
    assert incomplete.resolution.value == "AMBIGUOUS"
    assert complete.resolution.value == "RESOLVED"
    assert incomplete.root_cause == complete.root_cause
    assert complete.root_cause is not None and complete.root_cause.name == "payment-service"
    assert before.hypotheses == after.hypotheses
    assert before.findings == after.findings
    assert before.candidates == after.candidates
    assert incomplete.resolution_trace is not None and complete.resolution_trace is not None
    assert (
        incomplete.resolution_trace.plausible_hypotheses
        == complete.resolution_trace.plausible_hypotheses
    )
    assert incomplete.resolution_trace.mechanism_verified_hypotheses == ()
    assert len(complete.resolution_trace.mechanism_verified_hypotheses) == 1
    assert complete.resolution_trace.unresolved_hypotheses == ()
    assert complete.resolution_trace.material_frontier_ids == ()


@pytest.mark.parametrize("name", CASES)
def test_experimental_guard_does_not_rewrite_root_support_or_frontier(name: str) -> None:
    result = evaluate(name)
    assert all(result["invariants"].values())
    assert result["production_rule_registered"] is False
    assert RCA_ENGINE_VERSION == "2.18.0"
    expected = "RESOLVED" if name == "current_refusal_complete" else "AMBIGUOUS"
    assert result["guarded_experiment"]["resolution"] == expected


def test_current_group_must_account_for_historical_log_even_when_not_all_logs_are_cited() -> None:
    case = build_case(variant("historical_manifestation"))
    diagnosis = diagnose_case(case)
    before = deepcopy(case.hypotheses)
    (assessment,) = assess_rollout_refusal_accounting(case, diagnosis)
    assert case.hypotheses == before
    assert not assessment.eligible_for_shared_explanation
    assert assessment.unaccounted_log_ids == ("historical-log:independent",)
    assert assessment.accounted_log_ids == ("log:0", "log:1", "log:2", "log:3")
    assert (
        diagnosis.resolution.value == "RESOLVED"
    )  # reproduces current shared-evidence scope behavior
    guarded = evaluate("historical_manifestation")
    assert guarded["guarded_experiment"]["resolution"] == "AMBIGUOUS"


def test_uncaptured_aggregate_member_cannot_pass_by_checking_only_first_four_ids() -> None:
    observed = source()
    # Five logged failures; only four records survive while the derived finding still says five.
    observed.error_items.append(
        observed.error_items[-1].model_copy(update={"evidence_id": "log:4"})
    )
    case = build_case(observed)
    diagnosis = diagnose_case(case)
    observed.error_items.pop()
    (assessment,) = assess_rollout_refusal_accounting(case, diagnosis)
    assert not assessment.eligible_for_shared_explanation
    assert "AGGREGATE_LOG_INVENTORY_NOT_ACCOUNTED" in assessment.deficits


def test_uncited_fifth_log_is_checked_even_when_first_four_are_bound() -> None:
    observed = source()
    extra = observed.error_items[-1].model_copy(
        update={
            "evidence_id": "uncited-log:5",
            "attributes": {
                **observed.error_items[-1].attributes,
                "trace_id": "f" * 32,
                "request_id": "unseen-request",
            },
        }
    )
    observed.error_items.append(extra)
    case = build_case(observed)
    diagnosis = diagnose_case(case)
    (assessment,) = assess_rollout_refusal_accounting(case, diagnosis)
    assert (
        "uncited-log:5"
        not in next(f for f in case.findings if f.kind.value == "DEPENDENCY_ERRORS").evidence_ids
    )
    assert assessment.unaccounted_log_ids == ("uncited-log:5",)
    assert not assessment.eligible_for_shared_explanation
