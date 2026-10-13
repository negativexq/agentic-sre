"""Counterexamples for a development evidence contract, not an admission rule."""

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from scripts.episode_binding_analysis import main

from packages.evals.episode_binding import EpisodeBindingBundle, assess_episode_binding


def bundle() -> dict[str, Any]:
    relation = {"caller_id": "demo/Service/order", "target_id": "demo/Service/payment"}
    identity = {
        "target_uid": "pod-uid-1",
        "mechanism_fingerprint": "listener-closed",
        "fault_generation_id": "fault-1",
    }
    return {
        "claim_id": "payment-manifestation",
        "incident_id": "current-incident",
        **relation,
        "episode_start": "2026-10-13T15:00:00Z",
        "episode_end": "2026-10-13T15:02:00Z",
        "current_attempt": {
            "evidence_id": "dependency-current",
            "source_read_id": "read-current",
            "incident_id": "current-incident",
            "at": "2026-10-13T15:01:00Z",
            "request_id": "req-current",
            "operation_id": "dependency-op",
            "parent_operation_id": "server-op",
            "outcome": "FAILED",
            **relation,
            **identity,
        },
        "symptom_operation": {
            "evidence_id": "symptom-current",
            "source_read_id": "read-current",
            "incident_id": "current-incident",
            "at": "2026-10-13T15:01:01Z",
            "request_id": "req-current",
            "operation_id": "server-op",
            "actor_id": relation["caller_id"],
            "outcome": "FAILED",
        },
        "historical_attempt": {
            "evidence_id": "dependency-old",
            "source_read_id": "read-old",
            "incident_id": "prior-incident",
            "at": "2026-10-13T13:00:00Z",
            "request_id": "req-old",
            "operation_id": "old-op",
            "outcome": "FAILED",
            **relation,
            **identity,
        },
    }


def evaluate(value: dict[str, Any]) -> Any:
    before = deepcopy(value)
    result = assess_episode_binding(EpisodeBindingBundle.model_validate(value))
    assert value == before
    assert result.authority == "EXPLANATORY_ONLY"
    assert result.admission_change is False
    assert (
        result.causal_origin == result.recovery == result.uninterrupted_continuation == "UNASSESSED"
    )
    return result


def test_preonset_fault_recurs_with_positive_instance_mechanism_link() -> None:
    result = evaluate(bundle())
    assert result.request_relation == result.same_fault_at_two_observations == "RECORDED"
    assert result.deficits == ()


def test_current_relation_does_not_depend_on_historical_identity() -> None:
    value = bundle()
    value["historical_attempt"] = None
    result = evaluate(value)
    assert result.request_relation == "RECORDED"
    assert result.same_fault_at_two_observations == "UNASSESSED"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("target_uid", "pod-uid-other", "EXACT_TARGET_UID_MISSING_OR_MISMATCHED"),
        ("target_uid", None, "EXACT_TARGET_UID_MISSING_OR_MISMATCHED"),
        (
            "mechanism_fingerprint",
            "same-time-different-failure",
            "MECHANISM_IDENTITY_MISSING_OR_MISMATCHED",
        ),
        ("fault_generation_id", "new-rollout-fault", "FAULT_GENERATION_MISSING_OR_MISMATCHED"),
        ("caller_id", "other/Service/order", "HISTORICAL_RELATION_IDENTITY_MISMATCH"),
        ("target_id", "demo/Service/kafka", "HISTORICAL_RELATION_IDENTITY_MISMATCH"),
        ("at", "2026-10-13T15:01:00Z", "HISTORICAL_RECORD_NOT_BEFORE_EPISODE"),
    ],
)
def test_names_and_temporal_proximity_do_not_prove_same_fault(
    field: str, value: Any, code: str
) -> None:
    data = bundle()
    data["historical_attempt"][field] = value
    result = evaluate(data)
    assert result.request_relation == "RECORDED"
    assert result.same_fault_at_two_observations == "UNASSESSED"
    assert code in result.deficits


