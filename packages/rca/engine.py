"""The diagnosis pipeline: observe, extract signals, rank, verify, propose."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from packages.rca.model import (
    Candidate,
    Confidence,
    Diagnosis,
    EntityRef,
    Finding,
    InvestigationStep,
    ObjectVersion,
    Symptoms,
)
from packages.rca.ranking import (
    Context,
    RankingConfig,
    collapse_fault_instances,
    score_findings,
    symptom_tokens,
    verify,
)
from packages.rca.remediation import propose
from packages.rca.signals import (
    change_findings,
    container_findings,
    dependency_findings,
    extract_symptoms,
    failure_findings,
    fault_event_findings,
    policy_findings,
    resource_findings,
    symptom_entities,
)
from packages.rca.source import ObservationSource
from packages.rca.topology import Topology, derive_edges


@dataclass
class Case:
    """Everything the engine derived for one incident, shared with the investigator."""

    incident_id: str
    source: ObservationSource
    symptoms: Symptoms
    topology: Topology
    context: Context
    findings: list[Finding]
    candidates: list[Candidate]
    steps: list[InvestigationStep] = field(default_factory=list)


@dataclass(frozen=True)
class Choice:
    """An investigator's conclusion."""

    entity: EntityRef
    rationale: str
    model_calls: int = 0


class Investigator(Protocol):
    """Reviews the ranked case and may pick a different candidate."""

    name: str

    def investigate(self, case: Case) -> Choice | None: ...


@dataclass(frozen=True)
class EngineConfig:
    ranking: RankingConfig = field(default_factory=RankingConfig)
    alternatives: int = 4
    # Metrics older than onset minus this gap are the pressure baseline.
    pressure_baseline_gap: timedelta = timedelta(minutes=5)


def _pods(entities: Iterable[EntityRef], topology: Topology) -> set[EntityRef]:
    """Pods of the alerting services, including pods of alerting workloads."""
    pods: set[EntityRef] = set()
    for entity in entities:
        if entity.kind == "Pod":
            pods.add(entity)
            continue
        for neighbor in topology.reachable(entity, max_depth=2):
            if neighbor.kind == "Pod" and topology.workload_of(neighbor) == entity:
                pods.add(neighbor)
    return pods


def build_case(source: ObservationSource, config: EngineConfig | None = None) -> Case:
    """Run every deterministic stage and return the ranked case."""
    config = config or EngineConfig()
    alerts = list(source.alerts())
    history = source.object_history()
    events = list(source.events())
    latest: dict[EntityRef, ObjectVersion] = {
        ref: versions[-1] for ref, versions in history.items()
    }
    topology = Topology(derive_edges(latest, events), latest)
    symptoms = extract_symptoms(alerts)
    entities = symptom_entities(alerts, topology)
    observed = [v.observed_at for versions in history.values() for v in versions]
    context = Context(
        symptoms=symptoms,
        symptom_entities=entities,
        topology=topology,
        window_end=max(observed) if observed else None,
        tokens=symptom_tokens(symptoms, entities, topology),
    )
    findings = [
        *change_findings(history),
        *policy_findings(history, topology, set(symptoms.namespaces), events),
        *container_findings(history),
        *(
            resource_findings(
                source.resource_pressure(
                    sorted(_pods(entities, topology), key=str),
                    symptoms.onset - config.pressure_baseline_gap,
                )
            )
            if symptoms.onset is not None
            else []
        ),
        *fault_event_findings(events, topology),
        *failure_findings(events),
        *dependency_findings(list(source.error_logs()), topology, entities),
    ]
    candidates = collapse_fault_instances(
        score_findings(findings, context, config.ranking), topology, symptoms.onset
    )
    steps = [
        InvestigationStep(
            actor="engine",
            action="symptoms",
            detail=(
                f"{len(symptoms.alert_names)} diagnostic alert type(s) on "
                f"{', '.join(symptoms.services[:6]) or 'no named service'}; "
                f"{sum(symptoms.background_alert_counts.values())} background alert(s) ignored"
            ),
        ),
        InvestigationStep(
            actor="engine",
            action="signals",
            detail=(
                f"{len(findings)} finding(s) over {len(history)} object(s) and "
                f"{len(events)} event(s); {len(topology.edges)} structural edge(s)"
            ),
        ),
        InvestigationStep(
            actor="engine",
            action="ranking",
            detail="; ".join(
                f"{c.entity.canonical} {c.score:.1f}" for c in candidates[: config.alternatives + 1]
            )
            or "no candidates",
        ),
    ]
    return Case(
        incident_id=source.incident_id(),
        source=source,
        symptoms=symptoms,
        topology=topology,
        context=context,
        findings=findings,
        candidates=candidates,
        steps=steps,
    )


