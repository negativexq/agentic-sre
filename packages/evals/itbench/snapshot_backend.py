"""Bounded read-only tools over immutable ITBench-Lite snapshot files."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from packages.evals.itbench.contracts import InvestigatorData, ITBenchEvidenceCategory
from packages.evals.itbench.dataset import ITBenchLiteDataset, parse_tsv_prefix

_EVIDENCE_NAMESPACE = UUID("2e6bbd95-4c2a-47d5-8b7c-cf19ccf9a3c4")


class ITBenchSnapshotBackend:
    """Read bounded records and issue runtime-owned deterministic evidence IDs."""

    def __init__(
        self,
        dataset: ITBenchLiteDataset,
        scenario: Any,
        *,
        max_rows: int = 50,
        max_bytes: int = 100_000,
    ) -> None:
        self.dataset = dataset
        self.scenario = scenario
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self._cache: dict[ITBenchEvidenceCategory, tuple[dict[str, Any], ...]] = {}

    def investigator_data(self) -> InvestigatorData:
        """Build a bounded public data object with no ground-truth reference."""
        return InvestigatorData(
            scenario=self.scenario,
            alerts=self.records(ITBenchEvidenceCategory.ALERTS),
            metrics=self.records(ITBenchEvidenceCategory.METRICS),
            k8s_events=self.records(ITBenchEvidenceCategory.K8S_EVENTS),
            k8s_objects=self.records(ITBenchEvidenceCategory.K8S_OBJECTS),
            logs=self.records(ITBenchEvidenceCategory.LOGS),
            traces=self.records(ITBenchEvidenceCategory.TRACES),
        )

    def records(self, category: ITBenchEvidenceCategory) -> tuple[dict[str, Any], ...]:
        """Return bounded normalized records for one published evidence category."""
        if category in self._cache:
            return self._cache[category]
        records: list[dict[str, Any]] = []
        root = Path(self.scenario.snapshot_path)
        for relative_path in self.scenario.evidence_files[category]:
            path = root / relative_path
            if category is ITBenchEvidenceCategory.ALERTS:
                records.extend(self._read_alerts(path))
            else:
                for index, row in enumerate(
                    parse_tsv_prefix(path, max_rows=self.max_rows, max_bytes=self.max_bytes)
                ):
                    records.append(
                        {
                            "evidence_id": str(self.evidence_id(category, relative_path, index)),
                            "category": category.value,
                            "source_file": relative_path,
                            "row_index": index,
                            "record": row,
                        }
                    )
                    if len(records) >= self.max_rows:
                        break
            if len(records) >= self.max_rows:
                break
        result = tuple(records[: self.max_rows])
        self._cache[category] = result
        return result

    def query(self, category: ITBenchEvidenceCategory, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a named, bounded query; arbitrary filesystem access is impossible."""
        records = self.records(category)
        pattern = arguments.get("pattern")
        service = arguments.get("service")
        namespace = arguments.get("namespace")
        filtered = tuple(
            item
            for item in records
            if _matches(item, pattern=pattern, service=service, namespace=namespace)
        )
        limit = arguments.get("limit", self.max_rows)
        if not isinstance(limit, int) or not 1 <= limit <= self.max_rows:
            raise ValueError("limit must be within the bounded snapshot query limit")
        selected = _fit_bounded_records(filtered[:limit], self.max_bytes)
        return {
            "records": list(selected),
            "category": category.value,
            "scenario_id": self.scenario.scenario_id,
            "result_count": len(selected),
        }

    def evidence_id(self, category: ITBenchEvidenceCategory, source_file: str, row: int) -> str:
        """Return an ID derived from pinned scenario content location, not ground truth."""
        identity = f"{self.scenario.scenario_id}|{category.value}|{source_file}|{row}"
        return str(uuid5(_EVIDENCE_NAMESPACE, identity))

    def snapshot_hash(self) -> str:
        """Hash the bounded source manifest identity used by this backend."""
        encoded = "|".join(
            f"{category.value}:{','.join(self.scenario.evidence_files[category])}"
            for category in ITBenchEvidenceCategory
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _read_alerts(path: Path) -> list[dict[str, Any]]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, list):
            source = value
        elif isinstance(value, dict) and isinstance(value.get("alerts"), list):
            source = value["alerts"]
        elif (
            isinstance(value, dict)
            and isinstance(value.get("data"), dict)
            and isinstance(value["data"].get("alerts"), list)
        ):
            source = value["data"]["alerts"]
        else:
            source = [value]
        result: list[dict[str, Any]] = []
        for index, item in enumerate(source):
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "evidence_id": str(uuid5(_EVIDENCE_NAMESPACE, f"alert|{path}|{index}")),
                    "category": ITBenchEvidenceCategory.ALERTS.value,
                    "source_file": path.name,
                    "row_index": index,
                    "record": item,
                }
            )
        return result


def _matches(item: dict[str, Any], *, pattern: Any, service: Any, namespace: Any) -> bool:
    encoded = json.dumps(item, sort_keys=True, default=str).casefold()
    if isinstance(pattern, str) and pattern.casefold() not in encoded:
        return False
    if isinstance(service, str) and service.casefold() not in encoded:
        return False
    if isinstance(namespace, str) and namespace.casefold() not in encoded:
        return False
    return True


def _fit_bounded_records(
    records: tuple[dict[str, Any], ...], max_bytes: int
) -> tuple[dict[str, Any], ...]:
    """Keep a tool response bounded when a source row is unusually large."""
    selected: list[dict[str, Any]] = []
    for record in records:
        candidate = selected + [record]
        encoded = json.dumps(
            {"records": candidate}, ensure_ascii=False, sort_keys=True, default=str
        )
        if len(encoded.encode("utf-8")) > max_bytes:
            break
        selected.append(record)
    if selected or not records:
        return tuple(selected)
    record = records[0]
    return (
        {
            "evidence_id": record.get("evidence_id"),
            "category": record.get("category"),
            "source_file": record.get("source_file"),
            "row_index": record.get("row_index"),
            "record": "[bounded source record omitted: exceeds tool response limit]",
        },
    )


__all__ = ["ITBenchSnapshotBackend"]