@pytest.mark.parametrize(
    ("record", "field", "value", "code"),
    [
        ("current_attempt", "request_id", None, "REQUEST_ID_MISSING_OR_MISMATCHED"),
        ("current_attempt", "request_id", "other", "REQUEST_ID_MISSING_OR_MISMATCHED"),
        (
            "current_attempt",
            "parent_operation_id",
            None,
            "DIRECT_PARENT_OPERATION_LINK_MISSING_OR_MISMATCHED",
        ),
        ("current_attempt", "target_id", "demo/Service/redis", "RELATION_IDENTITY_MISMATCH"),
        ("current_attempt", "incident_id", "prior-incident", "CURRENT_INCIDENT_ID_MISMATCH"),
        ("current_attempt", "at", "2026-10-13T13:00:00Z", "CURRENT_RECORD_OUTSIDE_EPISODE"),
        ("current_attempt", "at", "2026-10-13T15:01:00", "TIME_MISSING_TIMEZONE"),
        ("symptom_operation", "outcome", "SUCCEEDED", "FAILED_DEPENDENCY_AND_SYMPTOM_NOT_RECORDED"),
    ],
)
def test_incomplete_trace_or_mismatched_relation_is_unassessed(
    record: str, field: str, value: Any, code: str
) -> None:
    data = bundle()
    data[record][field] = value
    result = evaluate(data)
    assert result.request_relation == result.same_fault_at_two_observations == "UNASSESSED"
    assert code in result.deficits


def test_intermittent_fault_endpoints_do_not_prove_continuity_or_recovery() -> None:
    # A successful call between these endpoints would not alter this contract:
    # neither gap coverage nor global recovery is inferred from two failures.
    result = evaluate(bundle())
    assert result.same_fault_at_two_observations == "RECORDED"
    assert result.uninterrupted_continuation == result.recovery == "UNASSESSED"


def test_missing_provenance_is_not_accepted_as_positive_record() -> None:
    data = bundle()
    del data["current_attempt"]["source_read_id"]
    with pytest.raises(ValidationError):
        EpisodeBindingBundle.model_validate(data)


def test_concurrent_new_fault_cannot_relabel_historical_fault() -> None:
    data = bundle()
    data["current_attempt"]["fault_generation_id"] = "rollout-fault-2"
    result = evaluate(data)
    assert result.request_relation == "RECORDED"
    assert result.same_fault_at_two_observations == "UNASSESSED"
    assert "FAULT_GENERATION_MISSING_OR_MISMATCHED" in result.deficits


def test_invalid_interval_cannot_record_current_relationship() -> None:
    data = bundle()
    data["episode_end"] = "2026-10-13T14:00:00Z"
    result = evaluate(data)
    assert result.request_relation == "UNASSESSED"
    assert "INVALID_EPISODE_INTERVAL" in result.deficits


def test_whitespace_provenance_cannot_pass_validation() -> None:
    data = bundle()
    data["current_attempt"]["source_read_id"] = "   "
    with pytest.raises(ValidationError):
        EpisodeBindingBundle.model_validate(data)


def test_explanatory_checker_does_not_invoke_rca(monkeypatch: pytest.MonkeyPatch) -> None:
    import packages.rca.engine as engine
    import packages.rca.resolution as resolution

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Evidence contract cannot invoke RCA authority")

    monkeypatch.setattr(engine, "diagnose_case", forbidden)
    monkeypatch.setattr(engine, "build_case", forbidden)
    monkeypatch.setattr(resolution, "_covered_symptoms", forbidden)
    assert evaluate(bundle()).request_relation == "RECORDED"


def test_cli_reports_development_authority_and_preserves_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, output = tmp_path / "bundles.json", tmp_path / "report.json"
    original = json.dumps([bundle()])
    source.write_text(original)
    monkeypatch.setattr(sys, "argv", ["check", str(source), "--output", str(output)])
    main()
    result = json.loads(output.read_text())
    assert result["source_authenticity"] == "NOT_VALIDATED"
    assert result["assessment_kind"] == "DEVELOPMENT_CONTRACT_CHECK"
    assert result["assessments"][0]["admission_change"] is False
    assert source.read_text() == original
    monkeypatch.setattr(sys, "argv", ["check", str(source), "--output", str(source)])
    with pytest.raises(SystemExit):
        main()
    assert source.read_text() == original


def test_invalid_second_bundle_does_not_emit_partial_positive_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, output = tmp_path / "bundles.json", tmp_path / "report.json"
    source.write_text(json.dumps([bundle(), {"claim_id": "invalid"}]))
    monkeypatch.setattr(sys, "argv", ["check", str(source), "--output", str(output)])
    with pytest.raises(ValidationError):
        main()
    assert not output.exists()
