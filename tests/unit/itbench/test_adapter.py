"""Offline tests for the ITBench-Lite snapshot boundary."""

import json
from pathlib import Path
from typing import cast

import pytest

from packages.evals.itbench import (
    ITBENCH_SCENARIO_IDS,
    ITBenchAgentOutput,
    ITBenchEntity,
    ITBenchEntityPrediction,
    ITBenchEvidenceCategory,
    ITBenchGroundTruth,
    ITBenchGroundTruthGroup,
    ITBenchLiteDataset,
    ITBenchRunStore,
    ITBenchScenario,
    ITBenchSnapshotBackend,
    ITBenchSnapshotToolRegistry,
    adapt_a1_output,
    atomic_json_write,
    grade_root_cause_entities,
    official_evaluator_spec,
    write_official_output,
)
from packages.evals.itbench.output_adapter import entities_from_k8s_records


def _scenario(tmp_path: Path) -> ITBenchScenario:
    scenario_dir = tmp_path / "Scenario-1"
    scenario_dir.mkdir()
    (scenario_dir / "alerts.json").write_text(
        json.dumps(
            {
                "data": {
                    "alerts": [
                        {
                            "state": "firing",
                            "activeAt": "2025-12-15T17:25:19Z",
                            "labels": {
                                "alertname": "RequestErrorRate",
                                "namespace": "otel-demo",
                                "service_name": "frontend",
                                "severity": "critical",
                            },
                            "annotations": {"description": "errors"},
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    header = "Timestamp\tServiceName\tBody\n"
    body = '{"metadata":{"name":"frontend","namespace":"otel-demo"},"kind":"Service"}'
    for filename in (
        "k8s_events_raw.tsv",
        "k8s_objects_raw.tsv",
        "otel_logs_raw.tsv",
        "otel_traces_raw.tsv",
    ):
        (scenario_dir / filename).write_text(header + f"2025\tfrontend\t{body}\n", encoding="utf-8")
    (scenario_dir / "metric.tsv").write_text(
        header + "2025\tfrontend\tdegraded\n", encoding="utf-8"
    )
    return ITBenchScenario(
        scenario_id="Scenario-1",
        snapshot_path=str(scenario_dir),
        evidence_categories=tuple(ITBenchEvidenceCategory),
        evidence_files={
            ITBenchEvidenceCategory.ALERTS: ("alerts.json",),
            ITBenchEvidenceCategory.METRICS: ("metric.tsv",),
            ITBenchEvidenceCategory.K8S_EVENTS: ("k8s_events_raw.tsv",),
            ITBenchEvidenceCategory.K8S_OBJECTS: ("k8s_objects_raw.tsv",),
            ITBenchEvidenceCategory.LOGS: ("otel_logs_raw.tsv",),
            ITBenchEvidenceCategory.TRACES: ("otel_traces_raw.tsv",),
        },
    )


def test_snapshot_registry_is_observable_only(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    registry = ITBenchSnapshotToolRegistry(backend)

    context = registry.public_context()
    assert "ground_truth" not in context.casefold()
    assert "fault_mechanism" not in context.casefold()
    assert all(
        "filesystem" not in descriptor["purpose"].casefold()
        for descriptor in registry.descriptors()
    )
    assert len(registry.invoke("itbench_logs", {"limit": 1})["records"]) == 1
    with pytest.raises(PermissionError):
        registry.invoke("ground_truth", {})


def test_evidence_ids_are_deterministic_and_k8s_entities_are_canonical(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    first = backend.records(ITBenchEvidenceCategory.LOGS)
    second = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    ).records(ITBenchEvidenceCategory.LOGS)
    assert first[0]["evidence_id"] == second[0]["evidence_id"]
    alert = backend.records(ITBenchEvidenceCategory.ALERTS)[0]
    assert alert["evidence_id"] == backend.evidence_id(
        ITBenchEvidenceCategory.ALERTS, "alerts.json", 0
    )
    entities = entities_from_k8s_records(backend.records(ITBenchEvidenceCategory.K8S_OBJECTS))
    assert entities[0].canonical == "otel-demo/Service/frontend"


def test_ground_truth_alias_entity_grading_is_deterministic() -> None:
    ground_truth = ITBenchGroundTruth(
        scenario_id="Scenario-1",
        root_cause_groups=(
            ITBenchGroundTruthGroup(
                group_id="config-1",
                kind="ConfigMap",
                namespace="otel-demo",
                name="flags",
                root_cause=True,
            ),
            ITBenchGroundTruthGroup(
                group_id="pod-1", kind="Pod", namespace="otel-demo", filters=("flags-.*",)
            ),
        ),
        aliases=(("config-1", "pod-1"),),
    )
    output = ITBenchAgentOutput(
        incident_id="incident-1",
        scenario_id="Scenario-1",
        contributing_factor=(
            ITBenchEntityPrediction(
                entity=ITBenchEntity(namespace="otel-demo", kind="ConfigMap", name="flags"),
                rank=1,
                condition="flag changed",
            ),
        ),
        native_terminal="HYPOTHESIS_SUBMITTED",
    )
    grade = grade_root_cause_entities(output, ground_truth)
    assert (grade.true_positive, grade.precision, grade.recall, grade.f1) == (1, 1.0, 1.0, 1.0)


def test_native_output_adapter_never_needs_ground_truth(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    entities = entities_from_k8s_records(backend.records(ITBenchEvidenceCategory.K8S_OBJECTS))
    output = adapt_a1_output(
        scenario_id="Scenario-1",
        incident_id="incident-1",
        native_output={
            "causal_hypothesis": {
                "causal_component": "frontend",
                "causal_summary": "observable error evidence",
            },
            "termination_reason": "HYPOTHESIS_SUBMITTED",
        },
        observed_entities=entities,
    )
    assert output.contributing_factor[0].entity.canonical == "otel-demo/Service/frontend"


def test_atomic_external_trial_store_rejects_duplicate(tmp_path: Path) -> None:
    store = ITBenchRunStore(tmp_path, execution_id="ITB-E1")
    output = ITBenchAgentOutput(
        incident_id="incident-1",
        scenario_id="Scenario-1",
        contributing_factor=(),
        native_terminal="STOP",
    )
    digest = store.write_trial(
        "Scenario-1",
        1,
        native_artifact={"terminal": "STOP"},
        itbench_output=output,
        usage={"model_calls": 0},
    )
    assert len(digest) == 64
    official = tmp_path / "Scenario-1" / "1" / "outputs" / "agent_output.json"
    assert json.loads(official.read_text(encoding="utf-8"))["incident_id"] == "incident-1"
    assert store.read_trial("Scenario-1", 1)["trial"] == 1
    (tmp_path / "Scenario-1" / "1" / "itbench_output.json").write_text(
        "{not-json", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid ITBench trial checkpoint"):
        store.read_trial("Scenario-1", 1)
    with pytest.raises(FileExistsError):
        store.write_trial(
            "Scenario-1",
            1,
            native_artifact={},
            itbench_output={},
            usage={},
        )


def test_dataset_discovery_requires_pinned_35_scenario_manifest(tmp_path: Path) -> None:
    manifest = {
        "source": "ibm-research/ITBench-Lite",
        "revision": "d0916b08ba421ce5e672e9ad68aa947d938dfef0",
        "sre_version": "v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7",
        "scenario_ids": list(ITBENCH_SCENARIO_IDS),
    }
    (tmp_path / ".itbench-lite-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="SRE snapshot directory"):
        ITBenchLiteDataset.open(tmp_path).scenarios()


def test_atomic_json_write_is_round_trippable(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    digest = atomic_json_write(path, {"scenario_id": "Scenario-1", "unicode": "ödeme 🛰️"})
    assert len(digest) == 64
    assert json.loads(path.read_text(encoding="utf-8"))["unicode"] == "ödeme 🛰️"
    assert not list(tmp_path.glob("*.tmp"))


def test_snapshot_tool_output_is_bounded_for_large_observation(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    log_path = Path(scenario.snapshot_path) / "otel_logs_raw.tsv"
    log_path.write_text(
        "Timestamp\tServiceName\tBody\n2025\tfrontend\t" + "日志🛰️" * 300 + "\n",
        encoding="utf-8",
    )
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=300
    )
    response = backend.query(ITBenchEvidenceCategory.LOGS, {"limit": 1})
    assert len(json.dumps(response, ensure_ascii=False).encode("utf-8")) <= 300
    assert response["records"][0]["evidence_id"]


def test_official_evaluator_is_pinned_and_output_is_separate(tmp_path: Path) -> None:
    output = ITBenchAgentOutput(
        incident_id="incident-1",
        scenario_id="Scenario-1",
        contributing_factor=(),
        native_terminal="STOP",
    )
    path = tmp_path / "agent-outputs" / "Scenario-1" / "1" / "outputs" / "agent_output.json"
    write_official_output(path, output)
    assert json.loads(path.read_text(encoding="utf-8"))["scenario_id"] == "Scenario-1"
    spec = official_evaluator_spec()
    assert spec.executed is False
    assert spec.revision == "14f026fc9cc348c4ecec5ab32714de954c95c1b1"


def test_investigator_context_cannot_contain_ground_truth_sentinel(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    registry = ITBenchSnapshotToolRegistry(backend)
    sentinel = "DO_NOT_LEAK_SECRET_ROOT_CAUSE_123"
    evaluator_only = ITBenchGroundTruth(
        scenario_id="Scenario-1",
        root_cause_groups=(
            ITBenchGroundTruthGroup(
                group_id="gt-root",
                kind="ConfigMap",
                namespace="otel-demo",
                name=sentinel,
                root_cause=True,
            ),
        ),
    )
    assert evaluator_only.root_cause_entities()[0].name == sentinel
    context = json.dumps(
        {"scenario": scenario.public_context(), "registry": registry.public_context()},
        sort_keys=True,
    )
    assert sentinel not in context
    assert "ground_truth" not in context.casefold()
