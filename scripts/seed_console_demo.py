"""Seed the local database with a realistic incident mix for the console demo.

This inserts incidents, stored diagnoses, bound timeline events, alerts and
evidence directly through the repositories, so the operator console renders
against the real /api/v1/console endpoints without needing a live cluster. It
is demo/dev scaffolding — never part of a benchmark run.

    DATABASE_URL=sqlite:///.local/agentic-sre.db python scripts/seed_console_demo.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.contracts import (
    Incident,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.rca.model import (
    CausalHop,
    Confidence,
    Diagnosis,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    Resolution,
    Symptoms,
)
from packages.storage.database import create_session_factory
from packages.storage.models import AlertRow, Base, EvidenceRow
from packages.storage.repositories import (
    DiagnosisRepository,
    IncidentEventRepository,
    IncidentRepository,
)

PHASES = [
    (IncidentEventType.DIAGNOSIS_STARTED, 0.0),
    (IncidentEventType.EVIDENCE_GATHERED, 4.5),
    (IncidentEventType.RCA_ENGINE_COMPLETED, 22.0),
    (IncidentEventType.DIAGNOSIS_COMPLETED, 24.0),
]


@dataclass(frozen=True)
class Scenario:
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    service: str
    actor_kind: str
    actor_name: str
    finding_kind: FindingKind
    confidence: Confidence | None
    resolution: Resolution | None
    minutes_ago: int
    alert_name: str
    summary: str


SCENARIOS = [
    Scenario(
        title="OrderDependencyLatencyHigh",
        severity=IncidentSeverity.CRITICAL,
        status=IncidentStatus.INVESTIGATING,
        service="order-service",
        actor_kind="Deployment",
        actor_name="payment-service",
        finding_kind=FindingKind.SPEC_CHANGE,
        confidence=Confidence.VERIFIED,
        resolution=Resolution.RESOLVED,
        minutes_ago=6,
        alert_name="OrderDependencyLatencyHigh",
        summary="A payment-service spec change 30s before onset initiated the order latency.",
    ),
    Scenario(
        title="CheckoutErrorRateHigh",
        severity=IncidentSeverity.CRITICAL,
        status=IncidentStatus.INVESTIGATING,
        service="checkout-service",
        actor_kind="ConfigMap",
        actor_name="checkout-config",
        finding_kind=FindingKind.CONFIG_CHANGE,
        confidence=Confidence.LIKELY,
        resolution=Resolution.AMBIGUOUS,
        minutes_ago=14,
        alert_name="CheckoutErrorRateHigh",
        summary="A checkout-config change and a concurrent rollout both fit; neither is excluded.",
    ),
    Scenario(
        title="PaymentSaturation",
        severity=IncidentSeverity.WARNING,
        status=IncidentStatus.INVESTIGATING,
        service="payment-service",
        actor_kind="Deployment",
        actor_name="payment-service",
        finding_kind=FindingKind.SCALE_CHANGE,
        confidence=Confidence.VERIFIED,
        resolution=Resolution.RESOLVED,
        minutes_ago=41,
        alert_name="PaymentSaturation",
        summary="payment-service was scaled down to 1 replica just before saturation.",
    ),
    Scenario(
        title="InventorySyncErrors",
        severity=IncidentSeverity.WARNING,
        status=IncidentStatus.INVESTIGATING,
        service="inventory-service",
        actor_kind="Deployment",
        actor_name="inventory-service",
        finding_kind=FindingKind.DEPENDENCY_ERRORS,
        confidence=Confidence.UNVERIFIED,
        resolution=Resolution.INSUFFICIENT_EVIDENCE,
        minutes_ago=78,
        alert_name="InventorySyncErrors",
        summary="Runtime errors observed, but no durable change aligns with onset.",
    ),
    Scenario(
        title="FrontendLatencyHigh",
        severity=IncidentSeverity.INFO,
        status=IncidentStatus.OPEN,
        service="frontend",
        actor_kind="Deployment",
        actor_name="frontend",
        finding_kind=FindingKind.SPEC_CHANGE,
        confidence=None,
        resolution=None,  # still diagnosing — no stored diagnosis
        minutes_ago=1,
        alert_name="FrontendLatencyHigh",
        summary="",
    ),
]


def _diagnosis(incident_id: UUID, onset: datetime, scenario: Scenario) -> Diagnosis:
    actor = EntityRef(kind=scenario.actor_kind, name=scenario.actor_name, namespace="sre-demo")
    symptom = EntityRef(kind="Deployment", name=scenario.service, namespace="sre-demo")
    finding = Finding(
        kind=scenario.finding_kind,
        entity=actor,
        at=onset - timedelta(seconds=30),
        summary=f"{scenario.actor_name} {scenario.finding_kind.value} 30s before onset",
        evidence_ids=("obj-1",),
        temporal_role=EvidenceTemporalRole.INITIATING,
        onset_delta_seconds=-30.0,
    )
    resolved = scenario.resolution is Resolution.RESOLVED
    return Diagnosis(
        incident_id=str(incident_id),
        root_cause=actor if scenario.resolution is not Resolution.INSUFFICIENT_EVIDENCE else None,
        confidence=scenario.confidence or Confidence.UNVERIFIED,
        resolution=scenario.resolution or Resolution.INSUFFICIENT_EVIDENCE,
        summary=scenario.summary,
        symptoms=Symptoms(
            onset=onset,
            last_seen=onset,
            services=(scenario.service,),
            namespaces=("sre-demo",),
            alert_names=(scenario.alert_name,),
        ),
        evidence=(finding,),
        causal_path=(
            (CausalHop(source=actor, relation="dependency", target=symptom),) if resolved else ()
        ),
        causal_explanation="LINKED" if resolved else "UNLINKED",
        hypothesis=Hypothesis(
            hypothesis_id="h1", causal_actor=actor, initiating_findings=(finding,)
        ),
    )


def seed(session: Session, now: datetime) -> int:
    incidents = IncidentRepository(session)
    diagnoses = DiagnosisRepository(session)
    events = IncidentEventRepository(session)
    count = 0
    for scenario in SCENARIOS:
        onset = now - timedelta(minutes=scenario.minutes_ago)
        incident = Incident(
            status=scenario.status,
            severity=scenario.severity,
            source=IncidentSource.ALERTMANAGER,
            title=scenario.title,
            description=f"{scenario.service} affected",
            created_at=onset,
            updated_at=onset + timedelta(seconds=24),
        )
        incidents.create(incident)
        count += 1
        session.add(
            AlertRow(
                alert_id=uuid4(),
                incident_id=incident.incident_id,
                alert_name=scenario.alert_name,
                service=scenario.service,
                namespace="sre-demo",
                cluster="kind",
                starts_at=onset - timedelta(seconds=2),
                ends_at=None,
                labels={"severity": scenario.severity.value.lower()},
                annotations={},
                fingerprint=f"fp-{scenario.title}",
                status="FIRING",
                source="PROMETHEUS",
            )
        )
        session.commit()
        if scenario.resolution is None:
            continue

        run_id = str(uuid4())
        diagnoses.save(
            incident.incident_id,
            _diagnosis(incident.incident_id, onset, scenario).model_dump(mode="json"),
            created_at=onset + timedelta(seconds=24),
            run_id=run_id,
        )
        for event_type, offset in PHASES:
            payload: dict[str, object] = {"run_id": run_id}
            if event_type is IncidentEventType.EVIDENCE_GATHERED:
                payload.update(objects=43, journal=17, events=9, logs=12)
            elif event_type is IncidentEventType.RCA_ENGINE_COMPLETED:
                payload.update(
                    leading_actor=f"sre-demo/{scenario.actor_kind}/{scenario.actor_name}", reads=6
                )
            elif event_type is IncidentEventType.DIAGNOSIS_COMPLETED:
                payload.update(
                    root_cause=f"sre-demo/{scenario.actor_kind}/{scenario.actor_name}",
                    resolution=scenario.resolution.value,
                )
            events.append(
                IncidentEvent(
                    incident_id=incident.incident_id,
                    event_type=event_type,
                    timestamp=onset + timedelta(seconds=offset),
                    correlation_id=incident.correlation_id,
                    payload=payload,
                )
            )
        session.add(
            EvidenceRow(
                evidence_id=uuid4(),
                incident_id=incident.incident_id,
                source_type="KUBERNETES",
                source_system="cluster-reader",
                observation={"kind": scenario.actor_kind, "name": scenario.actor_name},
                time_window={
                    "starts_at": (onset - timedelta(minutes=2)).isoformat(),
                    "ends_at": onset.isoformat(),
                },
                tool_call_id=uuid4(),
                raw_result_reference="obj-1",
                collected_at=onset + timedelta(seconds=5),
            )
        )
        session.commit()
    return count


def main() -> None:
    url = os.environ.get("DATABASE_URL", "sqlite:///.local/agentic-sre.db")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        seeded = seed(session, datetime.now(UTC))
    print(f"seeded {seeded} incidents into {url}")


if __name__ == "__main__":
    main()
