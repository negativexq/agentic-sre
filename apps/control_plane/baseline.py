"""Incident-free clean-baseline probe (M19-6.8).

Before a product scenario stages anything, the harness asks whether the
already-persisted evidence would by itself yield a root candidate. The probe
diagnoses the window ``[collector_started_at, baseline_reference_at]`` of
persisted journal, Event and lifecycle evidence, using ``baseline_reference_at``
only as the ephemeral onset of the engine's existing temporal semantics.

It writes nothing: no incident, alert, diagnosis revision, requirement,
timeline event, investigation run, provider read, snapshot cycle or manifest.
It reads no provider at all, so resource/A2 state is NOT ASSESSED: absent
provider evidence is never normality, contradiction or elimination.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.live import LiveSource
from packages.rca.manifest import event_evidence_id
from packages.rca.model import EliminationConsequence, EvidenceTemporalRole, Finding, Hypothesis
from packages.storage.manifest import ManifestRequest, load_members, select_members

# Selecting members is keyed by incident only for alerts and logs; the probe
# has no incident, so this id matches neither.
NO_INCIDENT = UUID(int=0)


@dataclass(frozen=True)
class BaselineEvaluation:
    """An ephemeral evaluation; never stored and never a diagnosis revision."""

    reference_at: datetime
    window_start: datetime
    findings: tuple[Finding, ...]
    hypotheses: tuple[Hypothesis, ...]
    eliminations: tuple[Any, ...]
    initiating_finding_ids: tuple[str, ...]
    root_eligible_manifestation_only_hypothesis_ids: tuple[str, ...]

    @property
    def initiating_finding_count(self) -> int:
        return len(self.initiating_finding_ids)

    @property
    def root_eligible_manifestation_only_count(self) -> int:
        return len(self.root_eligible_manifestation_only_hypothesis_ids)

    def document(self) -> dict[str, Any]:
        return {
            "reference_at": self.reference_at.isoformat(),
            "window_start": self.window_start.isoformat(),
            "findings": [item.model_dump(mode="json") for item in self.findings],
            "hypotheses": [item.model_dump(mode="json") for item in self.hypotheses],
            "eliminations": [item.model_dump(mode="json") for item in self.eliminations],
            "initiating_finding_count": self.initiating_finding_count,
            "initiating_finding_ids": list(self.initiating_finding_ids),
            "root_eligible_manifestation_only_count": self.root_eligible_manifestation_only_count,
            "root_eligible_manifestation_only_hypothesis_ids": list(
                self.root_eligible_manifestation_only_hypothesis_ids
            ),
        }


def _finding_id(finding: Finding) -> str:
    return "|".join((finding.kind.value, finding.entity.canonical, *finding.evidence_ids))


def evaluate_baseline(
    session_factory: sessionmaker[Session],
    *,
    namespaces: tuple[str, ...],
    evidence_namespaces: tuple[str, ...],
    collector_started_at: datetime,
    baseline_reference_at: datetime,
    config: EngineConfig | None = None,
) -> BaselineEvaluation:
    """Diagnose the persisted window at ``baseline_reference_at``; read-only."""
    for name, value in (
        ("collector_started_at", collector_started_at),
        ("baseline_reference_at", baseline_reference_at),
    ):
        if value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
    if collector_started_at > baseline_reference_at:
        raise ValueError("collector_started_at must not be after baseline_reference_at")
    request = ManifestRequest(
        run_id="baseline-probe",
        incident_id=NO_INCIDENT,
        correlation_id=NO_INCIDENT,
        starts_at=collector_started_at,
        ends_at=baseline_reference_at,
        window_end=baseline_reference_at,
        namespaces=frozenset(namespaces),
        journal_namespaces=frozenset((*namespaces, *evidence_namespaces)),
        snapshot_cycle_id=None,
        listed_objects=0,
        provider_capabilities=(),
    )
    with session_factory() as session:
        members = load_members(session, select_members(session, request))
        session.rollback()  # reads only; nothing to keep
    source = LiveSource(
        incident="baseline-probe",
        alert_items=[],
        journal=list(members.journal),
        current_objects=[],
        event_bodies=[body for _, body in members.events],
        event_evidence_ids=[event_evidence_id(version_id) for version_id, _ in members.events],
        error_items=[],
        observed_at=baseline_reference_at,
        current_is_live=False,
        provider_adapter=None,
        lifecycle_records=members.lifecycle,
    )
    effective = config or EngineConfig()
    case = build_case(source, effective, reference_onset=baseline_reference_at)
    trace = diagnose_case(case, config=effective).resolution_trace
    eliminations = tuple(trace.eliminations) if trace is not None else ()
    root_ineligible = {
        item.hypothesis_id
        for item in eliminations
        if item.consequence is EliminationConsequence.ROOT_INELIGIBILITY
    }
    findings = tuple(case.findings)
    hypotheses = tuple(case.hypotheses)
    return BaselineEvaluation(
        reference_at=baseline_reference_at,
        window_start=collector_started_at,
        findings=findings,
        hypotheses=hypotheses,
        eliminations=eliminations,
        initiating_finding_ids=tuple(
            sorted(
                _finding_id(item)
                for item in findings
                if item.temporal_role is EvidenceTemporalRole.INITIATING
            )
        ),
        # MANIFESTATION_ONLY is the existing semantic class: no initiating premise.
        root_eligible_manifestation_only_hypothesis_ids=tuple(
            sorted(
                item.hypothesis_id
                for item in hypotheses
                if not item.initiating_findings and item.hypothesis_id not in root_ineligible
            )
        ),
    )


__all__ = ["NO_INCIDENT", "BaselineEvaluation", "evaluate_baseline"]
