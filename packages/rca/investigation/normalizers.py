"""Deterministic conversion from typed observations to existing RCA findings."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

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
    refs = tuple(dict.fromkeys((*finding.evidence_ids, *observation.evidence_refs)))
    return finding.model_copy(update={"details": details, "evidence_ids": refs})


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
    """Keep one effective finding per stable evidence identity, preserving provenance."""
    result: list[Finding] = []
    seen: set[str] = set()
    fallbacks: set[tuple[str, str, str]] = set()
    for finding in findings:
        ids = set(finding.evidence_ids)
        fallback = (finding.entity.canonical, finding.kind.value, finding.summary)
        if ids and ids <= seen:
            continue
        if not ids and fallback in fallbacks:
            continue
        seen.update(ids)
        fallbacks.add(fallback)
        result.append(finding)
    return tuple(result)


__all__ = ["NormalizedObservation", "deduplicate_findings", "normalize_observation"]
