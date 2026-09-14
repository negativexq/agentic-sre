"""Pinned, ground-truth-separated ITBench-Lite snapshot loading."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from packages.evals.itbench.contracts import (
    InvestigatorData,
    ITBenchEvidenceCategory,
    ITBenchGroundTruth,
    ITBenchGroundTruthGroup,
    ITBenchScenario,
)

ITBENCH_SOURCE = "ibm-research/ITBench-Lite"
ITBENCH_LICENSE = "Apache-2.0"
ITBENCH_SRE_VERSION = "v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7"
ITBENCH_DATASET_REVISION = "d0916b08ba421ce5e672e9ad68aa947d938dfef0"
ITBENCH_SCENARIO_IDS: tuple[str, ...] = (
    "Scenario-1",
    "Scenario-2",
    "Scenario-4",
    "Scenario-5",
    "Scenario-6",
    "Scenario-7",
    "Scenario-8",
    "Scenario-9",
    "Scenario-11",
    "Scenario-12",
    "Scenario-13",
    "Scenario-14",
    "Scenario-15",
    "Scenario-16",
    "Scenario-17",
    "Scenario-18",
    "Scenario-19",
    "Scenario-20",
    "Scenario-21",
    "Scenario-22",
    "Scenario-23",
    "Scenario-24",
    "Scenario-25",
    "Scenario-29",
    "Scenario-31",
    "Scenario-33",
    "Scenario-34",
    "Scenario-35",
    "Scenario-38",
    "Scenario-80",
    "Scenario-81",
    "Scenario-83",
    "Scenario-91",
    "Scenario-102",
    "Scenario-105",
)

_RAW_FILES = {
    ITBenchEvidenceCategory.K8S_EVENTS: "k8s_events_raw.tsv",
    ITBenchEvidenceCategory.K8S_OBJECTS: "k8s_objects_raw.tsv",
    ITBenchEvidenceCategory.LOGS: "otel_logs_raw.tsv",
    ITBenchEvidenceCategory.TRACES: "otel_traces_raw.tsv",
}


class ITBenchDatasetError(ValueError):
    """Raised when a pinned external snapshot is incomplete or malformed."""


def _scenario_number(scenario_id: str) -> int:
    return int(scenario_id.split("-", 1)[1])


class ITBenchLiteDataset:
    """Read-only view of one pinned SRE snapshot directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.snapshot_root = root / "snapshots" / "sre" / ITBENCH_SRE_VERSION
        self.manifest_path = root / ".itbench-lite-manifest.json"

    @classmethod
    def open(cls, root: str | Path) -> ITBenchLiteDataset:
        """Open and validate the local pinned dataset manifest."""
        dataset = cls(Path(root))
        if not dataset.manifest_path.exists():
            raise ITBenchDatasetError(f"dataset manifest is missing: {dataset.manifest_path}")
        try:
            manifest = json.loads(dataset.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ITBenchDatasetError("dataset manifest is not valid JSON") from error
        if manifest.get("source") != ITBENCH_SOURCE:
            raise ITBenchDatasetError("dataset source is not the pinned IBM Research source")
        if manifest.get("revision") != ITBENCH_DATASET_REVISION:
            raise ITBenchDatasetError("dataset revision does not match the pinned revision")
        if manifest.get("sre_version") != ITBENCH_SRE_VERSION:
            raise ITBenchDatasetError("SRE snapshot version does not match the pinned version")
        if tuple(manifest.get("scenario_ids", ())) != ITBENCH_SCENARIO_IDS:
            raise ITBenchDatasetError("scenario list does not match the pinned 35-scenario set")
        return dataset

    def scenarios(self) -> tuple[ITBenchScenario, ...]:
        """Discover all 35 scenarios without opening ground truth."""
        if not self.snapshot_root.is_dir():
            raise ITBenchDatasetError(f"SRE snapshot directory is missing: {self.snapshot_root}")
        scenarios = tuple(self._load_scenario(scenario_id) for scenario_id in ITBENCH_SCENARIO_IDS)
        if len(scenarios) != 35:
            raise ITBenchDatasetError("expected exactly 35 SRE scenarios")
        return scenarios

    def _load_scenario(self, scenario_id: str) -> ITBenchScenario:
        path = self.snapshot_root / scenario_id
        if not path.is_dir():
            raise ITBenchDatasetError(f"scenario directory is missing: {scenario_id}")
        evidence_files: dict[ITBenchEvidenceCategory, tuple[str, ...]] = {}
        alert_files = sorted((path / "alerts").glob("*.json")) if (path / "alerts").is_dir() else []
        single_alerts = sorted(path.glob("alerts_in_alerting_state_*.json"))
        if alert_files or single_alerts:
            evidence_files[ITBenchEvidenceCategory.ALERTS] = tuple(
                str(item.relative_to(path)) for item in (single_alerts[-1:] or alert_files[-1:])
            )
        metrics = sorted((path / "metrics").glob("*.tsv")) if (path / "metrics").is_dir() else []
        if metrics:
            evidence_files[ITBenchEvidenceCategory.METRICS] = tuple(
                str(item.relative_to(path)) for item in metrics
            )
        for category, filename in _RAW_FILES.items():
            candidate = path / filename
            if candidate.exists():
                evidence_files[category] = (filename,)
        missing = [
            category.value for category in ITBenchEvidenceCategory if category not in evidence_files
        ]
        if missing:
            raise ITBenchDatasetError(f"{scenario_id} is missing evidence categories: {missing}")
        return ITBenchScenario(
            scenario_id=scenario_id,
            snapshot_path=str(path),
            evidence_categories=tuple(ITBenchEvidenceCategory),
            evidence_files=evidence_files,
        )

    def load_ground_truth(self, scenario_id: str) -> ITBenchGroundTruth:
        """Load evaluator-only ground truth; never called by investigator loaders."""
        if scenario_id not in ITBENCH_SCENARIO_IDS:
            raise ITBenchDatasetError(f"unknown pinned scenario: {scenario_id}")
        path = self.snapshot_root / scenario_id / "ground_truth.yaml"
        if not path.exists():
            raise ITBenchDatasetError(f"ground truth is missing: {scenario_id}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ITBenchDatasetError(f"ground truth is not an object: {scenario_id}")
        payload = data.get("spec", data)
        if not isinstance(payload, dict):
            raise ITBenchDatasetError(f"ground truth spec is not an object: {scenario_id}")
        groups = tuple(_parse_group(item) for item in payload.get("groups", ()))
        if not groups or not any(item.root_cause for item in groups):
            raise ITBenchDatasetError(f"ground truth has no root-cause group: {scenario_id}")
        aliases = tuple(
            tuple(str(value) for value in group)
            for group in payload.get("aliases", ())
            if isinstance(group, list)
        )
        return ITBenchGroundTruth(
            scenario_id=scenario_id,
            root_cause_groups=groups,
            aliases=aliases,
            alerts=_bounded_objects(payload.get("alerts", ()), 100),
            faults=_bounded_objects(payload.get("fault", ()), 100),
            propagations=_bounded_objects(payload.get("propagations", ()), 200),
        )

    def load_investigator_data(
        self,
        scenario: ITBenchScenario,
        *,
        max_rows: int = 50,
        max_bytes: int = 100_000,
    ) -> InvestigatorData:
        """Load bounded observable snapshot data, intentionally excluding GT."""
        from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

        backend = ITBenchSnapshotBackend(self, scenario, max_rows=max_rows, max_bytes=max_bytes)
        return backend.investigator_data()


def _bounded_objects(value: Any, maximum: int) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value[:maximum] if isinstance(item, dict))


def _parse_group(value: Any) -> ITBenchGroundTruthGroup:
    if not isinstance(value, dict):
        raise ITBenchDatasetError("ground-truth group is not an object")
    group_id = value.get("id", value.get("group_id"))
    kind = value.get("kind")
    if not isinstance(group_id, str) or not group_id:
        raise ITBenchDatasetError("ground-truth group has no valid ID")
    if not isinstance(kind, str) or not kind:
        raise ITBenchDatasetError("ground-truth group has no valid kind")
    filters = value.get("filter", ())
    if isinstance(filters, str):
        filters = (filters,)
    if not isinstance(filters, (list, tuple)):
        filters = ()
    if not all(isinstance(item, str) for item in filters):
        raise ITBenchDatasetError("ground-truth group filters must be strings")
    namespace = value.get("namespace")
    name = value.get("name")
    if namespace is not None and not isinstance(namespace, str):
        raise ITBenchDatasetError("ground-truth group namespace must be a string")
    if name is not None and not isinstance(name, str):
        raise ITBenchDatasetError("ground-truth group name must be a string")
    return ITBenchGroundTruthGroup(
        group_id=group_id,
        kind=kind,
        namespace=namespace or None,
        name=name or None,
        filters=tuple(str(item) for item in filters),
        root_cause=bool(value.get("root_cause", False)),
    )


def parse_tsv_prefix(path: Path, *, max_rows: int, max_bytes: int) -> tuple[dict[str, Any], ...]:
    """Parse a bounded TSV prefix without loading multi-gigabyte snapshots."""
    raw = path.read_bytes()[:max_bytes]
    if not raw:
        return ()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return ()
    reader = csv.DictReader(lines, delimiter="\t")
    records: list[dict[str, Any]] = []
    for row in reader:
        if len(records) >= max_rows:
            break
        records.append({str(key): value for key, value in row.items() if key is not None})
    return tuple(records)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse the common ITBench timestamp forms without changing source data."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = [
    "ITBENCH_DATASET_REVISION",
    "ITBENCH_LICENSE",
    "ITBENCH_SCENARIO_IDS",
    "ITBENCH_SOURCE",
    "ITBENCH_SRE_VERSION",
    "ITBenchDatasetError",
    "ITBenchLiteDataset",
    "parse_timestamp",
    "parse_tsv_prefix",
]
