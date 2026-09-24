"""Deterministic conversion from typed observations to existing RCA findings."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from packages.rca.engine import Case
from packages.rca.model import (
    ClusterEvent,
    Finding,
    GapOutcomeKind,
    InformationGap,
    InvestigationObservation,
    LogRecord,
    ObjectVersion,
    ResourcePressure,
    TrafficObservation,
)
from packages.rca.signals import (
    autoscaling_findings,
    change_findings,
    dependency_findings,
    failure_findings,
    resource_findings,
    traffic_findings,
)


@dataclass(frozen=True)
class NormalizedObservation:
    observation: InvestigationObservation
    findings: tuple[Finding, ...]


def _with_provenance(
    finding: Finding, observation: InvestigationObservation, gap: InformationGap
) -> Finding:
    details = dict(finding.details)
    details.update(
        {
            "observation_id": observation.observation_id,
            "investigation_gap_id": gap.gap_id,
            "investigation_capability": observation.capability,
        }
    )
    runtime = observation.runtime
    if runtime is not None:
        rule_by_capability = {
            "resource_pressure": "prometheus.resource_pressure_threshold.v1",
            "traffic": "prometheus.traffic_increase_baseline.v1",
            "logs": "loki.dependency_error_pattern.v1",
            "runtime_traces": "tempo.trace_observation.v1",
        }
        details.update(
            {
                "runtime_pillar": runtime.pillar.value,
                "runtime_capability": runtime.capability,
                "runtime_target": runtime.query.target.canonical,
                "runtime_requested_start": runtime.query.requested_start.isoformat(),
                "runtime_requested_end": runtime.query.requested_end.isoformat(),
                "runtime_effective_start": runtime.query.effective_start.isoformat(),
                "runtime_effective_end": runtime.query.effective_end.isoformat(),
                "runtime_query_descriptor_id": runtime.query.descriptor_id,
                "runtime_query_template_id": runtime.query.template_id,
                "runtime_source_observation_ids": runtime.source_observation_ids,
                "runtime_observation_state": runtime.state.value,
                "runtime_normalization_rule_id": rule_by_capability[runtime.capability],
            }
        )
    return finding.model_copy(update={"details": details})


_ACQUISITION_DETAIL_KEYS = frozenset(
    {"observation_id", "investigation_gap_id", "investigation_capability"}
)


def _semantic_value(value: Any) -> Any:
    """Convert model/json-friendly details into a deterministic JSON value."""
    if isinstance(value, BaseModel):
        return _semantic_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _semantic_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (set, frozenset)):
        return sorted((_semantic_value(item) for item in value), key=lambda item: str(item))
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item) for item in value]
    return value


def finding_identity(finding: Finding) -> tuple[Any, ...]:
    """Return the semantic identity used for investigation Finding deduplication."""
    semantic_details = {
        key: value for key, value in finding.details.items() if key not in _ACQUISITION_DETAIL_KEYS
    }
    return (
        finding.kind.value,
        finding.entity.canonical,
        finding.at.isoformat() if finding.at is not None else None,
        finding.summary,
        tuple(sorted(item.canonical for item in finding.related)),
        json.dumps(
            _semantic_value(semantic_details),
            sort_keys=True,
            separators=(",", ":"),
        ),
        tuple(sorted(set(finding.evidence_ids))),
    )


def _payload_findings(payload: dict[str, Any]) -> tuple[Finding, ...]:
    raw = payload.get("findings")
    if not isinstance(raw, list):
        return ()
    findings: list[Finding] = []
    for item in raw[:32]:
        if not isinstance(item, dict):
            continue
        try:
            findings.append(Finding.model_validate(item))
        except ValueError:
            continue
    return tuple(findings)


def _event_findings(case: Case, payload: dict[str, Any]) -> tuple[Finding, ...]:
    raw = payload.get("events")
    if not isinstance(raw, list):
        return ()
    events: list[ClusterEvent] = []
    for item in raw[:32]:
        if isinstance(item, dict):
            try:
                events.append(ClusterEvent.model_validate(item))
            except ValueError:
                continue
    if not events:
        return ()
    history = case.source.object_history()
    return tuple([*failure_findings(events), *autoscaling_findings(history, events, case.topology)])


def _history_findings(payload: dict[str, Any]) -> tuple[Finding, ...]:
    raw = payload.get("versions")
    if not isinstance(raw, list):
        return ()
    history: dict[Any, list[ObjectVersion]] = {}
    for item in raw[:64]:
        if not isinstance(item, dict):
            continue
        try:
            version = ObjectVersion.model_validate(item)
        except ValueError:
            continue
        history.setdefault(version.entity, []).append(version)
    return tuple(change_findings(history))


def _metric_findings(case: Case, payload: dict[str, Any]) -> tuple[Finding, ...]:
    raw_pressure = payload.get("resource_pressure")
    if isinstance(raw_pressure, list):
        pressure: list[ResourcePressure] = []
        for item in raw_pressure[:32]:
            if isinstance(item, dict):
                try:
                    pressure.append(ResourcePressure.model_validate(item))
                except ValueError:
                    continue
        return tuple(resource_findings(pressure))
    raw_traffic = payload.get("traffic")
    if isinstance(raw_traffic, list):
        traffic: list[TrafficObservation] = []
        for item in raw_traffic[:64]:
            if isinstance(item, dict):
                try:
                    traffic.append(TrafficObservation.model_validate(item))
                except ValueError:
                    continue
        return tuple(traffic_findings(traffic, case.symptoms.onset, case.context.window_end))
    return ()


def _log_findings(case: Case, payload: dict[str, Any]) -> tuple[Finding, ...]:
    raw = payload.get("logs")
    if not isinstance(raw, list):
        return ()
    records: list[LogRecord] = []
    for item in raw[:32]:
        if not isinstance(item, dict):
            continue
        try:
            records.append(LogRecord.model_validate(item))
        except ValueError:
            continue
    return tuple(dependency_findings(records, case.topology, case.context.symptom_entities))


def normalize_observation(
    observation: InvestigationObservation,
    *,
    case: Case,
    gap: InformationGap,
) -> NormalizedObservation:
    """Interpret tool data with existing signal functions, never model claims."""
    if observation.outcome is GapOutcomeKind.NO_DATA or observation.error:
        return NormalizedObservation(observation=observation, findings=())
    payload = observation.payload
    findings = list(_payload_findings(payload))
    if observation.capability == "history":
        findings.extend(_history_findings(payload))
    elif observation.capability == "events":
        findings.extend(_event_findings(case, payload))
    elif observation.capability in {"resource_pressure", "traffic"}:
        findings.extend(_metric_findings(case, payload))
    elif observation.capability == "logs":
        findings.extend(_log_findings(case, payload))
    normalized = tuple(_with_provenance(item, observation, gap) for item in findings)
    hypothesis_ids = tuple(
        sorted(
            hypothesis.hypothesis_id
            for hypothesis in case.hypotheses
            if observation.target == hypothesis.causal_actor
            or observation.target in hypothesis.members
        )
    )
    interpreted = observation.model_copy(
        update={
            "hypothesis_ids": hypothesis_ids,
            "outcome": (
                GapOutcomeKind.SUPPORTS if normalized and hypothesis_ids else GapOutcomeKind.UNKNOWN
            ),
        }
    )
    return NormalizedObservation(observation=interpreted, findings=normalized)


def deduplicate_findings(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Keep one effective Finding per semantic identity, preserving first occurrence."""
    result: list[Finding] = []
    seen: set[tuple[Any, ...]] = set()
    for finding in findings:
        identity = finding_identity(finding)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(finding)
    return tuple(result)


def new_investigation_findings(
    existing: Iterable[Finding], incoming: Iterable[Finding]
) -> tuple[Finding, ...]:
    """Return incoming Findings with semantic identities absent from existing Findings."""
    known = {finding_identity(finding) for finding in existing}
    fresh: list[Finding] = []
    for finding in deduplicate_findings(incoming):
        identity = finding_identity(finding)
        if identity in known:
            continue
        fresh.append(finding)
        known.add(identity)
    return tuple(fresh)


__all__ = [
    "NormalizedObservation",
    "deduplicate_findings",
    "finding_identity",
    "new_investigation_findings",
    "normalize_observation",
]
