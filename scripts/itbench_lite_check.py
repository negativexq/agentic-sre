#!/usr/bin/env python3
"""Run the zero-model ITBench-Lite adapter qualification."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import time
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBENCH_SOURCE,
    ITBENCH_SRE_VERSION,
    ITBenchLiteDataset,
    ITBenchScenarioQualification,
    ITBenchSnapshotBackend,
    ITBenchSnapshotToolRegistry,
    atomic_json_write,
    build_observable_incident,
    entities_from_k8s_records,
    trace_index_path,
)
from packages.evals.itbench.dataset import source_file_digest

OFFICIAL_EVALUATOR_REVISION = "14f026fc9cc348c4ecec5ab32714de954c95c1b1"
REPORT_PATH = Path("docs/benchmarks/itbench-lite-adapter-qualification-e0.1.json")


def qualify(root: Path) -> dict[str, Any]:
    dataset = ITBenchLiteDataset.open(root)
    source_manifest = dataset.verify_complete_sources()
    completeness_sha256, _ = source_file_digest(dataset.completeness_path)
    scenarios = dataset.scenarios()
    records: list[ITBenchScenarioQualification] = []
    root_kinds: Counter[str] = Counter()
    query_latencies: dict[str, list[float]] = {
        category: []
        for category in ("alerts", "metrics", "k8s_events", "k8s_objects", "logs", "traces")
    }
    trace_id_latencies: list[float] = []
    sparse_indexes: list[dict[str, Any]] = []
    for scenario in scenarios:
        backend = ITBenchSnapshotBackend(dataset, scenario)
        data = dataset.load_investigator_data(scenario)
        registry = ITBenchSnapshotToolRegistry(backend)
        context = registry.public_context()
        if "ground_truth" in context.casefold() or "fault_mechanism" in context.casefold():
            raise ValueError(
                f"ground-truth terminology leaked into context: {scenario.scenario_id}"
            )
        incident, alerts = build_observable_incident(backend)
        if incident.incident_id is None or not alerts:
            raise ValueError(f"observable incident construction failed: {scenario.scenario_id}")
        ground_truth = dataset.load_ground_truth(scenario.scenario_id)
        for group in ground_truth.root_cause_groups:
            if group.root_cause:
                root_kinds[group.kind] += 1
        counts = {
            "alerts": len(data.alerts),
            "metrics": len(data.metrics),
            "k8s_events": len(data.k8s_events),
            "k8s_objects": len(data.k8s_objects),
            "logs": len(data.logs),
            "traces": len(data.traces),
        }
        records.append(
            ITBenchScenarioQualification(
                scenario_id=scenario.scenario_id,
                loaded=True,
                ground_truth_loaded_separately=True,
                evidence_category_counts=counts,
                generated_evidence_ids=(
                    len(data.alerts)
                    + len(data.metrics)
                    + len(data.k8s_events)
                    + len(data.k8s_objects)
                    + len(data.logs)
                    + len(data.traces)
                ),
            )
        )
        for tool_name in (
            "itbench_alerts",
            "itbench_metrics",
            "itbench_kubernetes_events",
            "itbench_kubernetes_objects",
            "itbench_logs",
            "itbench_traces",
        ):
            started = time.perf_counter()
            _ = registry.invoke(tool_name, {"limit": 1})
            category = {
                "itbench_alerts": "alerts",
                "itbench_metrics": "metrics",
                "itbench_kubernetes_events": "k8s_events",
                "itbench_kubernetes_objects": "k8s_objects",
                "itbench_logs": "logs",
                "itbench_traces": "traces",
            }[tool_name]
            query_latencies[category].append(time.perf_counter() - started)
        index_path = trace_index_path(scenario.snapshot_path)
        if index_path.exists():
            with sqlite3.connect(f"file:{index_path}?mode=ro", uri=True) as connection:
                indexed_rows = connection.execute("SELECT COUNT(*) FROM trace_index").fetchone()[0]
                trace_id = connection.execute(
                    "SELECT trace_id FROM trace_index ORDER BY row_index DESC LIMIT 1"
                ).fetchone()[0]
            started = time.perf_counter()
            indexed_response = registry.invoke("itbench_traces", {"trace_id": trace_id, "limit": 1})
            trace_id_latencies.append(time.perf_counter() - started)
            if indexed_response["matching_count"] < 1:
                raise ValueError(f"trace sidecar lookup failed: {scenario.scenario_id}")
            sparse_indexes.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "path": str(index_path),
                    "bytes": index_path.stat().st_size,
                    "indexed_rows": indexed_rows,
                }
            )
        entities_from_k8s_records(data.k8s_objects)
    if tuple(item.scenario_id for item in records) != ITBENCH_SCENARIO_IDS:
        raise ValueError("scenario order or count does not match pinned set")
    payload: dict[str, Any] = {
        "artifact_type": "ITBENCH_LITE_ADAPTER_QUALIFICATION",
        "status": "PASS",
        "benchmark": "ITBench-Lite",
        "source": ITBENCH_SOURCE,
        "revision": ITBENCH_DATASET_REVISION,
        "sre_version": ITBENCH_SRE_VERSION,
        "scenario_count": len(records),
        "scenario_passes": len(records),
        "scenario_failures": 0,
        "official_evaluator_revision": OFFICIAL_EVALUATOR_REVISION,
        "source_file_count": source_manifest["source_file_count"],
        "source_bytes": sum(int(item["byte_count"]) for item in source_manifest["files"]),
        "source_completeness_manifest": ".local/itbench-lite/.itbench-source-completeness.json",
        "source_completeness_sha256": completeness_sha256,
        "source_files": source_manifest["files"],
        "source_coverage": source_manifest["coverage"],
        "full_source_coverage": True,
        "query_latency_seconds": {
            category: _latency_summary(values) for category, values in query_latencies.items()
        },
        "trace_id_index_latency_seconds": _latency_summary(trace_id_latencies),
        "trace_id_sparse_indexes": sparse_indexes,
        "openai_calls": 0,
        "network_calls": 0,
        "investigator_ground_truth_access": False,
        "root_cause_entity_kind_counts": dict(sorted(root_kinds.items())),
        "scenarios": [item.model_dump(mode="json") for item in records],
        "note": "E0 adapter qualification only; no provider or official judge evaluation was run.",
    }
    sparse_manifest = root / ".itbench-sparse-index.json"
    if sparse_manifest.exists():
        sparse_sha256, sparse_bytes = source_file_digest(sparse_manifest)
        payload["sparse_index_manifest"] = str(sparse_manifest)
        payload["sparse_index_manifest_sha256"] = sparse_sha256
        payload["sparse_index_manifest_bytes"] = sparse_bytes
    return payload


def _latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"median": 0.0, "p95": 0.0, "max": 0.0}
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return {
        "median": statistics.median(ordered),
        "p95": ordered[p95_index],
        "max": max(ordered),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    args = parser.parse_args()
    payload = qualify(args.root)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_json_write(args.report, payload)
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    print(
        f"ITBench-Lite adapter qualification: PASS ({payload['scenario_passes']}/35, 0 OpenAI calls)"
    )
    print(f"qualification_sha256={digest}")


if __name__ == "__main__":
    main()
