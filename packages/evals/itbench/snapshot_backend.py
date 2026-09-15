"""Bounded read-only tools over immutable ITBench-Lite snapshot files."""

from __future__ import annotations

import csv
import json
import re
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
        self._candidate_cache: tuple[dict[str, Any], ...] | None = None
        self._topology_cache: dict[tuple[Any, ...], tuple[dict[str, Any], ...]] = {}
        self._entity_record_index: dict[str, tuple[dict[str, Any], ...]] | None = None
        self._observable_entities_cache: tuple[dict[str, str], ...] | None = None
        self._complete_alert_cache: tuple[dict[str, Any], ...] | None = None
        self._performance = {
            "source_file_scans": 0,
            "records_scanned": 0,
            "entity_index_lookups": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

    def performance_snapshot(self) -> dict[str, int]:
        """Return bounded deterministic backend-work counters for qualification."""
        return dict(self._performance)

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
        pattern = arguments.get("pattern", arguments.get("contains"))
        service = arguments.get("service")
        namespace = arguments.get("namespace")
        trace_id = arguments.get("trace_id")
        entity = arguments.get("entity")
        severity = arguments.get("severity")
        status = arguments.get("status")
        reason = arguments.get("reason")
        event_type = arguments.get("type")
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
                item,
                pattern=pattern,
                service=service,
                namespace=namespace,
                trace_id=trace_id,
                entity=entity,
                severity=severity,
                status=status,
                reason=reason,
                event_type=event_type,
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

    def query_entity_context(
        self, entity: str, limit: int, *, include_telemetry: bool = True
    ) -> dict[str, Any]:
        """Resolve one canonical entity using structured observable fields."""
        parsed = parse_canonical_entity(entity)
        index = self._get_entity_record_index()
        self._performance["entity_index_lookups"] += 1
        objects = list(
            index.get(f"{ITBenchEvidenceCategory.K8S_OBJECTS.value}:{parsed.canonical}", ())
        )
        events = list(
            index.get(f"{ITBenchEvidenceCategory.K8S_EVENTS.value}:{parsed.canonical}", ())
        )
        object_records = _fit_bounded_records(tuple(objects[:limit]), self.max_bytes)
        event_records = _fit_bounded_records(tuple(events[:limit]), self.max_bytes)
        topology = list(self.topology(entity=parsed.canonical, limit=limit))
        object_summaries = [_summarize_k8s_object(item) for item in object_records]
        event_summaries = [_summarize_k8s_event(item) for item in event_records]
        related_text = f"{parsed.namespace or '_cluster'} {parsed.kind} {parsed.name}"
        related_alerts = []
        for alert in self.complete_source_records(ITBenchEvidenceCategory.ALERTS):
            if (
                related_text.casefold()
                in json.dumps(alert.get("record", {}), default=str).casefold()
                or parsed.name.casefold()
                in json.dumps(alert.get("record", {}), default=str).casefold()
            ):
                related_alerts.append(alert)
        # Keep the semantic pack bounded; focused telemetry remains available
        # through dedicated tools instead of triggering three full scans here.
        metric_anomalies: dict[str, Any] = {
            "data_available": "not_scanned",
            "query_scope": "dedicated_metric_analysis",
        }
        log_summary = (
            self._sample_log_summary(parsed, min(limit, 8))
            if include_telemetry
            else {"patterns": [], "data_available": "unknown", "query_scope": "not_scanned"}
        )
        trace_summary = (
            self._sample_trace_summary(parsed, min(limit, 8))
            if include_telemetry
            else {"records": [], "data_available": "unknown", "query_scope": "not_scanned"}
        )
        return {
            "entity": parsed.canonical,
            "object_records": list(object_records),
            "event_records": list(event_records),
            "identity": {
                "namespace": parsed.namespace or "_cluster",
                "kind": parsed.kind,
                "name": parsed.name,
            },
            "object_state": object_summaries,
            "ownership": [edge for edge in topology if edge["relationship"] == "owner"],
            "configuration_dependencies": [
                edge for edge in topology if edge["relationship"] == "configuration_reference"
            ],
            "topology": topology,
            "events_summary": event_summaries,
            "related_alerts": [normalize_alert for normalize_alert in related_alerts[:limit]],
            "metric_anomalies": metric_anomalies,
            "log_error_patterns": log_summary.get("patterns", []),
            "trace_error_summary": trace_summary.get("records", []),
            "backend_operations": [
                "object_lookup",
                "event_lookup",
                "topology",
                "metric_summary",
                *(("log_summary", "trace_summary") if include_telemetry else ()),
            ],
            "data_quality": {
                "data_available": bool(object_records or event_records or topology),
                "object_source_count": len(objects),
                "event_source_count": len(events),
                "topology_returned_count": len(topology),
                "truncated": len(objects) > len(object_records) or len(events) > len(event_records),
            },
            "object_matching_count": len(objects),
            "event_matching_count": len(events),
            "truncated": len(objects) > len(object_records) or len(events) > len(event_records),
            "telemetry_availability": {
                "logs": log_summary.get("data_available"),
                "traces": trace_summary.get("data_available"),
            },
        }

    def _get_entity_record_index(self) -> dict[str, tuple[dict[str, Any], ...]]:
        if self._entity_record_index is not None:
            self._performance["cache_hits"] += 1
            return self._entity_record_index
        self._performance["cache_misses"] += 1
        grouped: dict[str, list[dict[str, Any]]] = {}
        for category in (ITBenchEvidenceCategory.K8S_OBJECTS, ITBenchEvidenceCategory.K8S_EVENTS):
            for item in self._iter_records(category):
                entity = _record_entity(item)
                if entity is not None:
                    grouped.setdefault(f"{category.value}:{entity.canonical}", []).append(item)
        self._entity_record_index = {key: tuple(value) for key, value in grouped.items()}
        return self._entity_record_index

    def _sample_log_summary(self, parsed: ITBenchEntity, limit: int) -> dict[str, Any]:
        items = [
            item
            for item in self.records(ITBenchEvidenceCategory.LOGS)
            if parsed.name.casefold() in json.dumps(item.get("record", {}), default=str).casefold()
        ]
        return {"patterns": items[:limit], "data_available": bool(items)}

    def _sample_trace_summary(self, parsed: ITBenchEntity, limit: int) -> dict[str, Any]:
        items = [
            item
            for item in self.records(ITBenchEvidenceCategory.TRACES)
            if parsed.name.casefold() in json.dumps(item.get("record", {}), default=str).casefold()
        ]
        return {"records": items[:limit], "data_available": bool(items)}

    def metric_aggregate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Compute numeric metric statistics over every matching source row.

        Model responses remain bounded by :meth:`query`; aggregation is a
        backend operation and never exposes a query language to the model.
        """
        values: list[float] = []
        for item in self._iter_metric_records(arguments):
            if not _metric_matches(item, arguments):
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
        values_by_metric: dict[str, list[float]] = {}
        timestamps_by_metric: dict[str, list[str]] = {}
        metric_identity_fields: dict[str, dict[str, Any]] = {}
        matching_count = 0
        for item in self._iter_metric_records(arguments):
            if not _metric_matches(item, arguments):
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
                        value = float(record[key])
                        values.append(value)
                        metric_name = str(
                            record.get("metric_name", record.get("MetricName", "unknown"))
                        )
                        metric_service = str(
                            record.get(
                                "service_name", record.get("service", record.get("ServiceName", ""))
                            )
                        )
                        metric_namespace = str(record.get("namespace", record.get("Namespace", "")))
                        label_identity = _metric_label_identity(record)
                        metric_key = "|".join(
                            (metric_name, metric_service, metric_namespace, label_identity)
                        )
                        values_by_metric.setdefault(metric_key, []).append(value)
                        metric_identity_fields[metric_key] = {
                            "metric_name": metric_name,
                            "service": metric_service or None,
                            "namespace": metric_namespace or None,
                            "label_identity": label_identity,
                        }
                        timestamp = record.get("timestamp", record.get("Timestamp"))
                        if isinstance(timestamp, str):
                            timestamps_by_metric.setdefault(metric_key, []).append(timestamp)
                        break
                except (TypeError, ValueError):
                    continue
        bounded = _fit_bounded_records(tuple(selected), self.max_bytes)
        all_aggregates_by_metric = {
            name: {
                **metric_identity_fields[name],
                **_metric_summary(metric_values, tuple(timestamps_by_metric.get(name, ()))),
            }
            for name, metric_values in sorted(values_by_metric.items())
        }
        ranked_metric_names = sorted(
            all_aggregates_by_metric,
            key=lambda name: (
                -abs(float(all_aggregates_by_metric[name].get("relative_change") or 0.0)),
                -abs(float(all_aggregates_by_metric[name].get("absolute_delta") or 0.0)),
                name,
            ),
        )
        bounded_metric_names = ranked_metric_names[:limit]
        aggregates_by_metric = {
            name: all_aggregates_by_metric[name] for name in bounded_metric_names
        }
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
            "aggregates_by_metric": aggregates_by_metric,
            "aggregate_group_count": len(all_aggregates_by_metric),
            "aggregate_groups_returned": len(aggregates_by_metric),
            "aggregate_groups_truncated": len(aggregates_by_metric) < len(all_aggregates_by_metric),
            "sample_count": len(bounded),
            "sample_truncated": matching_count > len(bounded),
        }

    def log_analysis(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Group matching log observations into bounded, useful patterns."""
        limit = arguments.get("limit", self.max_rows)
        groups: dict[str, dict[str, Any]] = {}
        matching = 0
        for item in self._iter_records(ITBenchEvidenceCategory.LOGS):
            if not _matches(
                item,
                pattern=arguments.get("pattern", arguments.get("contains")),
                service=arguments.get("service"),
                namespace=arguments.get("namespace"),
                trace_id=None,
                entity=arguments.get("entity"),
                severity=arguments.get("severity"),
            ):
                continue
            matching += 1
            record = item.get("record", {})
            body = record.get("Body", "") if isinstance(record, dict) else ""
            normalized = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<id>", str(body), flags=re.I)
            normalized = re.sub(r"\b\d{3,}\b", "<n>", normalized)
            group = groups.setdefault(
                normalized[:500],
                {
                    "pattern": normalized[:500],
                    "count": 0,
                    "first_observed": record.get("Timestamp") if isinstance(record, dict) else None,
                    "last_observed": record.get("Timestamp") if isinstance(record, dict) else None,
                    "evidence_id": item.get("evidence_id"),
                },
            )
            group["count"] += 1
            timestamp = record.get("Timestamp") if isinstance(record, dict) else None
            if isinstance(timestamp, str):
                group["first_observed"] = min(group["first_observed"] or timestamp, timestamp)
                group["last_observed"] = max(group["last_observed"] or timestamp, timestamp)
        patterns = sorted(groups.values(), key=lambda value: (-value["count"], value["pattern"]))[
            :limit
        ]
        return {
            "category": ITBenchEvidenceCategory.LOGS.value,
            "patterns": patterns,
            "records": patterns,
            "matching_count": matching,
            "returned_count": len(patterns),
            "truncated": len(groups) > len(patterns),
            "data_available": matching > 0,
        }

    def trace_analysis(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return bounded trace summaries grouped by trace identity."""
        limit = arguments.get("limit", self.max_rows)
        groups: dict[str, dict[str, Any]] = {}
        matching = 0
        for item in self._iter_records(ITBenchEvidenceCategory.TRACES):
            if not _matches(
                item,
                pattern=arguments.get("pattern", arguments.get("contains")),
                service=arguments.get("service"),
                namespace=arguments.get("namespace"),
                trace_id=arguments.get("trace_id"),
                entity=arguments.get("entity"),
                status=arguments.get("status"),
            ):
                continue
            matching += 1
            record = item.get("record", {})
            trace_id = str(record.get("TraceId", "")) if isinstance(record, dict) else ""
            if not trace_id:
                continue
            group = groups.setdefault(
                trace_id,
                {
                    "trace_id": trace_id,
                    "services": set(),
                    "error_spans": [],
                    "span_count": 0,
                    "duration": [],
                    "evidence_id": item.get("evidence_id"),
                },
            )
            group["span_count"] += 1
            if isinstance(record.get("ServiceName"), str):
                group["services"].add(record["ServiceName"])
            if str(record.get("StatusCode", "")).casefold() not in {"", "ok", "unset", "0"}:
                group["error_spans"].append(str(record.get("SpanName", ""))[:200])
            try:
                group["duration"].append(float(record.get("Duration", 0)))
            except (TypeError, ValueError):
                pass
        records = []
        for group in sorted(groups.values(), key=lambda value: value["trace_id"])[:limit]:
            duration = group.pop("duration")
            group["services"] = sorted(group["services"])
            group["error_spans"] = group["error_spans"][:8]
            group["duration_max"] = max(duration) if duration else None
            records.append(group)
        return {
            "category": ITBenchEvidenceCategory.TRACES.value,
            "records": records,
            "matching_count": matching,
            "returned_count": len(records),
            "truncated": len(groups) > len(records),
            "data_available": matching > 0,
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
        if category is ITBenchEvidenceCategory.ALERTS:
            if self._complete_alert_cache is None:
                self._complete_alert_cache = tuple(self._iter_records(category))
            else:
                self._performance["cache_hits"] += 1
            return iter(self._complete_alert_cache)
        return self._iter_records(category)

    def _iter_metric_records(self, arguments: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Read only metric files that can satisfy a typed filter.

        ITBench metric files are partitioned by service/pod identity.  Using
        that observable filename catalog avoids scanning unrelated partitions
        while preserving exact row-level filtering inside selected files.
        """
        tokens = [
            str(arguments[key]).casefold()
            for key in ("service", "metric_name")
            if isinstance(arguments.get(key), str)
        ]
        files = self.scenario.evidence_files[ITBenchEvidenceCategory.METRICS]
        selected = tuple(
            path
            for path in files
            if not tokens or any(token in path.casefold() for token in tokens)
        )
        if tokens and not selected:
            selected = files
        if selected == files:
            yield from self._iter_records(ITBenchEvidenceCategory.METRICS)
            return
        root = Path(self.scenario.snapshot_path)
        for relative_path in selected:
            for index, row in enumerate(iter_tsv(root / relative_path)):
                yield {
                    "evidence_id": str(
                        self.evidence_id(ITBenchEvidenceCategory.METRICS, relative_path, index)
                    ),
                    "category": ITBenchEvidenceCategory.METRICS.value,
                    "source_file": relative_path,
                    "row_index": index,
                    "record": row,
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

    def observable_entities(self) -> tuple[dict[str, str], ...]:
        """Return the complete deduplicated Kubernetes entity catalog."""
        if self._observable_entities_cache is not None:
            self._performance["cache_hits"] += 1
            return self._observable_entities_cache
        self._performance["cache_misses"] += 1
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
        self._observable_entities_cache = tuple(entities[key] for key in sorted(entities))
        return self._observable_entities_cache

    def candidate_entities(self, *, limit: int = 10) -> tuple[dict[str, Any], ...]:
        """Rank observable entities using generic evidence-derived signals only.

        This is a shortlist for investigation, not a diagnosis and never
        reads evaluator files.  The scoring deliberately favors direct
        abnormalities and structurally upstream/control resources over the
        already-affected workload symptom.
        """
        if self._candidate_cache is not None:
            return self._candidate_cache[:limit]
        entities = self.observable_entities()
        event_counts: dict[str, int] = {}
        for item in self._iter_records(ITBenchEvidenceCategory.K8S_EVENTS):
            entity = _record_entity(item)
            if entity is not None:
                event_counts[entity.canonical] = event_counts.get(entity.canonical, 0) + 1
        edges = self.topology(limit=None)
        degree: dict[str, int] = {}
        adjacency: dict[str, set[str]] = {}
        for edge in edges:
            for endpoint in (edge["source"], edge["target"]):
                degree[endpoint] = degree.get(endpoint, 0) + 1
            adjacency.setdefault(edge["source"], set()).add(edge["target"])
            adjacency.setdefault(edge["target"], set()).add(edge["source"])
        alert_services: set[str] = set()
        for alert in self.complete_source_records(ITBenchEvidenceCategory.ALERTS):
            record = alert.get("record", {})
            labels = record.get("labels", {}) if isinstance(record, dict) else {}
            if isinstance(labels, dict):
                for key in ("service", "service_name", "app", "workload"):
                    value = labels.get(key)
                    if isinstance(value, str):
                        alert_services.add(value.casefold())
        seeds = {
            f"{entity['namespace']}/{entity['kind']}/{entity['name']}"
            for entity in entities
            if entity["name"].casefold() in alert_services
            or entity["kind"].casefold() == "service"
            and entity["name"].casefold() in alert_services
        }
        distances: dict[str, int] = {seed: 0 for seed in seeds}
        frontier = list(seeds)
        while frontier:
            current = frontier.pop(0)
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor not in distances and distances[current] < 6:
                    distances[neighbor] = distances[current] + 1
                    frontier.append(neighbor)
        control_kinds = {
            "configmap": 4,
            "secret": 3,
            "networkpolicy": 5,
            "networkchaos": 6,
            "podchaos": 6,
            "stresschaos": 6,
            "jvmchaos": 6,
            "schedule": 5,
            "horizontalpodautoscaler": 4,
            "resourcequota": 4,
            "namespace": 3,
        }
        name_signal = (
            "chaos",
            "stress",
            "partition",
            "policy",
            "quota",
            "config",
            "hpa",
            "schedule",
        )
        ranked: list[dict[str, Any]] = []
        for item in entities:
            canonical = f"{item['namespace']}/{item['kind']}/{item['name']}"
            kind = item["kind"].casefold()
            name = item["name"].casefold()
            signals: list[str] = []
            score = control_kinds.get(kind, 0)
            if score:
                signals.append("control_or_configuration_resource")
            warning_count = event_counts.get(canonical, 0)
            if warning_count:
                score += min(warning_count, 5)
                signals.append(f"observable_event_count={warning_count}")
            if any(token in name for token in name_signal):
                score += 2
                signals.append("diagnostic_name_signal")
            if degree.get(canonical):
                score += min(degree[canonical], 4)
                signals.append(f"topology_degree={degree[canonical]}")
            if canonical in distances:
                score += max(0, 6 - distances[canonical])
                signals.append(f"alert_graph_distance={distances[canonical]}")
            ranked.append({**item, "canonical": canonical, "score": score, "signals": signals})
        ranked.sort(key=lambda item: (-int(item["score"]), item["canonical"]))
        self._candidate_cache = tuple(ranked)
        return self._candidate_cache[:limit]

    def topology(
        self,
        *,
        entity: str | None = None,
        namespace: str | None = None,
        kind: str | None = None,
        pattern: str | None = None,
        relationship: str | None = None,
        limit: int | None = 100,
    ) -> tuple[dict[str, Any], ...]:
        """Derive and filter complete observable structural edges before bounding.

        Filters operate on parsed canonical endpoints and relationship names;
        they are not no-op decoration around a global sorted prefix.
        """
        if limit is not None and (
            not isinstance(limit, int) or not 1 <= limit <= max(self.max_rows * 4, 100)
        ):
            raise ValueError("limit must be within the bounded topology limit")
        cache_key = (entity, namespace, kind, pattern, relationship, limit)
        if cache_key in self._topology_cache:
            return self._topology_cache[cache_key]
        parsed_entity = parse_canonical_entity(entity) if entity is not None else None
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
            object_kind, name = body.get("kind"), metadata.get("name")
            if not isinstance(object_kind, str) or not isinstance(name, str):
                continue
            object_namespace = (
                metadata.get("namespace")
                if isinstance(metadata.get("namespace"), str)
                else "_cluster"
            )
            source = f"{object_namespace}/{object_kind}/{name}"
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
                                f"{object_namespace}/{owner['kind']}/{owner['name']}",
                                "owner",
                            )
                        )
            if object_kind == "HorizontalPodAutoscaler":
                target = (
                    body.get("spec", {}).get("scaleTargetRef")
                    if isinstance(body.get("spec"), dict)
                    else None
                )
                if (
                    isinstance(target, dict)
                    and isinstance(target.get("kind"), str)
                    and isinstance(target.get("name"), str)
                ):
                    edges.add(
                        (source, f"{object_namespace}/{target['kind']}/{target['name']}", "scales")
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
            pod_namespace = str(pod_metadata.get("namespace", "_cluster"))
            labels = pod_metadata.get("labels", {})
            if isinstance(labels, dict):
                pods_by_namespace.setdefault(pod_namespace, []).append((pod_source, labels))
        for service_source, service_body, service_metadata in services:
            selector = service_body["spec"].get("selector", {})
            service_namespace = service_metadata.get("namespace", "_cluster")
            candidates = pods_by_namespace.get(str(service_namespace), [])
            for pod_source, labels in candidates:
                if all(labels.get(key) == value for key, value in selector.items()):
                    edges.add((service_source, pod_source, "selector"))
        for source, body, metadata in objects:
            if body.get("kind") == "NetworkPolicy":
                policy_spec = body.get("spec") if isinstance(body.get("spec"), dict) else {}
                selector = policy_spec.get("podSelector") if isinstance(policy_spec, dict) else {}
                labels = selector.get("matchLabels", {}) if isinstance(selector, dict) else {}
                if isinstance(labels, dict):
                    policy_namespace = str(metadata.get("namespace", "_cluster"))
                    for pod_source, pod_labels in pods_by_namespace.get(policy_namespace, []):
                        if all(pod_labels.get(key) == value for key, value in labels.items()):
                            edges.add((source, pod_source, "policy_selects"))
            if body.get("kind") == "Pod":
                node_name = (
                    body.get("spec", {}).get("nodeName")
                    if isinstance(body.get("spec"), dict)
                    else None
                )
                if isinstance(node_name, str):
                    object_namespace = metadata.get("namespace", "_cluster")
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
                    for ref_key, ref_kind in (("configMap", "ConfigMap"), ("secret", "Secret")):
                        config = ref.get(ref_key)
                        if isinstance(config, dict):
                            refs.append((ref_kind, config.get("name")))
                    for env_from in ref.get("envFrom", []):
                        if isinstance(env_from, dict) and isinstance(
                            env_from.get("configMapRef"), dict
                        ):
                            refs.append(("ConfigMap", env_from["configMapRef"].get("name")))
                        if isinstance(env_from, dict) and isinstance(
                            env_from.get("secretRef"), dict
                        ):
                            refs.append(("Secret", env_from["secretRef"].get("name")))
                    for ref_kind, ref_name in refs:
                        if isinstance(ref_name, str):
                            object_namespace = metadata.get("namespace", "_cluster")
                            edges.add(
                                (
                                    source,
                                    f"{object_namespace}/{ref_kind}/{ref_name}",
                                    "configuration_reference",
                                )
                            )
        filtered: list[dict[str, Any]] = []
        for source, target, edge_relationship in sorted(edges):
            if parsed_entity is not None and parsed_entity.canonical not in {source, target}:
                continue
            if relationship is not None and edge_relationship.casefold() != relationship.casefold():
                continue
            if pattern is not None:
                needle = pattern.casefold()
                if needle not in f"{source} {target} {edge_relationship}".casefold():
                    continue
            if namespace is not None or kind is not None:
                endpoints = []
                for endpoint in (source, target):
                    try:
                        endpoints.append(parse_canonical_entity(endpoint))
                    except ValueError:
                        continue
                if not any(
                    (namespace is None or endpoint.namespace == namespace)
                    and (kind is None or endpoint.kind.casefold() == kind.casefold())
                    for endpoint in endpoints
                ):
                    continue
            filtered.append({"source": source, "target": target, "relationship": edge_relationship})
        result = tuple(filtered if limit is None else filtered[:limit])
        self._topology_cache[cache_key] = result
        return result

    def _iter_records(self, category: ITBenchEvidenceCategory) -> Iterator[dict[str, Any]]:
        root = Path(self.scenario.snapshot_path)
        for relative_path in self.scenario.evidence_files[category]:
            self._performance["source_file_scans"] += 1
            path = root / relative_path
            if category is ITBenchEvidenceCategory.ALERTS:
                source: Iterator[dict[str, Any]] = iter(self._read_alerts(path))
            else:
                source = iter_tsv(path)
            for index, row in enumerate(source):
                self._performance["records_scanned"] += 1
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


def _metric_label_identity(record: dict[str, Any]) -> str:
    """Hash stable metric-label dimensions so heterogeneous series never merge."""
    tags = record.get("tags", {})
    # The source stores tags as a deterministic serialized label map. Hashing
    # that representation avoids reparsing it for every row while preserving
    # all stable label dimensions in the identity.
    dimensions: dict[str, Any] = {"tags": str(tags)}
    for key in ("metric_type", "status_code", "bucket_le"):
        if record.get(key) is not None:
            dimensions[key] = record[key]
    encoded = json.dumps(dimensions, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()[:16]


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


def _summarize_k8s_object(item: dict[str, Any]) -> dict[str, Any]:
    """Extract high-signal object state without exposing a giant raw object."""
    record = item.get("record", {})
    body = _json_object(record.get("Body")) if isinstance(record, dict) else None
    if not body:
        return {"evidence_id": item.get("evidence_id"), "state": "object body unavailable"}
    metadata = body.get("metadata", {}) if isinstance(body.get("metadata"), dict) else {}
    spec = body.get("spec", {}) if isinstance(body.get("spec"), dict) else {}
    status = body.get("status", {}) if isinstance(body.get("status"), dict) else {}
    summary: dict[str, Any] = {
        "evidence_id": item.get("evidence_id"),
        "kind": body.get("kind"),
        "namespace": metadata.get("namespace", "_cluster"),
        "name": metadata.get("name"),
        "generation": metadata.get("generation"),
        "phase": status.get("phase"),
        "conditions": status.get("conditions", [])[:6]
        if isinstance(status.get("conditions"), list)
        else [],
        "replicas": {
            key: status.get(key, spec.get(key))
            for key in ("replicas", "readyReplicas", "availableReplicas", "updatedReplicas")
            if status.get(key, spec.get(key)) is not None
        },
        "owner_references": metadata.get("ownerReferences", [])[:6]
        if isinstance(metadata.get("ownerReferences"), list)
        else [],
    }
    if isinstance(spec.get("nodeName"), str):
        summary["node_name"] = spec["nodeName"]
    if isinstance(spec.get("serviceAccountName"), str):
        summary["service_account"] = spec["serviceAccountName"]
    return summary


def _summarize_k8s_event(item: dict[str, Any]) -> dict[str, Any]:
    record = item.get("record", {})
    body = _json_object(record.get("Body")) if isinstance(record, dict) else None
    body = body or record if isinstance(record, dict) else {}
    return {
        "evidence_id": item.get("evidence_id"),
        "type": body.get("type"),
        "reason": body.get("reason"),
        "message": str(body.get("message", body.get("note", "")))[:400],
        "count": body.get("count"),
        "last_observed": body.get("lastTimestamp", body.get("eventTime")),
    }


def _matches(
    item: dict[str, Any],
    *,
    pattern: Any,
    service: Any,
    namespace: Any,
    trace_id: Any,
    entity: Any = None,
    severity: Any = None,
    status: Any = None,
    reason: Any = None,
    event_type: Any = None,
) -> bool:
    record = item.get("record")
    if not any(
        isinstance(value, str)
        for value in (
            pattern,
            service,
            namespace,
            trace_id,
            entity,
            severity,
            status,
            reason,
            event_type,
        )
    ):
        return True
    if isinstance(trace_id, str):
        if not isinstance(record, dict) or str(record.get("TraceId", "")) != trace_id:
            return False
    if isinstance(record, dict):
        body = _json_object(record.get("Body")) or {}
        semantic_record = {**body, **record} if isinstance(body, dict) else record
        if (
            isinstance(reason, str)
            and reason.casefold() != str(semantic_record.get("reason", "")).casefold()
        ):
            return False
        if (
            isinstance(event_type, str)
            and event_type.casefold() != str(semantic_record.get("type", "")).casefold()
        ):
            return False
        if isinstance(severity, str):
            actual = str(record.get("SeverityText", record.get("severity", "")))
            if actual and actual.casefold() != severity.casefold():
                return False
        if isinstance(status, str):
            actual = str(record.get("StatusCode", record.get("status", "")))
            if actual and status.casefold() not in actual.casefold():
                return False
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
        if not any(isinstance(value, str) for value in (pattern, service, namespace, entity)):
            return True
    record_text = json.dumps(record, sort_keys=True, default=str).casefold()
    if isinstance(pattern, str) and pattern.casefold() not in record_text:
        return False
    if isinstance(service, str) and service.casefold() not in record_text:
        return False
    if isinstance(namespace, str) and namespace.casefold() not in record_text:
        return False
    if isinstance(entity, str) and entity.casefold() not in record_text:
        return False
    return True


def _metric_matches(item: dict[str, Any], arguments: dict[str, Any]) -> bool:
    """Apply typed metric filters without collapsing metric families."""
    record = item.get("record", {})
    if not isinstance(record, dict):
        return False
    metric_name = arguments.get("metric_name")
    actual_metric_name = record.get("metric_name", record.get("MetricName", ""))
    if (
        isinstance(metric_name, str)
        and str(actual_metric_name).casefold() != metric_name.casefold()
    ):
        return False
    if isinstance(arguments.get("service"), str):
        service = arguments["service"].casefold()
        tags = str(record.get("tags", "")).casefold()
        pod = str(record.get("pod_name", record.get("ResourceName", ""))).casefold()
        service_field = str(record.get("service", record.get("ServiceName", ""))).casefold()
        if service not in tags and service not in pod and service != service_field:
            return False
    actual_namespace = record.get("namespace", record.get("Namespace", ""))
    if (
        isinstance(arguments.get("namespace"), str)
        and str(actual_namespace).casefold() != arguments["namespace"].casefold()
    ):
        return False
    pattern = arguments.get("pattern", arguments.get("contains"))
    return (
        not isinstance(pattern, str)
        or pattern.casefold() in json.dumps(record, sort_keys=True, default=str).casefold()
    )


def _metric_summary(values: list[float], timestamps: tuple[str, ...]) -> dict[str, Any]:
    """Summarize one metric family, never mixing names or label identities."""
    time_start: str | None
    time_end: str | None
    ordered = list(zip(timestamps, values, strict=False)) if timestamps else []
    if ordered:
        ordered.sort(key=lambda pair: pair[0])
        series = [value for _timestamp, value in ordered]
        first = series[0]
        last = series[-1]
        time_start = ordered[0][0]
        time_end = ordered[-1][0]
    else:
        first = values[0]
        last = values[-1]
        time_start = None
        time_end = None
    baseline = values[: max(1, len(values) // 3)]
    incident = values[-max(1, len(values) // 3) :]
    baseline_median = sorted(baseline)[len(baseline) // 2]
    incident_median = sorted(incident)[len(incident) // 2]
    delta = incident_median - baseline_median
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
        "first": first,
        "last": last,
        "delta": last - first if len(values) > 1 else None,
        "baseline_median": baseline_median,
        "incident_median": incident_median,
        "absolute_delta": delta,
        "relative_change": delta / abs(baseline_median) if baseline_median else None,
        "direction": "increase" if delta > 0 else "decrease" if delta < 0 else "stable",
        "time_start": time_start,
        "time_end": time_end,
    }


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
