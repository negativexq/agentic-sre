"""Ground-truth-blind temporal and plausibility semantics diagnostics.

This module observes the production Case and Diagnosis objects.  E0--E3 are
evaluation-only epistemic models; none of them participates in RCA.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from packages.evals.causal_semantics import finding_key
from packages.rca.engine import Case
from packages.rca.model import (
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    Resolution,
)
from packages.rca.ranking import (
    _INITIATING_KINDS,
    RankingConfig,
    _causal_time,
)
from packages.rca.resolution import _CHANGE_KINDS
from packages.rca.source import ObservationSource

MODEL_IDS = (
    "E0_CURRENT",
    "E1_HARD_CONTRADICTIONS_ONLY",
    "E2_INTERVAL_OVERLAP_SUPPORT",
    "E3_SUPPORTING_ONLY_PLAUSIBLE",
)
_CHANGE_KIND_NAMES = frozenset(item.value for item in _CHANGE_KINDS)
_INITIATING_KIND_NAMES = frozenset(item.value for item in _INITIATING_KINDS)
_OBJECT_CHANGE_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE.value,
        FindingKind.SPEC_CHANGE.value,
        FindingKind.IMAGE_CHANGE.value,
        FindingKind.SCALE_CHANGE.value,
        FindingKind.ROLLOUT_RESTART.value,
    }
)
_SUPPORTING_KIND_NAMES = frozenset(
    {
        FindingKind.CONTAINER_FAILURE.value,
        FindingKind.RESOURCE_PRESSURE.value,
        FindingKind.DEPENDENCY_ERRORS.value,
        FindingKind.FAILURE_EVENT.value,
    }
)


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class TemporalEvidenceSnapshot:
    finding_key: str
    kind: str
    entity: str
    production_at: str | None
    production_onset_delta_seconds: float | None
    production_temporal_role: str
    temporal_source_class: str
    previous_observed_at: str | None
    current_observed_at: str | None
    explicit_effective_at: str | None
    verifier_causal_at: str | None
    observation_interval_start: str | None
    observation_interval_end: str | None
    interval_relation_to_onset_grace: str
    role_verifier_time_disagreement: bool
    evidence_ids: tuple[str, ...]
    interval_reconstruction: str = "NOT_APPLICABLE"


@dataclass(frozen=True)
class HypothesisTemporalAudit:
    hypothesis_id: str
    causal_actor: str
    members: tuple[str, ...]
    causal_explanation: str
    production_plausible: bool
    production_reason_codes: tuple[str, ...]
    initiating_gap_category: str | None
    temporal_contradiction_certainties: tuple[str, ...]
    production_verification_decision: str | None
    finding_keys: tuple[str, ...]


@dataclass(frozen=True)
class CounterfactualResolution:
    model_id: str
    supported_hypothesis_ids: tuple[str, ...]
    unresolved_hypothesis_ids: tuple[str, ...]
    contradicted_hypothesis_ids: tuple[str, ...]
    terminal_state: str
    selected_hypothesis_id: str | None = None


@dataclass(frozen=True)
class TemporalPlausibilityBlindAudit:
    incident_id: str
    production_resolution: str
    onset: str | None
    verification_onset_grace_seconds: float
    temporal_evidence: tuple[TemporalEvidenceSnapshot, ...]
    hypothesis_temporal_audits: tuple[HypothesisTemporalAudit, ...]
    counterfactuals: tuple[CounterfactualResolution, ...]
    selected_hypothesis_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(asdict(self)))


@dataclass(frozen=True)
class _EvidenceInventory:
    objects: Mapping[str, Any]
    events: Mapping[str, Any]
    logs: Mapping[str, Any]
    traffic: Mapping[str, Any]


def _inventory(source: ObservationSource) -> _EvidenceInventory:
    objects = {
        version.evidence_id: version
        for versions in source.object_history().values()
        for version in versions
    }
    events = {event.evidence_id: event for event in source.events()}
    logs = {item.evidence_id: item for item in source.error_logs()}
    traffic = {item.evidence_id: item for item in source.traffic_observations()}
    return _EvidenceInventory(objects=objects, events=events, logs=logs, traffic=traffic)


def _creation_time(version: Any) -> datetime | None:
    metadata = version.body.get("metadata", {})
    if not isinstance(metadata, dict):
        return None
    return _parse_time(metadata.get("creationTimestamp"))


def _point_relation(value: datetime | None, onset: datetime | None, grace: timedelta) -> str:
    if value is None or onset is None:
        return "UNKNOWN"
    return "DEFINITELY_ONSET_CAPABLE" if value <= onset + grace else "DEFINITELY_LATE"


def _interval_relation(
    start: datetime | None,
    end: datetime | None,
    onset: datetime | None,
    grace: timedelta,
) -> str:
    if start is None or end is None or onset is None:
        return "UNKNOWN"
    boundary = onset + grace
    if end <= boundary:
        return "DEFINITELY_ONSET_CAPABLE"
    if start > boundary:
        return "DEFINITELY_LATE"
    return "STRADDLES_ONSET_GRACE"


def _object_interval(
    finding: Finding, inventory: _EvidenceInventory
) -> tuple[Any | None, Any | None]:
    versions = [
        inventory.objects[item] for item in finding.evidence_ids if item in inventory.objects
    ]
    versions = [item for item in versions if item.entity == finding.entity]
    if len(versions) < 2:
        return None, None
    ordered = sorted(versions, key=lambda item: (item.observed_at, item.evidence_id))
    return ordered[-2], ordered[-1]


def _temporal_source_class(
    finding: Finding,
    previous: Any | None,
    current: Any | None,
    inventory: _EvidenceInventory,
) -> str:
    if finding.kind.value in _OBJECT_CHANGE_KINDS and previous is not None and current is not None:
        return "OBJECT_CHANGE_OBSERVATION_INTERVAL"
    if "initiating_at" in finding.details:
        return "EXPLICIT_INITIATING_TIME"
    if "schedule_active_from" in finding.details:
        return "SCHEDULE_ACTIVE_FROM"
    if finding.kind is FindingKind.OBJECT_CREATED:
        return (
            "OBJECT_CREATION_EXPLICIT_TIME"
            if current and _creation_time(current)
            else "OBJECT_FIRST_OBSERVED_TIME"
        )
    if finding.kind is FindingKind.OBJECT_DELETED:
        return "OBJECT_DELETION_FIRST_NOTICED_TIME"
    if any(item in inventory.events for item in finding.evidence_ids):
        return "KUBERNETES_EVENT_TIME"
    if any(item in inventory.logs for item in finding.evidence_ids):
        return "LOG_RECORD_TIME"
    if any(item in inventory.traffic for item in finding.evidence_ids):
        return "METRIC_OBSERVATION_TIME"
    if finding.at is not None:
        return "UNKNOWN_TIME_SOURCE"
    return "UNKNOWN_TIME_SOURCE"


def _explicit_time(finding: Finding) -> datetime | None:
    return _parse_time(
        finding.details.get("initiating_at") or finding.details.get("schedule_active_from")
    )


def temporal_evidence_snapshots(
    findings: Sequence[Finding],
    source: ObservationSource,
    onset: datetime | None,
    grace: timedelta,
) -> tuple[TemporalEvidenceSnapshot, ...]:
    """Capture production time plus source intervals without changing Findings."""
    inventory = _inventory(source)
    result: list[TemporalEvidenceSnapshot] = []
    for finding in sorted(findings, key=lambda item: finding_key(item)):
        previous, current = (None, None)
        if finding.kind.value in _OBJECT_CHANGE_KINDS:
            previous, current = _object_interval(finding, inventory)
        explicit = _explicit_time(finding)
        verifier = _causal_time(finding)
        interval_relation = (
            _interval_relation(
                previous.observed_at if previous is not None else None,
                current.observed_at if current is not None else None,
                onset,
                grace,
            )
            if finding.kind.value in _OBJECT_CHANGE_KINDS
            else "NO_INTERVAL"
        )
        interval_reconstruction = (
            "OK"
            if previous is not None and current is not None
            else "INTERVAL_RECONSTRUCTION_FAILED"
            if finding.kind.value in _OBJECT_CHANGE_KINDS
            else "NOT_APPLICABLE"
        )
        production_relation = _point_relation(finding.at, onset, grace)
        verifier_relation = _point_relation(verifier, onset, grace)
        disagreement = (
            verifier is not None
            and finding.at is not None
            and verifier != finding.at
            and production_relation != verifier_relation
        )
        result.append(
            TemporalEvidenceSnapshot(
                finding_key=finding_key(finding),
                kind=finding.kind.value,
                entity=finding.entity.canonical,
                production_at=_iso(finding.at),
                production_onset_delta_seconds=finding.onset_delta_seconds,
                production_temporal_role=finding.temporal_role.value,
                temporal_source_class=_temporal_source_class(finding, previous, current, inventory),
                previous_observed_at=_iso(previous.observed_at if previous else None),
                current_observed_at=_iso(current.observed_at if current else None),
                explicit_effective_at=_iso(explicit),
                verifier_causal_at=_iso(verifier),
                observation_interval_start=_iso(previous.observed_at if previous else None),
                observation_interval_end=_iso(current.observed_at if current else None),
                interval_relation_to_onset_grace=(
                    interval_relation
                    if finding.kind.value in _OBJECT_CHANGE_KINDS
                    else "NO_INTERVAL"
                ),
                role_verifier_time_disagreement=disagreement,
                evidence_ids=tuple(finding.evidence_ids),
                interval_reconstruction=interval_reconstruction,
            )
        )
    return tuple(result)


def _verification_decision(audit: Any) -> str | None:
    verification = audit.verification if audit is not None else None
    decision = verification.decision if verification is not None else None
    return decision.value if decision is not None else None


def _initiating_gap_category(
    hypothesis: Hypothesis,
    temporal: Mapping[str, TemporalEvidenceSnapshot],
) -> str:
    if hypothesis.causal_explanation not in {"PATH", "DIRECT"}:
        return "NO_CAUSALLY_LINKED_EVIDENCE"
    if not hypothesis.findings:
        return "NO_CAUSALLY_LINKED_EVIDENCE"
    initiating_kinds = [
        item
        for item in hypothesis.findings
        if item.kind.value in _INITIATING_KIND_NAMES or item.kind.value in _CHANGE_KIND_NAMES
    ]
    if initiating_kinds:
        relations = [
            temporal[finding_key(item)].interval_relation_to_onset_grace
            for item in initiating_kinds
        ]
        if "STRADDLES_ONSET_GRACE" in relations:
            return "INITIATING_KIND_BUT_INTERVAL_UNCERTAIN"
        if relations and all(item == "DEFINITELY_LATE" for item in relations):
            return "INITIATING_KIND_DEFINITELY_LATE"
        if any(item == "UNKNOWN" for item in relations):
            return "UNKNOWN_INITIATING_GAP"
        if all(
            item.production_temporal_role == EvidenceTemporalRole.CONSEQUENCE.value
            for item in (temporal[finding_key(finding)] for finding in initiating_kinds)
        ):
            return "ONLY_CONSEQUENCE_EVIDENCE"
    kinds = {item.kind.value for item in hypothesis.findings}
    roles = {item.temporal_role.value for item in hypothesis.findings}
    if kinds and kinds <= _SUPPORTING_KIND_NAMES:
        return "ONLY_SUPPORTING_EVIDENCE"
    if roles and roles <= {EvidenceTemporalRole.CONSEQUENCE.value}:
        return "ONLY_CONSEQUENCE_EVIDENCE"
    if any(item in _SUPPORTING_KIND_NAMES for item in kinds):
        return "MIXED_NON_INITIATING_EVIDENCE"
    return "UNKNOWN_INITIATING_GAP"


def _contradiction_certainties(
    hypothesis: Hypothesis,
    temporal: Mapping[str, TemporalEvidenceSnapshot],
    onset: datetime | None,
    grace: timedelta,
) -> tuple[str, ...]:
    values: list[str] = []
    for finding in hypothesis.contradictory_findings:
        snapshot = temporal[finding_key(finding)]
        if snapshot.interval_relation_to_onset_grace == "DEFINITELY_LATE":
            values.append("DEFINITE_TEMPORAL_CONTRADICTION")
        elif snapshot.interval_relation_to_onset_grace == "STRADDLES_ONSET_GRACE":
            values.append("OBSERVATION_INTERVAL_UNCERTAIN")
        elif snapshot.temporal_source_class != "UNKNOWN_TIME_SOURCE":
            when = _parse_time(snapshot.verifier_causal_at)
            if _point_relation(when, onset, grace) == "DEFINITELY_LATE":
                values.append("POINT_TIME_TEMPORAL_CONTRADICTION")
            else:
                values.append("UNKNOWN_TEMPORAL_BASIS")
        else:
            values.append("UNKNOWN_TEMPORAL_BASIS")
    return tuple(sorted(set(values)))


def _hypothesis_status(
    model_id: str,
    hypothesis: HypothesisTemporalAudit,
) -> str:
    reasons = set(hypothesis.production_reason_codes)
    definite = set(hypothesis.temporal_contradiction_certainties).intersection(
        {"DEFINITE_TEMPORAL_CONTRADICTION", "POINT_TIME_TEMPORAL_CONTRADICTION"}
    )
    if model_id == "E0_CURRENT":
        return "SUPPORTED" if hypothesis.production_plausible else "CONTRADICTED"
    if "NO_CAUSAL_SYMPTOM_LINK" in reasons or definite:
        return "CONTRADICTED"
    if (
        model_id == "E2_INTERVAL_OVERLAP_SUPPORT"
        and hypothesis.initiating_gap_category == "INITIATING_KIND_BUT_INTERVAL_UNCERTAIN"
    ):
        return "SUPPORTED"
    if (
        model_id == "E3_SUPPORTING_ONLY_PLAUSIBLE"
        and hypothesis.initiating_gap_category == "ONLY_SUPPORTING_EVIDENCE"
    ):
        return "SUPPORTED"
    if hypothesis.production_plausible:
        return "SUPPORTED"
    return "UNRESOLVED"


def _terminal(
    supported: tuple[str, ...], unresolved: tuple[str, ...], contradicted: tuple[str, ...]
) -> tuple[str, str | None]:
    if len(supported) == 1 and not unresolved:
        return "RESOLVED", supported[0]
    if supported or unresolved:
        return "AMBIGUOUS" if supported else "INSUFFICIENT_EVIDENCE", None
    return "INSUFFICIENT_EVIDENCE", None


def counterfactual_resolution(
    model_id: str, hypotheses: Sequence[HypothesisTemporalAudit]
) -> CounterfactualResolution:
    if model_id not in MODEL_IDS:
        raise ValueError(f"unsupported temporal model: {model_id}")
    statuses = {item.hypothesis_id: _hypothesis_status(model_id, item) for item in hypotheses}
    supported = tuple(sorted(key for key, value in statuses.items() if value == "SUPPORTED"))
    unresolved = tuple(sorted(key for key, value in statuses.items() if value == "UNRESOLVED"))
    contradicted = tuple(sorted(key for key, value in statuses.items() if value == "CONTRADICTED"))
    terminal, selected = _terminal(supported, unresolved, contradicted)
    return CounterfactualResolution(
        model_id=model_id,
        supported_hypothesis_ids=supported,
        unresolved_hypothesis_ids=unresolved,
        contradicted_hypothesis_ids=contradicted,
        terminal_state=terminal,
        selected_hypothesis_id=selected,
    )


def build_temporal_audit(
    case: Case,
    diagnosis: Any,
    *,
    ranking: RankingConfig | None = None,
) -> TemporalPlausibilityBlindAudit:
    """Capture production time semantics and frozen counterfactual outcomes."""
    ranking = ranking or RankingConfig()
    trace = diagnosis.resolution_trace
    audit_by_id = {item.hypothesis_id: item for item in (trace.hypothesis_audits if trace else ())}
    evidence = temporal_evidence_snapshots(
        tuple(case.findings),
        case.source,
        case.symptoms.onset,
        ranking.verification_onset_grace,
    )
    temporal_by_key = {item.finding_key: item for item in evidence}
    hypotheses: list[HypothesisTemporalAudit] = []
    for hypothesis in sorted(case.hypotheses, key=lambda item: item.hypothesis_id):
        resolution_audit = audit_by_id.get(hypothesis.hypothesis_id)
        reasons = (
            tuple(sorted(reason.value for reason in resolution_audit.plausibility_reasons))
            if resolution_audit is not None
            else ()
        )
        hypotheses.append(
            HypothesisTemporalAudit(
                hypothesis_id=hypothesis.hypothesis_id,
                causal_actor=hypothesis.causal_actor.canonical,
                members=tuple(sorted(item.canonical for item in hypothesis.members)),
                causal_explanation=hypothesis.causal_explanation,
                production_plausible=bool(resolution_audit and resolution_audit.plausible),
                production_reason_codes=reasons,
                initiating_gap_category=(
                    _initiating_gap_category(hypothesis, temporal_by_key)
                    if "NO_ONSET_CAPABLE_INITIATING_EVIDENCE" in reasons
                    else None
                ),
                temporal_contradiction_certainties=_contradiction_certainties(
                    hypothesis,
                    temporal_by_key,
                    case.symptoms.onset,
                    ranking.verification_onset_grace,
                ),
                production_verification_decision=_verification_decision(resolution_audit),
                finding_keys=tuple(sorted(finding_key(item) for item in hypothesis.findings)),
            )
        )
    hypothesis_audits = tuple(hypotheses)
    selected_id = diagnosis.hypothesis.hypothesis_id if diagnosis.hypothesis else None
    e0 = counterfactual_resolution("E0_CURRENT", hypothesis_audits)
    e0 = e0.__class__(
        **{
            **asdict(e0),
            "terminal_state": diagnosis.resolution.value,
            "selected_hypothesis_id": selected_id
            if diagnosis.resolution is Resolution.RESOLVED
            else None,
        }
    )
    counterfactuals = (e0,) + tuple(
        counterfactual_resolution(model_id, hypothesis_audits) for model_id in MODEL_IDS[1:]
    )
    return TemporalPlausibilityBlindAudit(
        incident_id=case.incident_id,
        production_resolution=diagnosis.resolution.value,
        onset=_iso(case.symptoms.onset),
        verification_onset_grace_seconds=ranking.verification_onset_grace.total_seconds(),
        temporal_evidence=evidence,
        hypothesis_temporal_audits=hypothesis_audits,
        counterfactuals=counterfactuals,
        selected_hypothesis_id=selected_id,
    )


def blind_audit_from_dict(value: Mapping[str, Any]) -> TemporalPlausibilityBlindAudit:
    return TemporalPlausibilityBlindAudit(
        incident_id=str(value["incident_id"]),
        production_resolution=str(value["production_resolution"]),
        onset=value.get("onset"),
        verification_onset_grace_seconds=float(value["verification_onset_grace_seconds"]),
        temporal_evidence=tuple(
            TemporalEvidenceSnapshot(
                **{
                    **item,
                    "evidence_ids": tuple(item["evidence_ids"]),
                }
            )
            for item in value["temporal_evidence"]
        ),
        hypothesis_temporal_audits=tuple(
            HypothesisTemporalAudit(
                **{
                    **item,
                    "members": tuple(item.get("members", ())),
                    "production_reason_codes": tuple(item["production_reason_codes"]),
                    "temporal_contradiction_certainties": tuple(
                        item["temporal_contradiction_certainties"]
                    ),
                    "finding_keys": tuple(item["finding_keys"]),
                }
            )
            for item in value["hypothesis_temporal_audits"]
        ),
        counterfactuals=tuple(
            CounterfactualResolution(
                **{
                    **item,
                    "supported_hypothesis_ids": tuple(item["supported_hypothesis_ids"]),
                    "unresolved_hypothesis_ids": tuple(item["unresolved_hypothesis_ids"]),
                    "contradicted_hypothesis_ids": tuple(item["contradicted_hypothesis_ids"]),
                }
            )
            for item in value["counterfactuals"]
        ),
        selected_hypothesis_id=value.get("selected_hypothesis_id"),
    )


__all__ = [
    "MODEL_IDS",
    "CounterfactualResolution",
    "HypothesisTemporalAudit",
    "TemporalEvidenceSnapshot",
    "TemporalPlausibilityBlindAudit",
    "blind_audit_from_dict",
    "build_temporal_audit",
    "counterfactual_resolution",
    "temporal_evidence_snapshots",
]
