"""Offline regressions for evidence-layer correctness before retrieval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import ITBenchEvidenceCategory, ITBenchLiteDataset, ITBenchScenario
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import (
    E9SemanticOperations,
    SemanticCapabilityResolver,
    _resources,
)
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend


def _scenario(
    tmp_path: Path,
    *,
    metric_rows: list[dict[str, str]] | None = None,
    object_bodies: list[dict[str, Any]] | None = None,
    event_bodies: list[dict[str, Any]] | None = None,
    log_rows: list[dict[str, str]] | None = None,
) -> ITBenchScenario:
    root = tmp_path / "Scenario-evidence"
    root.mkdir(parents=True)
    (root / "alerts.json").write_text(json.dumps({"data": {"alerts": []}}), encoding="utf-8")

    def write_tsv(name: str, rows: list[dict[str, str]], fields: list[str]) -> str:
        path = root / name
        lines = ["\t".join(fields)]
        lines.extend("\t".join(row.get(field, "") for field in fields) for row in rows)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return name

    objects = object_bodies or []
    events = event_bodies or []
    object_rows = [
        {"Timestamp": "2025-01-01T00:00:00Z", "Body": json.dumps(body)} for body in objects
    ]
    event_rows = [
        {"Timestamp": "2025-01-01T00:00:00Z", "Body": json.dumps(body)} for body in events
    ]
    metrics = metric_rows or []
    logs = log_rows or []
    files: dict[ITBenchEvidenceCategory, tuple[str, ...]] = {
        ITBenchEvidenceCategory.ALERTS: ("alerts.json",),
        ITBenchEvidenceCategory.K8S_OBJECTS: (
            write_tsv("objects.tsv", object_rows, ["Timestamp", "Body"]),
        ),
        ITBenchEvidenceCategory.K8S_EVENTS: (
            write_tsv("events.tsv", event_rows, ["Timestamp", "Body"]),
        ),
        ITBenchEvidenceCategory.METRICS: (
            write_tsv(
                "metrics.tsv",
                metrics,
                sorted(
                    {key for row in metrics for key in row} or {"timestamp", "metric_name", "value"}
                ),
            ),
        ),
        ITBenchEvidenceCategory.LOGS: (
            write_tsv(
                "logs.tsv",
                logs,
                sorted({key for row in logs for key in row} or {"Timestamp", "Body"}),
            ),
        ),
        ITBenchEvidenceCategory.TRACES: (
            write_tsv("traces.tsv", [], ["Timestamp", "ServiceName", "Body"]),
        ),
    }
    return ITBenchScenario(
        scenario_id="Scenario-901",
        snapshot_path=str(root),
        evidence_categories=tuple(ITBenchEvidenceCategory),
        evidence_files=files,
    )


def _backend(tmp_path: Path, **kwargs: Any) -> ITBenchSnapshotBackend:
    scenario = _scenario(tmp_path, **kwargs)
    return ITBenchSnapshotBackend(cast(ITBenchLiteDataset, object()), scenario, max_rows=50)


def test_metric_analysis_is_invariant_to_input_order(tmp_path: Path) -> None:
    rows = [
        {
            "timestamp": "2025-01-01T00:00:00Z",
            "metric_name": "error_rate",
            "service_name": "checkout",
            "value": "1",
        },
        {
            "timestamp": "2025-01-01T00:01:00Z",
            "metric_name": "error_rate",
            "service_name": "checkout",
            "value": "2",
        },
        {
            "timestamp": "2025-01-01T00:02:00Z",
            "metric_name": "error_rate",
            "service_name": "checkout",
            "value": "10",
        },
    ]
    results: list[dict[str, Any]] = []
    for ordered in (rows, list(reversed(rows)), [rows[1], rows[2], rows[0]]):
        backend = _backend(tmp_path / str(len(results)), metric_rows=ordered)
        results.append(
            backend.metric_analysis({"metric_name": "error_rate", "service": "checkout"})
        )
    summaries = [result["aggregate"] for result in results]
    assert summaries[0] == summaries[1] == summaries[2]
    assert summaries[0]["delta"] == 9.0
    group = next(iter(results[0]["aggregates_by_metric"].values()))
    assert group["direction"] == "increase"


def test_metric_identity_uses_exact_structured_fields_and_namespace(tmp_path: Path) -> None:
    rows = [
        {
            "timestamp": "2025-01-01T00:00:00Z",
            "metric_name": "latency",
            "service_name": "checkout",
            "namespace": "prod",
            "value": "1",
        },
        {
            "timestamp": "2025-01-01T00:01:00Z",
            "metric_name": "latency",
            "service.name": "checkout-worker-old",
            "namespace": "prod",
            "value": "9",
        },
        {
            "timestamp": "2025-01-01T00:02:00Z",
            "metric_name": "latency",
            "service.name": "checkout",
            "namespace": "dev",
            "value": "9",
        },
    ]
    backend = _backend(tmp_path, metric_rows=rows)
    result = backend.metric_analysis(
        {"metric_name": "latency", "service": "checkout", "namespace": "prod"}
    )
    assert result["matching_count"] == 1


def test_resources_include_container_and_init_container_bounds() -> None:
    value = _resources(
        {},
        {
            "containers": [{"name": "app", "resources": {"requests": {"cpu": "10m"}}}],
            "initContainers": [{"name": "migrate", "resources": {"limits": {"memory": "1Gi"}}}],
        },
    )
    assert value is not None
    assert len(value["container_resources"]) == 2
    assert value["requests"] == {"cpu": "10m"}
    assert value["limits"] == {"memory": "1Gi"}
    assert _resources({}, {}) is None


def test_log_analysis_is_bounded_and_capability_is_honest(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        object_bodies=[{"kind": "Service", "metadata": {"name": "checkout", "namespace": "prod"}}],
        log_rows=[
            {
                "Timestamp": "2025-01-01T00:00:00Z",
                "ServiceName": "checkout",
                "Namespace": "prod",
                "SeverityText": "ERROR",
                "Body": "connection timeout id=123",
            },
        ],
    )
    result = backend.log_analysis({"entity": "prod/Service/checkout", "limit": 5})
    assert result["data_available"] is True
    assert result["patterns"][0]["count"] == 1
    memory = E9CaseMemory(execution_id="offline", scenario_id=backend.scenario.scenario_id)
    memory.discover_entities(({"canonical": "prod/Service/checkout"},))
    resolver = SemanticCapabilityResolver(backend, memory)
    assert resolver.resolve("C001")["LOG_ANALYSIS"]["available"] is True
    assert "LOG_ANALYSIS" in resolver.available_operations(phase="VERIFY", target_handle="C001")


def _pod(name: str, owner: str) -> dict[str, Any]:
    return {
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "prod",
            "labels": {"app": "checkout"},
            "ownerReferences": [{"kind": "ReplicaSet", "name": owner}],
        },
        "spec": {"containers": [{"name": "app", "image": "checkout:v1"}]},
    }


def test_compare_replicas_resolves_owner_siblings_and_singletons_are_unavailable(
    tmp_path: Path,
) -> None:
    backend = _backend(
        tmp_path / "many",
        object_bodies=[_pod("checkout-a", "checkout-rs"), _pod("checkout-b", "checkout-rs")],
    )
    memory = E9CaseMemory(execution_id="offline", scenario_id=backend.scenario.scenario_id)
    memory.discover_entities(
        (
            {"canonical": "prod/Pod/checkout-a"},
            {"canonical": "prod/Pod/checkout-b"},
        )
    )
    operations = E9SemanticOperations(backend, memory)
    comparison = operations._compare_replicas("prod/Pod/checkout-a")
    assert comparison["comparison_available"] is True
    assert comparison["peer_count"] == 1

    singleton_backend = _backend(
        tmp_path / "one", object_bodies=[_pod("checkout-a", "checkout-rs")]
    )
    singleton_memory = E9CaseMemory(
        execution_id="offline", scenario_id=singleton_backend.scenario.scenario_id
    )
    singleton_memory.discover_entities(({"canonical": "prod/Pod/checkout-a"},))
    singleton_ops = E9SemanticOperations(singleton_backend, singleton_memory)
    assert singleton_ops._compare_replicas("prod/Pod/checkout-a")["comparison_available"] is False
