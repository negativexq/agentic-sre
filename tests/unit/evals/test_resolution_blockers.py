"""Blocker projections preserve recorded decisions and missing-data boundaries."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from packages.evals.resolution_blockers import (
    BlockerReport,
    analyze_resolution_blockers,
    summarize_blockers,
)


def entity(kind: str, name: str) -> dict[str, str]:
    return {"namespace": "test", "kind": kind, "name": name}


def document() -> dict[str, Any]:
    return {
        "incident_id": "incident",
        "resolution": "AMBIGUOUS",
        "hypothesis": {
            "hypothesis_id": "leader",
            "causal_actor": entity("Deployment", "payment"),
            "symptom_entities": [entity("Service", "order")],
        },
        "alternative_hypotheses": [],
        "resolution_trace": {
            "semantics_version": "m21.v3",
            "state": "AMBIGUOUS",
            "plausible_hypotheses": ["leader"],
            "mechanism_verified_hypotheses": ["leader"],
            "unresolved_hypotheses": [],
            "material_frontier_ids": [],
            "hypothesis_audits": [
                {
                    "hypothesis_id": "leader",
                    "root_support": [
                        {
                            "status": "FIRED",
                            "support_kind": "OBSERVED_MECHANISM_CAUSE",
                            "witnesses": [
                                {
                                    "symptom": entity("Service", "order"),
                                    "evidence_ids": ["observed:1"],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }


def states(report: BlockerReport) -> dict[str, str]:
    return {a.condition: a.status for a in report.conditions}


def test_missing_fields_are_not_satisfied_by_empty_defaults() -> None:
    report = analyze_resolution_blockers(
        {"resolution": "AMBIGUOUS", "resolution_trace": {"semantics_version": "m21.v3"}}
    )
    assert set(states(report).values()) == {"NOT_RECORDED"}
    assert report.material_question_count is None
    assert report.unresolved_claim_count is None
    assert report.unmet_condition_count == 0
    assert report.unassessed_condition_count == 4
    assert "ONE_MATERIAL_QUESTION_REMAINING" not in report.potential


def test_null_inventories_are_unassessed_not_empty() -> None:
    raw = document()
    for field in (
        "plausible_hypotheses",
        "mechanism_verified_hypotheses",
        "unresolved_hypotheses",
        "material_frontier_ids",
        "hypothesis_audits",
    ):
        raw["resolution_trace"][field] = None
    report = analyze_resolution_blockers(raw)
    assert set(states(report).values()) == {"UNASSESSED"}
    assert report.material_question_count is None
    assert report.unresolved_claim_count is None


def test_report_never_calls_engine_or_coverage_rules_and_preserves_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import packages.rca.engine as engine
    import packages.rca.resolution as resolution
    from packages.rca.demo import demo_source
    from packages.rca.epistemic_digest import diagnosis_epistemic_digest

    diagnosis = engine.diagnose(demo_source())
    raw = diagnosis.model_dump(mode="json")
    original = deepcopy(raw)
    digest = diagnosis_epistemic_digest(diagnosis)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Blocker analysis must not evaluate a decision rule")

    monkeypatch.setattr(engine, "diagnose_case", forbidden)
    monkeypatch.setattr(resolution, "_covered_symptoms", forbidden)
    analyze_resolution_blockers(raw)
    assert raw == original
    assert diagnosis_epistemic_digest(diagnosis) == digest


def test_legacy_trace_is_not_reinterpreted_as_m21() -> None:
    raw = document()
    raw["resolution_trace"]["semantics_version"] = "legacy"
    report = analyze_resolution_blockers(raw)
    assert set(states(report).values()) == {"UNASSESSED"}
    assert report.material_question_count is None


def test_direct_scope_does_not_claim_measurement_accounting_or_change_resolution() -> None:
    raw = document()
    before = deepcopy(raw)
    report = analyze_resolution_blockers(raw)
    assert all(a.status == "SATISFIED" for a in report.conditions)
    assert report.resolution == "AMBIGUOUS"
    assert report.measurement_accounting == "NOT_ASSESSED_BY_BLOCKER_ANALYSIS"
    assert raw == before


def test_missing_direct_coverage_is_not_an_uncovered_scope_verdict() -> None:
    raw = document()
    raw["hypothesis"]["symptom_entities"].append(entity("Pod", "order-0"))
    report = analyze_resolution_blockers(raw)
    assert states(report)["WITNESS_SCOPE_COVERED"] == "UNASSESSED"
    assert report.unmet_condition_count == 0
    assert any(d.code == "COVERAGE_ASSESSMENT_NOT_RECORDED" for d in report.evidence_deficits)


def test_recorded_resolution_certifies_scope_without_reconstructing_bridges() -> None:
    raw = document()
    raw["hypothesis"]["symptom_entities"].append(entity("Pod", "order-0"))
    raw["resolution"] = raw["resolution_trace"]["state"] = "RESOLVED"
    report = analyze_resolution_blockers(raw)
    assert states(report)["WITNESS_SCOPE_COVERED"] == "SATISFIED"
    assert "Recorded M21 RESOLVED authority" in report.conditions[1].basis


def test_two_questions_are_one_unmet_condition_and_read_failure_is_a_deficit() -> None:
    raw = document()
    trace = raw["resolution_trace"]
    trace["material_frontier_ids"] = ["kafka", "redis"]
    trace["frontier_answers"] = [
        {"alternative_id": "kafka", "state": "OPEN", "blocked_reason": "TRACE_READ_FAILED"}
    ]
    report = analyze_resolution_blockers(raw)
    assert report.unmet_condition_count == 1
    assert report.material_question_count == 2
    assert {d.code for d in report.evidence_deficits} == {"READ_FAILED", "ANSWER_NOT_RECORDED"}
    assert "ONE_MATERIAL_QUESTION_REMAINING" not in report.potential


def test_nonmaterial_open_answers_and_remaining_contribution_are_not_blockers() -> None:
    raw = document()
    raw["resolution_trace"]["frontier_answers"] = [
        {"alternative_id": "context", "state": "OPEN", "blocked_reason": "TRACE_READ_FAILED"},
        {
            "alternative_id": "kafka",
            "state": "ANSWERED_SYMPTOM_MET_ELSEWHERE",
            "remaining_uncertainty": ["AN_ADDITIONAL_CONTRIBUTION"],
        },
    ]
    report = analyze_resolution_blockers(raw)
    assert report.material_question_count == 0
    assert report.unmet_condition_count == 0
    assert not report.evidence_deficits


def test_rival_with_logs_does_not_imply_a_missing_rule() -> None:
    raw = document()
    raw["alternative_hypotheses"] = [
        {
            "hypothesis_id": "service",
            "causal_actor": entity("Service", "payment"),
            "findings": [{"kind": "DEPENDENCY_ERRORS", "evidence_ids": ["loki:1"]}],
        }
    ]
    raw["resolution_trace"]["unresolved_hypotheses"] = ["service"]
    raw["resolution_trace"]["hypothesis_audits"].append(
        {
            "hypothesis_id": "service",
            "plausibility_reasons": ["NO_ONSET_CAPABLE_INITIATING_EVIDENCE"],
        }
    )
    raw["information_gaps"] = [
        {
            "gap_id": "gap:1",
            "hypothesis_ids": ["service"],
            "missing_fact": "recorded missing fact",
            "resolvability": "UNRESOLVABLE_WITH_CURRENT_TOOLS",
        }
    ]
    report = analyze_resolution_blockers(raw)
    assert report.unmet_condition_count == 1
    assert report.unresolved_claims[0]["actor"] == "test/Service/payment"
    assert {d.code for d in report.evidence_deficits} == {
        "INITIATING_EVIDENCE_NOT_ESTABLISHED",
        "CURRENT_TOOLS_UNAVAILABLE",
    }
    assert all(d.code != "RULE_MISSING" for d in report.evidence_deficits)


def test_single_question_potential_requires_other_conditions_to_be_assessed() -> None:
    raw = document()
    raw["resolution_trace"]["material_frontier_ids"] = ["kafka"]
    report = analyze_resolution_blockers(raw, run_id="run:1", revision_number=2)
    assert "ONE_MATERIAL_QUESTION_REMAINING" in report.potential
    assert summarize_blockers([report])["single_material_question_revisions"] == ["run:1"]
    del raw["resolution_trace"]["hypothesis_audits"]
    assert "ONE_MATERIAL_QUESTION_REMAINING" not in analyze_resolution_blockers(raw).potential


def test_competing_strong_claims_are_not_unique() -> None:
    raw = document()
    raw["resolution_trace"]["plausible_hypotheses"] = ["leader", "other"]
    raw["resolution_trace"]["mechanism_verified_hypotheses"] = ["leader", "other"]
    report = analyze_resolution_blockers(raw)
    assert states(report)["UNIQUE_STRONG_CAUSE"] == "UNSATISFIED"
