"""Bounded read-only tools over immutable ITBench-Lite snapshot files."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from hashlib import sha256
from itertools import islice
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from packages.evals.itbench.contracts import InvestigatorData, ITBenchEvidenceCategory
from packages.evals.itbench.dataset import ITBenchLiteDataset, iter_tsv
from packages.evals.itbench.sparse_index import trace_index_path

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
        result = tuple(islice(self._iter_records(category), self.max_rows))
        self._cache[category] = result
        return result

    def query(self, category: ITBenchEvidenceCategory, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a named, bounded query; arbitrary filesystem access is impossible."""
        selected: list[dict[str, Any]] = []
        matching_count = 0
        pattern = arguments.get("pattern")
        service = arguments.get("service")
        namespace = arguments.get("namespace")
        trace_id = arguments.get("trace_id")
        limit = arguments.get("limit", self.max_rows)
        if not isinstance(limit, int) or not 1 <= limit <= self.max_rows:
            raise ValueError("limit must be within the bounded snapshot query limit")
        if (
            category is ITBenchEvidenceCategory.TRACES
            and isinstance(trace_id, str)
            and not any(isinstance(arguments.get(key), str) for key in ("pattern", "service"))
        ):
            indexed = self._query_trace_index(trace_id, limit)
            if indexed is not None:
                return indexed
        for item in self._iter_records(category):
            if _matches(
                item, pattern=pattern, service=service, namespace=namespace, trace_id=trace_id
            ):
                matching_count += 1
                if len(selected) < limit:
                    selected.append(item)
        bounded = _fit_bounded_records(tuple(selected), self.max_bytes)
        response = {
            "records": list(bounded),
            "category": category.value,
            "scenario_id": self.scenario.scenario_id,
            "matching_count": matching_count,
            "returned_count": len(bounded),
            "truncated": matching_count > len(bounded),
        }
        if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > self.max_bytes:
            response["records"] = [{"evidence_id": item.get("evidence_id")} for item in bounded[:1]]
            response["returned_count"] = len(response["records"])
        return response

    def _query_trace_index(self, trace_id: str, limit: int) -> dict[str, Any] | None:
        index_path = trace_index_path(self.scenario.snapshot_path)
        if not index_path.exists():
            return None
        source_file = self.scenario.evidence_files[ITBenchEvidenceCategory.TRACES][0]
        source_path = Path(self.scenario.snapshot_path) / source_file
        with sqlite3.connect(f"file:{index_path}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT row_index, byte_offset, byte_length FROM trace_index "
                "WHERE trace_id = ? ORDER BY row_index LIMIT ?",
                (trace_id, limit),
            ).fetchall()
            matching_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM trace_index WHERE trace_id = ?", (trace_id,)
                ).fetchone()[0]
            )
        with source_path.open("rb") as stream:
            header = next(csv.reader([stream.readline().decode("utf-8")], delimiter="\t"))
            selected: list[dict[str, Any]] = []
            for row_index, offset, length in rows:
                stream.seek(offset)
                values = next(csv.reader([stream.read(length).decode("utf-8")], delimiter="\t"))
                record = dict(zip(header, values, strict=False))
                selected.append(
                    {
                        "evidence_id": str(
                            self.evidence_id(
                                ITBenchEvidenceCategory.TRACES, source_file, int(row_index)
                            )
                        ),
                        "category": ITBenchEvidenceCategory.TRACES.value,
                        "source_file": source_file,
                        "row_index": int(row_index),
                        "record": record,
                    }
                )
        bounded = _fit_bounded_records(tuple(selected), self.max_bytes)
        return {
            "records": list(bounded),
            "category": ITBenchEvidenceCategory.TRACES.value,
            "scenario_id": self.scenario.scenario_id,
            "matching_count": matching_count,
            "returned_count": len(bounded),
            "truncated": matching_count > len(bounded),
        }

    def complete_source_records(
        self, category: ITBenchEvidenceCategory
    ) -> Iterator[dict[str, Any]]:
        """Iterate every valid source record for qualification and bounded queries."""
        return self._iter_records(category)

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

    def _iter_records(self, category: ITBenchEvidenceCategory) -> Iterator[dict[str, Any]]:
        root = Path(self.scenario.snapshot_path)
        for relative_path in self.scenario.evidence_files[category]:
            path = root / relative_path
            if category is ITBenchEvidenceCategory.ALERTS:
                source: Iterator[dict[str, Any]] = iter(self._read_alerts(path))
            else:
                source = iter_tsv(path)
            for index, row in enumerate(source):
                yield {
                    "evidence_id": str(self.evidence_id(category, relative_path, index)),
                    "category": category.value,
                    "source_file": relative_path,
                    "row_index": index,
                    "record": row,
                }

    def _read_alerts(self, path: Path) -> list[dict[str, Any]]:
        """Read raw alert objects; evidence envelopes are added exactly once."""
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
        for item in source:
            if not isinstance(item, dict):
                continue
            result.append(item)
        return result


def _matches(
    item: dict[str, Any], *, pattern: Any, service: Any, namespace: Any, trace_id: Any
) -> bool:
    record = item.get("record")
    if isinstance(trace_id, str):
        if not isinstance(record, dict) or str(record.get("TraceId", "")) != trace_id:
            return False
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
