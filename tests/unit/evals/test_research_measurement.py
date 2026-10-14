"""Measurement separates observed acquisition from claims of answered questions."""

import hashlib
from pathlib import Path

import pytest
from scripts.measure_evidence_directed_research import compare, research_metrics

from packages.rca.demo import demo_source
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.model import FrontierAnswer, Resolution, ResolutionTrace


def test_research_measurement_never_counts_a_finding_as_an_answer() -> None:
    result = investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    metrics = research_metrics(result)
    assert metrics["executed_reads"] == result.tool_calls
    assert metrics["reads_with_research_question"] == result.tool_calls
    assert metrics["question_answered_count"] is None
    assert metrics["remaining_gap_count"] == metrics["remaining_question_count"]
    assert sum(metrics["remaining_availability"].values()) == len(result.diagnosis.information_gaps)
    assert (
        sum(
            metrics[key]
            for key in ("reads_supporting", "reads_contradicting", "reads_no_data", "reads_unknown")
        )
        == result.tool_calls
    )


def test_paired_comparison_detects_query_change_with_same_resolution_and_call_count() -> None:
    result = investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    audit = next(item for item in result.action_audits if item.action.query is not None)
    assert audit.action.query is not None
    different_query = audit.action.query.model_copy(update={"limit": 1})
    assert audit.action.query != different_query
    changed_audit = audit.model_copy(
        update={"action": audit.action.model_copy(update={"query": different_query})}
    )
    changed = result.model_copy(
        update={
            "action_audits": tuple(
                changed_audit if item is audit else item for item in result.action_audits
            )
        }
    )
    comparison = compare(result, changed)
    assert comparison["same_decision_digest"]
    assert comparison["same_tool_calls"]
    assert not comparison["same_actions"]


def test_acquisition_counts_do_not_depend_on_question_annotations() -> None:
    result = investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    without_metadata = result.model_copy(
        update={
            "action_audits": tuple(
                audit.model_copy(update={"research_question": None})
                for audit in result.action_audits
            ),
            "remaining_research_questions": (),
        }
    )
    enriched = research_metrics(result)
    control = research_metrics(without_metadata)
    assert enriched["reads_with_research_question"] > 0
    assert control["reads_with_research_question"] == 0
    for key in (
        "executed_reads",
        "reads_with_new_evidence",
        "reads_with_normalized_findings",
        "reads_with_decision_change",
        "reads_supporting",
        "reads_contradicting",
        "reads_no_data",
        "reads_unknown",
        "asked_dimensions",
    ):
        assert enriched[key] == control[key]


def test_only_recorded_answer_transition_counts_not_disappearing_question() -> None:
    result = investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    assert result.initial_diagnosis is not None
    initial = result.initial_diagnosis.model_copy(
        update={
            "resolution_trace": ResolutionTrace(
                state=Resolution.AMBIGUOUS,
                frontier_answers=(
                    FrontierAnswer(alternative_id="answered", question="caller relation"),
                    FrontierAnswer(alternative_id="disappeared", question="other relation"),
                ),
            ),
        }
    )
    final = result.diagnosis.model_copy(
        update={
            "resolution_trace": ResolutionTrace(
                state=Resolution.AMBIGUOUS,
                frontier_answers=(
                    FrontierAnswer(
                        alternative_id="answered",
                        question="caller relation",
                        state="ANSWERED_ON_CLAIM_PATH",
                        evidence_ids=("path:1",),
                        rule_id="m21.frontier.dependency-on-claim-path",
                    ),
                ),
            ),
        }
    )
    metrics = research_metrics(
        result.model_copy(update={"initial_diagnosis": initial, "diagnosis": final})
    )
    assert metrics["initial_open_frontier_questions_answered"] == 1
    assert metrics["frontier_answer_rules"] == {"m21.frontier.dependency-on-claim-path": 1}
    assert metrics["question_answered_count"] is None


def test_reanalysis_preserves_raw_artifacts_and_never_reruns_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.measure_evidence_directed_research as measurement

    from packages.evals.investigation_metrics import derive_investigation_metrics

    result = investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    control = result.model_copy(
        update={
            "action_audits": tuple(
                audit.model_copy(update={"research_question": None})
                for audit in result.action_audits
            ),
            "remaining_research_questions": (),
        }
    )
    rows = []
    for arm, run in (("metadata_on", result), ("metadata_off", control)):
        measurement.write_json(
            tmp_path / "FULL_SOURCE" / "demo" / f"{arm}.json", run.model_dump(mode="json")
        )
        rows.append(
            {
                "seed_mode": "FULL_SOURCE",
                "scenario_id": "demo",
                "arm": arm,
                "seconds": 1.0,
                "initial_resolution": run.initial_resolution.value,
                "final_resolution": run.final_resolution.value,
                "stop_reason": run.stop_reason.value,
                "runtime": derive_investigation_metrics(run).model_dump(mode="json"),
            }
        )
    measurement.write_json(tmp_path / "summary.json", {"per_run": rows})
    raw = tuple(tmp_path.rglob("*.json"))
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in raw}
    monkeypatch.setattr(
        measurement,
        "investigate_diagnosis",
        lambda *args, **kwargs: pytest.fail("reanalysis must not run the engine"),
    )
    analyzed = measurement.reanalyze(tmp_path)
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in raw} == hashes
    assert analyzed["all_paired_invariants_pass"]
    assert all(
        check["selected_read_binding_failures"] == 0 for check in analyzed["research_record_checks"]
    )
    assert analyzed["groups"]["FULL_SOURCE"]["metadata_on"]["executed_reads"] == result.tool_calls
    assert analyzed["groups"]["FULL_SOURCE"]["metadata_off"]["executed_reads"] == result.tool_calls
