"""Temporal explanations cannot acquire admission, exclusion or recovery authority."""

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from scripts.episode_relevance_analysis import main

from packages.evals.episode_relevance import analyze_episode_relevance


def diagnosis(at: str | None = "2026-10-13T13:00:00Z") -> dict[str, Any]:
    return {
        "resolution": "AMBIGUOUS",
        "epistemic_digest": "frozen",
        "resolution_trace": {"unresolved_hypotheses": ["peer"]},
        "hypothesis": {
            "hypothesis_id": "peer",
            "mechanism": "MANIFESTATION_ONLY",
            "episode_onset": "2026-10-13T15:00:00Z",
            "causal_paths": [[{"relation": "dependency_of"}]],
            "findings": [
                {
                    "kind": "DEPENDENCY_ERRORS",
                    "at": at,
                    "incident_onset": "2026-10-13T15:00:00Z",
                    "evidence_ids": ["old", "ongoing"],
                }
            ],
        },
    }


def test_mixed_source_times_not_replaced_by_aggregate_or_assigned_onset() -> None:
    doc = diagnosis()
    before = deepcopy(doc)
    report = analyze_episode_relevance(
        doc,
        observations={
            "old": {"at": "2026-10-13T13:00:00Z"},
            "ongoing": {"at": "2026-10-13T15:01:00Z"},
        },
    )
    item = report["assessments"][0]
    assert item["aggregate_finding_timing"] == "BEFORE_EPISODE_ONSET"
    assert [r["timing"] for r in item["source_observations"]] == [
        "BEFORE_EPISODE_ONSET",
        "AT_OR_AFTER_EPISODE_ONSET",
    ]
    assert item["current_episode_binding"] == item["continuation"] == "UNASSESSED"
    assert item["recorded_unresolved_rival"] is True
    assert item["assigned_onset_is_continuation_proof"] is False
    assert doc == before
    item["evidence_ids"].append("new")
    assert doc == before


@pytest.mark.parametrize("at", [None, "invalid", "2026-10-13T13:00:00"])
def test_missing_or_unzoned_time_is_unassessed(at: str | None) -> None:
    item = analyze_episode_relevance(diagnosis(at))["assessments"][0]
    assert item["aggregate_finding_timing"] == "UNASSESSED"
    assert "TIME_MISSING_INVALID_OR_UNZONED" in item["evidence_deficits"]


def test_old_initiating_fault_is_out_of_scope() -> None:
    doc = diagnosis()
    doc["hypothesis"]["mechanism"] = "CONFIG_CHANGE"
    assert analyze_episode_relevance(doc)["assessments"] == []


def test_partial_success_does_not_end_persistent_or_intermittent_fault() -> None:
    item = analyze_episode_relevance(
        diagnosis(),
        observations={
            "success": {"at": "2026-10-13T14:59:00Z", "http_status": 201},
            "ongoing": {"at": "2026-10-13T15:01:00Z"},
        },
    )["assessments"][0]
    assert item["recovery"] == item["continuation"] == "UNASSESSED"
    assert "CITED_SOURCE_EVENT_TIMES_NOT_FULLY_RECORDED" in item["evidence_deficits"]


def test_cli_preserves_metadata_and_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, output = tmp_path / "doc.json", tmp_path / "report.json"
    original = json.dumps({"document": diagnosis(), "engine_version": "2.18.0"})
    source.write_text(original)
    monkeypatch.setattr(sys, "argv", ["analysis", str(source), "--output", str(output)])
    main()
    report = json.loads(output.read_text())["revisions"][0]
    assert report["metadata"]["engine_version"] == "2.18.0"
    assert source.read_text() == original
    monkeypatch.setattr(sys, "argv", ["analysis", str(source), "--output", str(source)])
    with pytest.raises(SystemExit):
        main()
    assert source.read_text() == original


def test_report_never_invokes_engine_or_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    import packages.rca.engine as engine
    import packages.rca.resolution as resolution

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("An explanatory report cannot invoke decision rules")

    monkeypatch.setattr(engine, "diagnose_case", forbidden)
    monkeypatch.setattr(engine, "build_case", forbidden)
    monkeypatch.setattr(resolution, "_covered_symptoms", forbidden)
    result = analyze_episode_relevance(diagnosis())
    assert result["authority"] == "EXPLANATORY_ONLY"
    assert result["recorded_resolution"] == "AMBIGUOUS"
