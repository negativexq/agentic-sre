"""The diagnosis pipeline: observe, extract signals, rank, verify, propose."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from packages.rca.frontier import derive_structural_frontier, investigation_status
from packages.rca.hypotheses import (
    GroupingResult,
    group_candidates,
    hypothesis_candidate,
)
from packages.rca.information_gap import derive_information_gaps
from packages.rca.model import (
    Candidate,
    Confidence,
    Diagnosis,
    EntityRef,
    Finding,
    Hypothesis,
    HypothesisDiagnostics,
    InvestigationStep,
    ObjectVersion,
    Resolution,
    StructuralAlternative,
    Symptoms,
)
from packages.rca.ranking import (
    Context,
    RankingConfig,
    annotate_temporal_roles,
    collapse_fault_instances,
    score_findings,
    symptom_tokens,
    verification_trace,
    verify,
)
from packages.rca.remediation import propose
from packages.rca.resolution import hypothesis_signature, resolve_hypotheses
from packages.rca.signals import (
    autoscaling_findings,
    change_findings,
    container_findings,
    dependency_findings,
    extract_symptoms,
    failure_findings,
    fault_event_findings,
    policy_findings,
    resource_findings,
    symptom_entities,
    traffic_findings,
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
    hypotheses: list[Hypothesis]
    hypothesis_diagnostics: HypothesisDiagnostics
    structural_alternatives: list[StructuralAlternative] = field(default_factory=list)
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


def build_case(
    source: ObservationSource,
    config: EngineConfig | None = None,
    extra_findings: Sequence[Finding] = (),
) -> Case:
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
    context = Context(
        symptoms=symptoms,
        symptom_entities=entities,
        topology=topology,
        window_end=source.observation_cutoff(),
        tokens=symptom_tokens(symptoms, entities, topology),
    )
    findings = [
        *change_findings(history),
        *policy_findings(history, topology, set(symptoms.namespaces), events),
        *autoscaling_findings(history, events, topology),
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
        *traffic_findings(
            list(source.traffic_observations()), symptoms.onset, source.observation_cutoff()
        ),
    ]
    findings.extend(extra_findings)
    findings = annotate_temporal_roles(
        findings, symptoms.onset, config.ranking.verification_onset_grace
    )
    candidates = score_findings(findings, context, config.ranking)
    candidates = collapse_fault_instances(candidates, topology, symptoms.onset)
    grouping: GroupingResult = group_candidates(candidates, topology, context, config.ranking)
    hypotheses = list(grouping.hypotheses)
    structural_alternatives = (
        list(derive_structural_frontier(context, candidates))
        if getattr(source, "initial_observation_bounded", False)
        else []
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
                f"{h.causal_actor.canonical} {h.score:.1f} ({len(h.members)} member(s))"
                for h in hypotheses[: config.alternatives + 1]
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
        hypotheses=hypotheses,
        structural_alternatives=structural_alternatives,
        hypothesis_diagnostics=grouping.diagnostics,
        steps=steps,
    )


def _summary(candidate: Candidate, confidence: Confidence, reason: str) -> str:
    finding = candidate.findings[0] if candidate.findings else None
    linked = (
        f" Linked to {', '.join(candidate.linked_symptoms[:3])}."
        if candidate.linked_symptoms
        else ""
    )
    evidence = (
        finding.summary
        if finding is not None
        else "structurally plausible actor without observed causal evidence"
    )
    return f"{candidate.entity.canonical}: {evidence}. {confidence.value.capitalize()} ({reason}).{linked}"


def _selected_hypothesis(case: Case, entity: EntityRef) -> Hypothesis | None:
    """Resolve an entity choice to its containing causal episode."""
    return next(
        (
            hypothesis
            for hypothesis in case.hypotheses
            if entity == hypothesis.causal_actor or entity in hypothesis.members
        ),
        None,
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


def diagnose_case(
    case: Case,
    *,
    investigator: Investigator | None = None,
    config: EngineConfig | None = None,
) -> Diagnosis:
    """Diagnose an already-built case without rereading its observation source."""
    config = config or EngineConfig()
    if not case.candidates:
        resolution_trace = resolve_hypotheses(())
        information_gaps = derive_information_gaps(
            (),
            resolution_trace,
            case.source,
            structural_alternatives=case.structural_alternatives,
        )
        return Diagnosis(
            incident_id=case.incident_id,
            root_cause=None,
            confidence=Confidence.UNVERIFIED,
            resolution=Resolution.INSUFFICIENT_EVIDENCE,
            summary="No change, fault, or failure signal was observed.",
            symptoms=case.symptoms,
            steps=tuple(case.steps),
            hypothesis_diagnostics=case.hypothesis_diagnostics,
            resolution_trace=resolution_trace,
            information_gaps=information_gaps,
            structural_alternatives=tuple(case.structural_alternatives),
            investigation_status=investigation_status(
                case.structural_alternatives,
                bounded=bool(getattr(case.source, "initial_observation_bounded", False)),
            ),
        )
    # Resolution compares immutable evidence structures.  Attach the same
    # signatures to the serialized hypotheses so API consumers can inspect the
    # comparison without reconstructing it from entity names.
    case.hypotheses = [
        hypothesis.model_copy(update={"signature": hypothesis_signature(hypothesis)})
        for hypothesis in case.hypotheses
    ]
    verification_traces = {
        hypothesis.hypothesis_id: verification_trace(
            hypothesis_candidate(hypothesis), case.context, config.ranking
        )
        for hypothesis in case.hypotheses[: config.alternatives + 4]
    }
    resolution_trace = resolve_hypotheses(case.hypotheses, verification_traces=verification_traces)
    information_gaps = derive_information_gaps(
        case.hypotheses,
        resolution_trace,
        case.source,
        structural_alternatives=case.structural_alternatives,
    )
    selected = case.hypotheses[0]
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
            proposed = _selected_hypothesis(case, choice.entity)
            if proposed is not None and proposed.hypothesis_id != selected.hypothesis_id:
                current_candidate = hypothesis_candidate(selected)
                proposed_candidate = hypothesis_candidate(proposed)
                accepted = _accept_override(case, current_candidate, proposed_candidate, config)
                if accepted.entity == proposed.causal_actor:
                    selected = proposed
    selected_candidate = hypothesis_candidate(selected)
    runner_up = (
        hypothesis_candidate(case.hypotheses[1])
        if selected.hypothesis_id == case.hypotheses[0].hypothesis_id and len(case.hypotheses) > 1
        else None
    )
    trace = verification_trace(
        selected_candidate, case.context, config.ranking, runner_up=runner_up
    )
    assert trace.decision is not None
    confidence, reason = trace.decision, trace.rationale
    alternatives = tuple(c for c in case.candidates if c.entity not in selected.members)[
        : config.alternatives
    ]
    alternative_hypotheses = tuple(
        hypothesis
        for hypothesis in case.hypotheses
        if hypothesis.hypothesis_id != selected.hypothesis_id
    )[: config.alternatives]
    case.steps.append(
        InvestigationStep(actor="engine", action="verify", detail=f"{confidence.value}: {reason}")
    )
    case.steps.append(
        InvestigationStep(
            actor="engine",
            action="resolution",
            detail=f"{resolution_trace.state.value}: {resolution_trace.rationale}",
        )
    )
    ambiguous_hypotheses = (
        tuple(
            hypothesis
            for hypothesis in case.hypotheses
            if hypothesis.hypothesis_id in resolution_trace.leading_hypothesis_ids
        )
        if resolution_trace.state is Resolution.AMBIGUOUS
        else ()
    )
    return Diagnosis(
        incident_id=case.incident_id,
        root_cause=selected.causal_actor,
        confidence=confidence,
        resolution=resolution_trace.state,
        summary=_summary(selected_candidate, confidence, reason),
        symptoms=case.symptoms,
        evidence=selected.findings[:5],
        causal_path=selected.causal_paths[0] if selected.causal_paths else (),
        causal_explanation=selected.causal_explanation,
        alternatives=alternatives,
        hypothesis=selected,
        alternative_hypotheses=alternative_hypotheses,
        hypothesis_diagnostics=case.hypothesis_diagnostics,
        remediation=propose(selected_candidate, case.topology),
        steps=tuple(case.steps),
        mode=mode,
        model_calls=model_calls,
        verification=trace,
        resolution_trace=resolution_trace,
        ambiguous_hypotheses=ambiguous_hypotheses,
        information_gaps=information_gaps,
        structural_alternatives=tuple(case.structural_alternatives),
        investigation_status=investigation_status(
            case.structural_alternatives,
            bounded=bool(getattr(case.source, "initial_observation_bounded", False)),
        ),
    )


def diagnose(
    source: ObservationSource,
    *,
    investigator: Investigator | None = None,
    config: EngineConfig | None = None,
) -> Diagnosis:
    """Produce a diagnosis for every incident; deterministic by default."""
    effective_config = config or EngineConfig()
    return diagnose_case(
        build_case(source, effective_config), investigator=investigator, config=effective_config
    )


__all__ = [
    "Case",
    "Choice",
    "EngineConfig",
    "Investigator",
    "build_case",
    "diagnose",
    "diagnose_case",
]