def _summary(candidate: Candidate, confidence: Confidence, reason: str) -> str:
    finding = candidate.findings[0]
    linked = (
        f" Linked to {', '.join(candidate.linked_symptoms[:3])}."
        if candidate.linked_symptoms
        else ""
    )
    return (
        f"{candidate.entity.canonical}: {finding.summary}. "
        f"{confidence.value.capitalize()} ({reason}).{linked}"
    )


_RANK = {Confidence.VERIFIED: 2, Confidence.LIKELY: 1, Confidence.UNVERIFIED: 0}


def _accept_override(
    case: Case, top: Candidate, proposed: Candidate, config: EngineConfig
) -> Candidate:
    """Let the investigator replace the ranked answer only without losing verification."""
    top_confidence, _ = verify(top, case.context, config.ranking)
    new_confidence, _ = verify(proposed, case.context, config.ranking)
    if _RANK[new_confidence] >= _RANK[top_confidence]:
        return proposed
    case.steps.append(
        InvestigationStep(
            actor="engine",
            action="kept",
            detail=(
                f"kept {top.entity.canonical} ({top_confidence.value}); the proposed "
                f"{proposed.entity.canonical} is only {new_confidence.value}"
            ),
        )
    )
    return top


def diagnose(
    source: ObservationSource,
    *,
    investigator: Investigator | None = None,
    config: EngineConfig | None = None,
) -> Diagnosis:
    """Produce a diagnosis for every incident; never returns an empty answer when signals exist."""
    config = config or EngineConfig()
    case = build_case(source, config)
    if not case.candidates:
        return Diagnosis(
            incident_id=case.incident_id,
            root_cause=None,
            confidence=Confidence.UNVERIFIED,
            summary="No change, fault, or failure signal was observed.",
            symptoms=case.symptoms,
            steps=tuple(case.steps),
        )
    chosen = case.candidates[0]
    mode = "deterministic"
    model_calls = 0
    if investigator is not None:
        mode = investigator.name
        client = getattr(investigator, "client", None)
        calls_before = int(getattr(client, "calls", 0))
        choice = investigator.investigate(case)
        model_calls = int(getattr(client, "calls", 0)) - calls_before
        if client is None and choice is not None:
            model_calls = choice.model_calls
        if choice is not None:
            case.steps.append(
                InvestigationStep(
                    actor=investigator.name,
                    action="conclude",
                    detail=f"{choice.entity.canonical}: {choice.rationale}"[:500],
                )
            )
            match = next((c for c in case.candidates if c.entity == choice.entity), None)
            if match is not None and match.entity != chosen.entity:
                chosen = _accept_override(case, chosen, match, config)
    confidence, reason = verify(chosen, case.context, config.ranking)
    alternatives = tuple(c for c in case.candidates if c.entity != chosen.entity)[
        : config.alternatives
    ]
    case.steps.append(
        InvestigationStep(actor="engine", action="verify", detail=f"{confidence.value}: {reason}")
    )
    return Diagnosis(
        incident_id=case.incident_id,
        root_cause=chosen.entity,
        confidence=confidence,
        summary=_summary(chosen, confidence, reason),
        symptoms=case.symptoms,
        evidence=chosen.findings[:5],
        causal_path=chosen.causal_path,
        alternatives=alternatives,
        remediation=propose(chosen, case.topology),
        steps=tuple(case.steps),
        mode=mode,
        model_calls=model_calls,
    )


__all__ = ["Case", "Choice", "EngineConfig", "Investigator", "build_case", "diagnose"]
