"""Crash-safe A1 live-run checkpoint tests."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.evals.a1_checkpoints import (
    A1CheckpointStore,
    A1PartialRun,
    A1ScenarioCheckpoint,
    write_partial_run,
)
from packages.evals.a1_graders import grade_a1_run
from packages.evals.a1_targets import A1_TARGET_BY_SCENARIO
from packages.evals.live_fixtures import BenchmarkTrial
from packages.investigation.artifacts import A1RunArtifact

CONFIG = {"topology": "topology-hash", "prompt": "prompt-hash"}


def _artifact() -> A1RunArtifact:
    payload = json.loads(Path("docs/benchmarks/a1-r1-live-smoke.json").read_text())
    return A1RunArtifact.from_json(
        json.dumps(payload["artifact"], sort_keys=True, separators=(",", ":"))
    )


def _checkpoint(tmp_path: Path, scenario_id: str = "V020-001") -> A1ScenarioCheckpoint:
    artifact = _artifact()
    grade = grade_a1_run(artifact, A1_TARGET_BY_SCENARIO[scenario_id])
    trial = BenchmarkTrial(
        scenario_id=scenario_id,
        fixture="payment_error_spike",
        started_at=datetime(2026, 9, 14, tzinfo=UTC),
        alert_name="PaymentErrorRateHigh",
        incident_id=artifact.incident_id,
        baseline_restored=True,
    )
    return A1ScenarioCheckpoint(
        execution_id="a1-r4-test",
        scenario_id=scenario_id,
        set="compatibility",
        fixture="payment_error_spike",
        scenario_hash="0" * 64,
        trial=trial,
        artifact=artifact,
        grade=grade,
        configuration_hashes=CONFIG,
        calls_before=0,
        calls_after=1,
        calls_consumed=1,
        completed_at="2026-09-14T00:00:00+00:00",
    )


def test_checkpoint_is_atomic_and_reloadable(tmp_path: Path) -> None:
    store = A1CheckpointStore(
        tmp_path / "run",
        execution_id="a1-r4-test",
        configuration_hashes=CONFIG,
    )
    store.prepare_new_run()
    reference = store.write(_checkpoint(tmp_path))

    assert reference.sha256
    assert store.load("V020-001").scenario_id == "V020-001"
    assert not list((tmp_path / "run").glob("*.tmp"))


def test_duplicate_checkpoint_is_rejected(tmp_path: Path) -> None:
    store = A1CheckpointStore(tmp_path, execution_id="a1-r4-test", configuration_hashes=CONFIG)
    store.write(_checkpoint(tmp_path))
    with pytest.raises(FileExistsError):
        store.write(_checkpoint(tmp_path))


def test_wrong_execution_and_configuration_are_rejected(tmp_path: Path) -> None:
    store = A1CheckpointStore(tmp_path, execution_id="a1-r4-test", configuration_hashes=CONFIG)
    wrong_execution = _checkpoint(tmp_path).model_copy(update={"execution_id": "other"})
    with pytest.raises(ValueError, match="execution ID"):
        store.write(wrong_execution)
    wrong_config = _checkpoint(tmp_path).model_copy(
        update={"configuration_hashes": {"topology": "different"}}
    )
    with pytest.raises(ValueError, match="configuration hashes"):
        store.write(wrong_config)


def test_corrupt_checkpoint_is_rejected(tmp_path: Path) -> None:
    store = A1CheckpointStore(tmp_path, execution_id="a1-r4-test", configuration_hashes=CONFIG)
    store.write(_checkpoint(tmp_path))
    path = store.path_for("V020-001")
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValidationError):
        store.load("V020-001")


def test_partial_run_preserves_completed_checkpoint_references(tmp_path: Path) -> None:
    directory = tmp_path / "checkpoints"
    store = A1CheckpointStore(directory, execution_id="a1-r4-test", configuration_hashes=CONFIG)
    first = _checkpoint(tmp_path, "V020-001")
    second = first.model_copy(
        update={
            "scenario_id": "V020-002",
            "trial": first.trial.model_copy(update={"scenario_id": "V020-002"}),
            "grade": first.grade.model_copy(update={"scenario_id": "V020-002"}),
        }
    )
    store.write(first)
    store.write(second)
    partial_path = tmp_path / "partial.json"
    write_partial_run(
        partial_path,
        A1PartialRun(
            artifact_type="A1_SINGLE_AGENT_LIVE_PARTIAL",
            execution_id="a1-r4-test",
            status="INVALIDATED",
            scenario_order=["V020-001", "V020-002", "V020-003"],
            completed_scenario_ids=["V020-001", "V020-002"],
            invalidation_scenario="V020-003",
            invalidation_code="FIXTURE_CORRELATION_FAILED",
            invalidation_reason="fixture failed",
            ledger_path=".local/a1-r4-single-agent-live-budget.json",
            ledger_before=0,
            ledger_after=2,
            outbound_attempts=2,
            checkpoint_references=[
                store.reference("V020-001"),
                store.reference("V020-002"),
            ],
        ),
    )
    restored = A1PartialRun.model_validate_json(partial_path.read_text())
    assert restored.completed_scenario_ids == ["V020-001", "V020-002"]
    assert restored.invalidation_scenario == "V020-003"
    assert not store.path_for("V020-003").exists()


def test_ordered_checkpoint_assembly_is_source_of_final_scenario_order(tmp_path: Path) -> None:
    store = A1CheckpointStore(
        tmp_path / "run", execution_id="a1-r4-test", configuration_hashes=CONFIG
    )
    first = _checkpoint(tmp_path, "V020-001")
    second = first.model_copy(
        update={
            "scenario_id": "V020-002",
            "trial": first.trial.model_copy(update={"scenario_id": "V020-002"}),
            "grade": first.grade.model_copy(update={"scenario_id": "V020-002"}),
        }
    )
    store.write(first)
    store.write(second)

    ordered = store.load_ordered(["V020-001", "V020-002"])

    assert [item.scenario_id for item in ordered] == ["V020-001", "V020-002"]
    with pytest.raises(FileNotFoundError):
        store.load_ordered(["V020-001", "V020-003"])


def test_prepare_new_run_ignores_temporary_files_but_rejects_completed_json(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / ".V020-001.json.tmp").write_text("partial", encoding="utf-8")
    store = A1CheckpointStore(directory, execution_id="a1-r4-test")
    store.prepare_new_run()
    store.write(_checkpoint(tmp_path))
    with pytest.raises(RuntimeError, match="not empty"):
        store.prepare_new_run()
