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

from packages.evals.itbench.contracts import (
    InvestigatorData,
    ITBenchEntity,
    ITBenchEvidenceCategory,
    parse_canonical_entity,
)
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

    def query_entity_context(self, entity: str, limit: int) -> dict[str, Any]:
        """Resolve one canonical entity using structured observable fields."""
        parsed = parse_canonical_entity(entity)
        objects = [
            item
            for item in self._iter_records(ITBenchEvidenceCategory.K8S_OBJECTS)
            if _record_entity(item) == parsed
        ]
        events = [
            item
            for item in self._iter_records(ITBenchEvidenceCategory.K8S_EVENTS)
            if _record_entity(item) == parsed
        ]
        object_records = _fit_bounded_records(tuple(objects[:limit]), self.max_bytes)
        event_records = _fit_bounded_records(tuple(events[:limit]), self.max_bytes)
        return {
            "entity": parsed.canonical,
            "object_records": list(object_records),
            "event_records": list(event_records),
            "object_matching_count": len(objects),
            "event_matching_count": len(events),
            "truncated": len(objects) > len(object_records) or len(events) > len(event_records),
            "topology": [
                edge
                for edge in self.topology(limit=limit * 2)
                if edge.get("source") == parsed.canonical or edge.get("target") == parsed.canonical
            ],
        }

    def metric_aggregate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Compute numeric metric statistics over every matching source row.

        Model responses remain bounded by :meth:`query`; aggregation is a
        backend operation and never exposes a query language to the model.
        """
        values: list[float] = []
        for item in self._iter_records(ITBenchEvidenceCategory.METRICS):
            if not _matches(
                item,
                pattern=arguments.get("pattern"),
                service=arguments.get("service"),
                namespace=arguments.get("namespace"),
                trace_id=None,
            ):
                continue
            record = item.get("record", {})
            if not isinstance(record, dict):
                continue
            for key in ("Value", "value", "metric_value"):
                try:
                    if key in record:
                        values.append(float(record[key]))
                        break
                except (TypeError, ValueError):
                    continue
        return {
            "count": len(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "mean": sum(values) / len(values) if values else None,
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
            "delta": values[-1] - values[0] if len(values) > 1 else None,
        }

    def metric_analysis(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Scan matching metrics once for both aggregates and bounded samples."""
        limit = arguments.get("limit", self.max_rows)
        if not isinstance(limit, int) or not 1 <= limit <= self.max_rows:
            raise ValueError("limit must be within the bounded snapshot query limit")
        selected: list[dict[str, Any]] = []
        values: list[float] = []
        matching_count = 0
        for item in self._iter_records(ITBenchEvidenceCategory.METRICS):
            if not _matches(
                item,
                pattern=arguments.get("pattern"),
                service=arguments.get("service"),
                namespace=arguments.get("namespace"),
                trace_id=None,
            ):
                continue
            matching_count += 1
            if len(selected) < limit:
                selected.append(item)
            record = item.get("record", {})
            if not isinstance(record, dict):
                continue
            for key in ("Value", "value", "metric_value"):
                try:
                    if key in record:
                        values.append(float(record[key]))
                        break
                except (TypeError, ValueError):
                    continue
        bounded = _fit_bounded_records(tuple(selected), self.max_bytes)
        return {
            "records": list(bounded),
            "category": ITBenchEvidenceCategory.METRICS.value,
            "scenario_id": self.scenario.scenario_id,
            "matching_count": matching_count,
            "returned_count": len(bounded),
            "truncated": matching_count > len(bounded),
            "aggregate": {
                "count": len(values),
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "mean": sum(values) / len(values) if values else None,
                "first": values[0] if values else None,
                "last": values[-1] if values else None,
                "delta": values[-1] - values[0] if len(values) > 1 else None,
            },
            "sample_count": len(bounded),
            "sample_truncated": matching_count > len(bounded),
        }

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

    def observable_entities(self) -> tuple[dict[str, str], ...]:
        """Return the complete deduplicated Kubernetes entity catalog."""
        entities: dict[str, dict[str, str]] = {}
        for category in (ITBenchEvidenceCategory.K8S_OBJECTS, ITBenchEvidenceCategory.K8S_EVENTS):
            for item in self._iter_records(category):
                record = item.get("record", {})
                body = record.get("Body") if isinstance(record, dict) else None
                parsed = _json_object(body)
                if not parsed:
                    parsed = record if isinstance(record, dict) else {}
                candidates = [parsed]
                obj = parsed.get("object") if isinstance(parsed, dict) else None
                if isinstance(obj, dict):
                    candidates.append(obj)
                involved = parsed.get("involvedObject") if isinstance(parsed, dict) else None
                if isinstance(involved, dict):
                    candidates.append({"kind": involved.get("kind"), "metadata": involved})
                for candidate in candidates:
                    metadata = candidate.get("metadata")
                    if not isinstance(metadata, dict):
                        continue
                    kind, name = candidate.get("kind"), metadata.get("name")
                    if not isinstance(kind, str) or not isinstance(name, str):
                        continue
                    namespace = metadata.get("namespace")
                    entity = {
                        "namespace": namespace if isinstance(namespace, str) else "_cluster",
                        "kind": kind,
                        "name": name,
                    }
                    entities[f"{entity['namespace']}/{kind}/{name}"] = entity
        return tuple(entities[key] for key in sorted(entities))

    def topology(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        """Derive bounded structural edges from observable Kubernetes objects only."""
        edges: set[tuple[str, str, str]] = set()
        objects: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for item in self._iter_records(ITBenchEvidenceCategory.K8S_OBJECTS):
            record = item.get("record", {})
            body = _json_object(record.get("Body")) if isinstance(record, dict) else None
            if not body:
                continue
            metadata = body.get("metadata")
            if not isinstance(metadata, dict):
                continue
            kind, name = body.get("kind"), metadata.get("name")
            if not isinstance(kind, str) or not isinstance(name, str):
                continue
            namespace = (
                metadata.get("namespace")
                if isinstance(metadata.get("namespace"), str)
                else "_cluster"
            )
            source = f"{namespace}/{kind}/{name}"
            objects.append((source, body, metadata))
            owners = metadata.get("ownerReferences", [])
            if isinstance(owners, list):
                for owner in owners:
                    if (
                        isinstance(owner, dict)
                        and isinstance(owner.get("kind"), str)
                        and isinstance(owner.get("name"), str)
                    ):
                        edges.add(
                            (
                                source,
                                f"{namespace}/{owner['kind']}/{owner['name']}",
                                "owner",
                            )
                        )
        services = [
            (source, body, metadata)
            for source, body, metadata in objects
            if body.get("kind") == "Service"
            and isinstance(body.get("spec"), dict)
            and isinstance(body["spec"].get("selector"), dict)
        ]
        pods_by_namespace: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for pod_source, pod_body, pod_metadata in objects:
            if pod_body.get("kind") != "Pod":
                continue
            namespace = str(pod_metadata.get("namespace", "_cluster"))
            labels = pod_metadata.get("labels", {})
            if isinstance(labels, dict):
                pods_by_namespace.setdefault(namespace, []).append((pod_source, labels))
        for service_source, service_body, service_metadata in services:
            selector = service_body["spec"].get("selector", {})
            service_namespace = service_metadata.get("namespace", "_cluster")
            candidates = pods_by_namespace.get(str(service_namespace), [])
            for pod_source, labels in candidates:
                if all(labels.get(key) == value for key, value in selector.items()):
                    edges.add((service_source, pod_source, "selector"))
        for source, body, metadata in objects:
            if body.get("kind") == "Pod":
                node_name = (
                    body.get("spec", {}).get("nodeName")
                    if isinstance(body.get("spec"), dict)
                    else None
                )
                if isinstance(node_name, str):
                    namespace = metadata.get("namespace", "_cluster")
                    edges.add((source, f"_cluster/Node/{node_name}", "scheduled_on"))
            spec = body.get("spec")
            if not isinstance(spec, dict):
                continue
            template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
            pod_spec = template.get("spec") if isinstance(template, dict) else None
            if isinstance(pod_spec, dict):
                for ref in (*pod_spec.get("volumes", []), *pod_spec.get("containers", [])):
                    if not isinstance(ref, dict):
                        continue
                    refs = []
                    config = ref.get("configMap")
                    if isinstance(config, dict):
                        refs.append(("ConfigMap", config.get("name")))
                    for env_from in ref.get("envFrom", []):
                        if isinstance(env_from, dict) and isinstance(
                            env_from.get("configMapRef"), dict
                        ):
                            refs.append(("ConfigMap", env_from["configMapRef"].get("name")))
                    for ref_kind, ref_name in refs:
                        if isinstance(ref_name, str):
                            namespace = metadata.get("namespace", "_cluster")
                            edges.add(
                                (
                                    source,
                                    f"{namespace}/{ref_kind}/{ref_name}",
                                    "configuration_reference",
                                )
                            )
        return tuple(
            {"source": source, "target": target, "relationship": relationship}
            for source, target, relationship in sorted(edges)[:limit]
        )

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


def _json_object(value: Any) -> dict[str, Any] | None:
    """Decode an embedded JSON object without accepting executable content."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _record_entity(item: dict[str, Any]) -> ITBenchEntity | None:
    """Extract an entity from an observable object or event record."""
    record = item.get("record", {})
    if not isinstance(record, dict):
        return None
    body = _json_object(record.get("Body"))
    if body is None:
        body = record
    candidate = body.get("object") if isinstance(body.get("object"), dict) else body
    involved = body.get("involvedObject") if isinstance(body.get("involvedObject"), dict) else None
    if involved is not None:
        candidate = {"kind": involved.get("kind"), "metadata": involved}
    if not isinstance(candidate, dict):
        return None
    metadata = candidate.get("metadata")
    kind = candidate.get("kind")
    if not isinstance(metadata, dict) or not isinstance(kind, str):
        return None
    name = metadata.get("name")
    if not isinstance(name, str):
        return None
    namespace = metadata.get("namespace")
    try:
        return ITBenchEntity(
            namespace=namespace if isinstance(namespace, str) else None,
            kind=kind,
            name=name,
        )
    except ValueError:
        return None


def _matches(
    item: dict[str, Any], *, pattern: Any, service: Any, namespace: Any, trace_id: Any
) -> bool:
    record = item.get("record")
    if not any(isinstance(value, str) for value in (pattern, service, namespace, trace_id)):
        return True
    if isinstance(trace_id, str):
        if not isinstance(record, dict) or str(record.get("TraceId", "")) != trace_id:
            return False
    if isinstance(record, dict):
        if isinstance(service, str):
            service_values = [record.get(key) for key in ("ServiceName", "service", "service.name")]
            if any(
                isinstance(value, str) and service.casefold() == value.casefold()
                for value in service_values
            ):
                service = None
            elif any(value is not None for value in service_values):
                return False
        if isinstance(namespace, str):
            namespace_values = [
                record.get(key) for key in ("Namespace", "namespace", "k8s.namespace.name")
            ]
            if any(
                isinstance(value, str) and namespace.casefold() == value.casefold()
                for value in namespace_values
            ):
                namespace = None
            elif any(value is not None for value in namespace_values):
                return False
        if not any(isinstance(value, str) for value in (pattern, service, namespace)):
            return True
    record_text = json.dumps(record, sort_keys=True, default=str).casefold()
    if isinstance(pattern, str) and pattern.casefold() not in record_text:
        return False
    if isinstance(service, str) and service.casefold() not in record_text:
        return False
    if isinstance(namespace, str) and namespace.casefold() not in record_text:
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
