"""Bounded semantic read-only tools backed by the existing ObservationSource."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

from packages.rca.engine import Case
from packages.rca.information_gap import CAPABILITIES
from packages.rca.investigation.environment import (
    InvestigationBackend,
    _default_query,
    _query_trace_observations,
)
from packages.rca.investigation.state import InvestigationTool
from packages.rca.model import (
    EntityRef,
    GapOutcomeKind,
    InformationGap,
    InvestigationObservation,
    InvestigationQuery,
)


def _safe_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _safe_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_payload(item) for item in value[:32]]
    if isinstance(value, str):
        return value[:4000]
    return value


def make_observation(
    *,
    gap: InformationGap,
    capability: str,
    target: EntityRef,
    payload: Mapping[str, Any],
    evidence_refs: tuple[str, ...] = (),
    observed_at: datetime | None = None,
    source_class: str = "observation_source",
    error: str | None = None,
) -> InvestigationObservation:
    bounded = _safe_payload(dict(payload))
    material = json.dumps(
        {
            "gap": gap.gap_id,
            "capability": capability,
            "target": target.canonical,
            "payload": bounded,
            "evidence": sorted(evidence_refs),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    observation_id = f"investigation:{capability}:{sha256(material.encode()).hexdigest()[:20]}"
    has_data = bool(payload) and any(bool(value) for value in payload.values())
    outcome = GapOutcomeKind.UNKNOWN if has_data and error is None else GapOutcomeKind.NO_DATA
    if error is not None:
        outcome = GapOutcomeKind.UNKNOWN
    return InvestigationObservation(
        observation_id=observation_id,
        gap_id=gap.gap_id,
        capability=capability,
        target=target,
        observed_at=observed_at,
        outcome=outcome,
        payload=bounded,
        evidence_refs=tuple(dict.fromkeys(evidence_refs))[:32],
        source_class=source_class,
        error=error,
    )


class _BaseTool:
    name: str

    def __init__(self, backend: InvestigationBackend | None = None) -> None:
        self.backend = backend

    def execute(
        self, case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        raise NotImplementedError

    def execute_query(
        self,
        case: Case,
        gap: InformationGap,
        target: EntityRef,
        query: InvestigationQuery | None,
    ) -> InvestigationObservation:
        """Execute a semantic query; legacy direct calls remain compatible."""
        del query
        return self.execute(case, gap, target)

    def _observation(
        self,
        gap: InformationGap,
        target: EntityRef,
        payload: Mapping[str, Any],
        *,
        refs: tuple[str, ...] = (),
        observed_at: datetime | None = None,
    ) -> InvestigationObservation:
        return make_observation(
            gap=gap,
            capability=self.name,
            target=target,
            payload=payload,
            evidence_refs=refs,
            observed_at=observed_at,
        )


class DescribeTool(_BaseTool):
    name = "describe"

    def execute(
        self, case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        version = case.topology.latest.get(target)
        if version is None:
            return self._observation(gap, target, {})
        body = version.body
        metadata_raw = body.get("metadata")
        metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}
        payload = {
            "kind": body.get("kind", target.kind),
            "spec": body.get("spec", {}),
            "status": body.get("status", {}),
            "data": body.get("data", {}),
            "labels": metadata.get("labels", {}),
        }
        return self._observation(gap, target, payload, refs=(version.evidence_id,))

    def execute_query(
        self, case: Case, gap: InformationGap, target: EntityRef, query: InvestigationQuery | None
    ) -> InvestigationObservation:
        if self.backend is None:
            return self.execute(case, gap, target)
        requested = _default_query(query, onset=case.symptoms.onset)
        versions = self.backend.query_history(target, requested)
        version = versions[-1] if versions else None
        if version is None:
            return self._observation(gap, target, {})
        body = version.body
        metadata_raw = body.get("metadata")
        metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}
        return self._observation(
            gap,
            target,
            {
                "kind": body.get("kind", target.kind),
                "spec": body.get("spec", {}),
                "status": body.get("status", {}),
                "data": body.get("data", {}),
                "labels": metadata.get("labels", {}),
            },
            refs=(version.evidence_id,),
            observed_at=version.observed_at,
        )


class HistoryTool(_BaseTool):
    name = "history"

    def execute(
        self, case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        findings = [finding for finding in case.findings if finding.entity == target]
        payload = {"findings": [finding.model_dump(mode="json") for finding in findings[:16]]}
        refs = tuple(ref for finding in findings for ref in finding.evidence_ids)[:32]
        observed = max((finding.at for finding in findings if finding.at), default=None)
        return self._observation(gap, target, payload, refs=refs, observed_at=observed)

    def execute_query(
        self, case: Case, gap: InformationGap, target: EntityRef, query: InvestigationQuery | None
    ) -> InvestigationObservation:
        if self.backend is None:
            return self.execute(case, gap, target)
        requested = _default_query(query, onset=case.symptoms.onset)
        versions = self.backend.query_history(target, requested)
        return self._observation(
            gap,
            target,
            {"versions": [version.model_dump(mode="json") for version in versions]},
            refs=tuple(version.evidence_id for version in versions),
            observed_at=max((version.observed_at for version in versions), default=None),
        )


class EventsTool(_BaseTool):
    name = "events"

    def execute(
        self, case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        cutoff = case.context.window_end
        events = [
            event
            for event in case.source.events()
            if event.entity == target
            and (cutoff is None or (event.last_at or event.first_at or cutoff) <= cutoff)
        ][-32:]
        refs = tuple(event.evidence_id for event in events)
        observed = max(
            (time for event in events for time in (event.last_at, event.first_at) if time),
            default=None,
        )
        return self._observation(
            gap,
            target,
            {"events": [event.model_dump(mode="json") for event in events]},
            refs=refs,
            observed_at=observed,
        )

    def execute_query(
        self, case: Case, gap: InformationGap, target: EntityRef, query: InvestigationQuery | None
    ) -> InvestigationObservation:
        if self.backend is None:
            return self.execute(case, gap, target)
        requested = _default_query(query, onset=case.symptoms.onset)
        events = self.backend.query_events(target, requested)
        return self._observation(
            gap,
            target,
            {"events": [event.model_dump(mode="json") for event in events]},
            refs=tuple(event.evidence_id for event in events),
            observed_at=max(
                (time for event in events for time in (event.last_at, event.first_at) if time),
                default=None,
            ),
        )


class NeighborsTool(_BaseTool):
    name = "neighbors"

    def execute(
        self, case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        neighbors = case.topology.neighbors(target)[:32]
        return self._observation(
            gap,
            target,
            {
                "neighbors": [
                    {"entity": entity.canonical, "relation": relation}
                    for entity, relation in neighbors
                ]
            },
            refs=(),
        )


class LogsTool(_BaseTool):
    name = "logs"

    def execute(
        self, case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        services = sorted(case.topology.service_names(target))
        records: list[dict[str, Any]] = []
        for service in services[:4]:
            records.extend(case.source.logs(service, limit=12))
        records = records[:32]
        refs = tuple(
            record.get("evidence_id", "") for record in records if record.get("evidence_id")
        )
        return self._observation(gap, target, {"logs": records}, refs=refs)

    def execute_query(
        self, case: Case, gap: InformationGap, target: EntityRef, query: InvestigationQuery | None
    ) -> InvestigationObservation:
        if self.backend is None:
            return self.execute(case, gap, target)
        requested = _default_query(query, onset=case.symptoms.onset)
        records = self.backend.query_logs(target, requested)
        return self._observation(
            gap,
            target,
            {"logs": [record.model_dump(mode="json") for record in records]},
            refs=tuple(record.evidence_id for record in records),
            observed_at=max((record.at for record in records if record.at), default=None),
        )


class ResourcePressureTool(_BaseTool):
    name = "resource_pressure"

    def execute(
        self, case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        onset = case.symptoms.onset or case.context.window_end
        since = onset - timedelta(hours=2) if onset else datetime.min.astimezone()
        records = list(case.source.resource_pressure((target,), since))[:32]
        return self._observation(
            gap,
            target,
            {"resource_pressure": [item.model_dump(mode="json") for item in records]},
            refs=tuple(item.evidence_id for item in records),
            observed_at=max((item.at for item in records if item.at), default=None),
        )

    def execute_query(
        self, case: Case, gap: InformationGap, target: EntityRef, query: InvestigationQuery | None
    ) -> InvestigationObservation:
        if self.backend is None:
            return self.execute(case, gap, target)
        requested = _default_query(query, onset=case.symptoms.onset)
        records = self.backend.query_resource_pressure(target, requested)
        return self._observation(
            gap,
            target,
            {"resource_pressure": [item.model_dump(mode="json") for item in records]},
            refs=tuple(item.evidence_id for item in records),
            observed_at=max((item.at for item in records if item.at), default=None),
        )


class TrafficTool(_BaseTool):
    name = "traffic"

    def execute(
        self, case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        records = [item for item in case.source.traffic_observations() if item.entity == target][
            :64
        ]
        return self._observation(
            gap,
            target,
            {"traffic": [item.model_dump(mode="json") for item in records]},
            refs=tuple(item.evidence_id for item in records),
            observed_at=max((item.at for item in records), default=None),
        )

    def execute_query(
        self, case: Case, gap: InformationGap, target: EntityRef, query: InvestigationQuery | None
    ) -> InvestigationObservation:
        if self.backend is None:
            return self.execute(case, gap, target)
        requested = _default_query(query, onset=case.symptoms.onset)
        records = self.backend.query_traffic(target, requested)
        return self._observation(
            gap,
            target,
            {"traffic": [item.model_dump(mode="json") for item in records]},
            refs=tuple(item.evidence_id for item in records),
            observed_at=max((item.at for item in records), default=None),
        )


class RuntimeTracesTool(_BaseTool):
    name = "runtime_traces"

    def execute(
        self, case: Case, gap: InformationGap, target: EntityRef
    ) -> InvestigationObservation:
        return self.execute_query(case, gap, target, None)

    def execute_query(
        self, case: Case, gap: InformationGap, target: EntityRef, query: InvestigationQuery | None
    ) -> InvestigationObservation:
        requested = _default_query(query, onset=case.symptoms.onset)
        if self.backend is None:
            spans = _query_trace_observations(
                case.source.trace_observations(),
                target,
                requested,
                case.source.observation_cutoff(),
            )
        else:
            spans = self.backend.query_traces(target, requested)
        return self._observation(
            gap,
            target,
            {"traces": [span.model_dump(mode="json") for span in spans]},
            refs=tuple(span.evidence_id for span in spans),
            observed_at=max((span.start_at for span in spans), default=None),
        )


def default_tools(backend: InvestigationBackend | None = None) -> dict[str, InvestigationTool]:
    """Build a fresh registry; tools contain no mutable cross-run state."""
    tools: tuple[InvestigationTool, ...] = (
        DescribeTool(backend),
        HistoryTool(backend),
        EventsTool(backend),
        NeighborsTool(backend),
        LogsTool(backend),
        ResourcePressureTool(backend),
        TrafficTool(backend),
        RuntimeTracesTool(backend),
    )
    return {capability.name: tool for capability, tool in zip(CAPABILITIES, tools, strict=True)}


__all__ = [
    "InvestigationTool",
    "RuntimeTracesTool",
    "default_tools",
    "make_observation",
]
