"""Offline qualification for the prediction-first E10 boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import packages.evals.itbench.e10_official as e10_official
from packages.evals.itbench.contracts import ITBenchGroundTruth, ITBenchGroundTruthGroup
from packages.evals.itbench.dataset import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBENCH_SRE_VERSION,
    ITBenchLiteDataset,
)
from packages.evals.itbench.e10_local_grading import grade_local_e10
from packages.evals.itbench.e10_official import (
    E10_OFFICIAL_RELEVANT_PATHS,
    E10PredictionError,
    E10PreflightError,
    build_e10_ledger,
    build_e10_manifest,
    predict_e10,
    seal_e10_predictions,
    validate_e10_preflight,
    verify_e10_seal,
)
from packages.evals.itbench.persistence import atomic_json_write
from packages.provider.contracts import (
    ModelRequest,
    ModelResponse,
    ProviderAccountingSnapshot,
    ProviderError,
    ProviderErrorCode,
)
from packages.provider.fake import FakeModelProvider

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _temporary_prediction_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep E10 orchestration tests independent of the optional local snapshot download."""
    dataset_root = tmp_path / "itbench-lite"
    snapshot_root = dataset_root / "snapshots" / "sre" / ITBENCH_SRE_VERSION
    for scenario_id in ITBENCH_SCENARIO_IDS:
        scenario_root = snapshot_root / scenario_id
        (scenario_root / "alerts").mkdir(parents=True)
        (scenario_root / "metrics").mkdir()
        (scenario_root / "alerts" / "alerts.json").write_text(
            json.dumps(
                [
                    {
                        "state": "firing",
                        "labels": {"alertname": "OfflineQualification", "service": "demo"},
                        "annotations": {"summary": "offline qualification"},
                        "activeAt": "2024-01-01T00:00:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (scenario_root / "metrics" / "metrics.tsv").write_text("name\tvalue\n", encoding="utf-8")
        for filename in (
            "k8s_events_raw.tsv",
            "k8s_objects_raw.tsv",
            "otel_logs_raw.tsv",
            "otel_traces_raw.tsv",
        ):
            (scenario_root / filename).write_text("name\tvalue\n", encoding="utf-8")
    atomic_json_write(
        dataset_root / ".itbench-lite-manifest.json",
        {
            "source": "ibm-research/ITBench-Lite",
            "revision": ITBENCH_DATASET_REVISION,
            "sre_version": ITBENCH_SRE_VERSION,
            "scenario_ids": list(ITBENCH_SCENARIO_IDS),
        },
    )
    atomic_json_write(
        dataset_root / ".itbench-source-completeness.json",
        {"status": "PASS", "files": []},
    )
    monkeypatch.setattr(e10_official, "E10_DATA_ROOT", str(dataset_root))


def _manifest_and_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    manifest = build_e10_manifest(ROOT)
    manifest_path = tmp_path / "manifest.json"
    atomic_json_write(manifest_path, manifest.model_dump(mode="json"))
    ledger_path = tmp_path / "ledger.json"
    atomic_json_write(ledger_path, build_e10_ledger(manifest))
    predictions_root = tmp_path / "predictions"
    seal_path = tmp_path / "seal.json"
    result_path = tmp_path / "local-results.json"
    return manifest_path, ledger_path, predictions_root, seal_path, result_path


def _stop_response(_request: object) -> dict[str, object]:
    return {
        "action": "STOP",
        "target": None,
        "targets": [],
        "operation": None,
        "rationale": None,
        "stop_reason": "offline qualification",
    }


def test_e10_manifest_uses_explicit_official_envelope() -> None:
    manifest = build_e10_manifest(ROOT)
    assert manifest.execution == "ITB-E10"
    assert manifest.experiment == "itbench-lite-sre-external-eval-v10"
    assert manifest.scenario_count == 35
    assert manifest.trial_count == 1
    assert manifest.ledger_cap == 35 * manifest.runtime_limits.max_model_calls
    assert manifest.runtime_limits.model_dump() == {
        "max_model_calls": 12,
        "max_tool_calls": 24,
        "max_agent_turns": 12,
        "max_wall_time_seconds": 240,
        "max_consecutive_rejected_actions": 2,
    }


def test_prediction_preflight_is_provider_free(tmp_path: Path) -> None:
    manifest_path, ledger_path, _predictions, _seal, _result = _manifest_and_paths(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    result = validate_e10_preflight(
        ROOT,
        manifest,
        relevant_paths=E10_OFFICIAL_RELEVANT_PATHS,
        ledger=ledger,
    )
    assert result["provider_constructed"] is False
    assert result["ground_truth_access"] is False
    assert result["judge_constructed"] is False


def test_preflight_failure_prevents_provider_factory(tmp_path: Path) -> None:
    manifest_path, ledger_path, predictions, seal, _result = _manifest_and_paths(tmp_path)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["cap"] -= 1
    atomic_json_write(ledger_path, ledger)
    called = False

    def factory(_manifest: object, _budget: object) -> FakeModelProvider:
        nonlocal called
        called = True
        return FakeModelProvider([])

    with pytest.raises(E10PreflightError):
        predict_e10(
            ROOT,
            manifest_path=manifest_path,
            ledger_path=ledger_path,
            predictions_root=predictions,
            seal_path=seal,
            provider_factory=factory,
        )
    assert called is False


def test_preflight_rejects_runtime_envelope_drift(tmp_path: Path) -> None:
    manifest_path, ledger_path, _predictions, _seal, _result = _manifest_and_paths(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_limits"]["max_model_calls"] = 8
    atomic_json_write(manifest_path, manifest)
    with pytest.raises(E10PreflightError, match="runtime envelope"):
        validate_e10_preflight(
            ROOT,
            manifest,
            relevant_paths=E10_OFFICIAL_RELEVANT_PATHS,
            ledger=json.loads(ledger_path.read_text(encoding="utf-8")),
        )


def test_fake_prediction_35x1_has_no_ground_truth_and_seals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, ledger_path, predictions, seal, result_path = _manifest_and_paths(tmp_path)
    gt_called = False

    def forbidden_gt(*_args: object, **_kwargs: object) -> object:
        nonlocal gt_called
        gt_called = True
        raise AssertionError("prediction phase accessed ground truth")

    monkeypatch.setattr(ITBenchLiteDataset, "load_ground_truth", forbidden_gt)
    provider = FakeModelProvider([_stop_response] * 35)
    checkpoints = predict_e10(
        ROOT,
        manifest_path=manifest_path,
        ledger_path=ledger_path,
        predictions_root=predictions,
        seal_path=seal,
        provider_factory=lambda _manifest, _budget: provider,
    )
    assert len(checkpoints) == 35
    assert provider.accounting_snapshot().provider_invocations == 35
    assert gt_called is False
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["status"] == "COMPLETE"
    assert ledger["calls_used"] == 0
    second_provider = FakeModelProvider([])
    resumed = predict_e10(
        ROOT,
        manifest_path=manifest_path,
        ledger_path=ledger_path,
        predictions_root=predictions,
        seal_path=seal,
        provider_factory=lambda _manifest, _budget: second_provider,
    )
    assert len(resumed) == 35
    assert second_provider.requests == []

    sealed = seal_e10_predictions(
        ROOT,
        manifest_path=manifest_path,
        predictions_root=predictions,
        seal_path=seal,
        ledger_path=ledger_path,
    )
    assert sealed["completion_count"] == 35
    assert len(sealed["scenarios"]) == 35
    assert (
        verify_e10_seal(
            ROOT, manifest_path=manifest_path, predictions_root=predictions, seal_path=seal
        )["completion_count"]
        == 35
    )

    def post_seal_gt(_self: ITBenchLiteDataset, scenario_id: str) -> ITBenchGroundTruth:
        return ITBenchGroundTruth(
            scenario_id=scenario_id,
            root_cause_groups=(
                ITBenchGroundTruthGroup(
                    group_id="offline-qualification",
                    kind="Service",
                    namespace="demo",
                    name="not-observed",
                    root_cause=True,
                ),
            ),
        )

    monkeypatch.setattr(ITBenchLiteDataset, "load_ground_truth", post_seal_gt)
    graded = grade_local_e10(
        ROOT,
        manifest=build_e10_manifest(ROOT),
        manifest_path=manifest_path,
        predictions_root=predictions,
        seal_path=seal,
        result_path=result_path,
    )
    assert graded["scenario_count"] == 35
    assert graded["provider_calls"] == 0

    target = predictions / "Scenario-1" / "1" / "agent_output.json"
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(E10PreflightError, match="seal invalid"):
        verify_e10_seal(
            ROOT, manifest_path=manifest_path, predictions_root=predictions, seal_path=seal
        )

    with pytest.raises(E10PreflightError, match="seal is missing"):
        grade_local_e10(
            ROOT,
            manifest=build_e10_manifest(ROOT),
            manifest_path=manifest_path,
            predictions_root=predictions,
            seal_path=tmp_path / "not-sealed.json",
            result_path=result_path,
        )


def test_partial_checkpoint_fails_closed(tmp_path: Path) -> None:
    manifest_path, ledger_path, predictions, seal, _result = _manifest_and_paths(tmp_path)
    (predictions / "Scenario-1" / "1").mkdir(parents=True)
    with pytest.raises(E10PredictionError, match="PARTIAL_SCENARIO"):
        predict_e10(
            ROOT,
            manifest_path=manifest_path,
            ledger_path=ledger_path,
            predictions_root=predictions,
            seal_path=seal,
            provider_factory=lambda _manifest, _budget: FakeModelProvider([]),
        )


def test_provider_failure_aborts_after_persisting_failure_evidence(tmp_path: Path) -> None:
    manifest_path, ledger_path, predictions, seal, _result = _manifest_and_paths(tmp_path)

    class FailingProvider:
        provider_name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _request: ModelRequest) -> ModelResponse:
            self.calls += 1
            raise ProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "offline provider failure",
            )

        def accounting_snapshot(self) -> ProviderAccountingSnapshot:
            return ProviderAccountingSnapshot(provider_invocations=self.calls)

    provider = FailingProvider()
    with pytest.raises(E10PredictionError, match="aborted"):
        predict_e10(
            ROOT,
            manifest_path=manifest_path,
            ledger_path=ledger_path,
            predictions_root=predictions,
            seal_path=seal,
            provider_factory=lambda _manifest, _budget: provider,
        )
    assert (predictions / "prediction_failure.json").is_file()
    assert (predictions / "Scenario-1" / "1" / "trial_manifest.json").is_file()
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["status"] == "FAILED"


def test_prediction_module_has_no_gt_or_judge_imports() -> None:
    source = (ROOT / "packages/evals/itbench/e10_official.py").read_text(encoding="utf-8")
    assert "load_ground_truth" not in source
    assert "packages.evals.itbench.grader" not in source
