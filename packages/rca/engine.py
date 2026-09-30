"""The diagnosis pipeline: observe, extract signals, rank, verify, propose."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from packages.rca.causal_roles import HypothesisCausalRoles, derive_hypothesis_causal_roles
from packages.rca.channels import attach_channel_assessments, channel_assessments
from packages.rca.claims import admitted
from packages.rca.episode_end import RULE_ID as EPISODE_END_RULE_ID
from packages.rca.episode_end import evaluate_ended_episodes
from packages.rca.frontier import (
    apply_frontier_progress,
    derive_structural_frontier,
    investigation_status,
    material_frontier,
)
from packages.rca.hypotheses import (
    GroupingResult,
    group_candidates,
    hypothesis_candidate,
)
from packages.rca.information_gap import (
    InformationGapContext,
    authorized_event_namespaces,
    derive_information_gaps,
    incident_namespaces,
)
from packages.rca.mechanism_bridge import (
    RuntimeMechanismBridges,
    derive_runtime_mechanism_bridges,
)
from packages.rca.model import (
    Alert,
    Candidate,
    ClusterEvent,
    Confidence,
    Diagnosis,
    EntityRef,
    Finding,
    Hypothesis,
    HypothesisDiagnostics,
    InvestigationStep,
    ObjectVersion,
    PreconditionAuditReason,
    PreconditionResult,
    ProviderReadFailure,
    RequirementEvaluation,
    Resolution,
    ResolutionTrace,
    StructuralAlternative,
    Symptoms,
    TimingAssessment,
    TimingStability,
)
from packages.rca.ranking import (
    Context,
    RankingConfig,
    annotate_temporal_roles,
    finding_in_window,
    score_findings,
    symptom_tokens,
    verification_trace,
    verify,
)
from packages.rca.remediation import propose
from packages.rca.requirements import build_requirement_evaluations, hypothesis_inventory
from packages.rca.resolution import hypothesis_signature, resolve_hypotheses
from packages.rca.resource_mechanism import RULE_ID as RESOURCE_RULE_ID
from packages.rca.resource_mechanism import MechanismMismatch, evaluate_resource_mechanisms
from packages.rca.root_cause_eligibility import (
    RootCauseEligibilities,
    derive_root_cause_eligibilities,
)
from packages.rca.runtime_evidence import RuntimeEvidence, derive_runtime_evidence
from packages.rca.runtime_graph import (
    CanonicalTraceIndex,
    RuntimeGraph,
    canonicalize_trace_spans,
    derive_runtime_graph_from_index,
)
from packages.rca.runtime_propagation import RuntimePropagation, derive_runtime_propagation
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
from packages.rca.timing_stability import (
    OnsetView,
    applied_timing_masks,
    claim_views,
    compute_timing_assessment,
    derive_onset_uncertainty,
    derive_timing_masks,
    source_alert_episodes,
)
from packages.rca.topology import Topology, derive_edges, with_runtime_propagation


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
    runtime_graph: RuntimeGraph = field(default_factory=RuntimeGraph.empty)
    runtime_evidence: RuntimeEvidence = field(default_factory=RuntimeEvidence.empty)
    runtime_propagation: RuntimePropagation = field(default_factory=RuntimePropagation.empty)
    hypothesis_causal_roles: HypothesisCausalRoles = field(
        default_factory=HypothesisCausalRoles.empty
    )
    root_cause_eligibilities: RootCauseEligibilities = field(
        default_factory=RootCauseEligibilities.empty
    )
    runtime_mechanism_bridges: RuntimeMechanismBridges = field(
        default_factory=RuntimeMechanismBridges.empty
    )
    structural_alternatives: list[StructuralAlternative] = field(default_factory=list)
    steps: list[InvestigationStep] = field(default_factory=list)
    mechanism_mismatches: dict[str, MechanismMismatch] = field(default_factory=dict)
    rule_preconditions: dict[tuple[str, str], tuple[PreconditionResult, ...]] = field(
        default_factory=dict
    )
    precondition_reasons: dict[tuple[str, str], tuple[PreconditionAuditReason, ...]] = field(
        default_factory=dict
    )
    requirement_evaluations: tuple[RequirementEvaluation, ...] = ()
    # What re-assessing this case against another onset needs (never part of a decision).
    extra_findings: tuple[Finding, ...] = ()
    inputs: CaseInputs | None = field(default=None, repr=False, compare=False)
    # Set when this case only re-derives the evidence against another admissible onset.
    assessed_onset: datetime | None = None


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


# The deterministic RCA semantics a run was diagnosed with, persisted on every
# revision. Bump it with any change that can alter a diagnosis from the same
# evidence; replay refuses a run recorded under another version (M20.3a).
RCA_ENGINE_VERSION = "2.1.0"


@dataclass(frozen=True)
class EngineConfig:
    ranking: RankingConfig = field(default_factory=RankingConfig)
    alternatives: int = 4
    # Metrics older than onset minus this gap are the pressure baseline.
    pressure_baseline_gap: timedelta = timedelta(minutes=5)
    # Explicit infrastructure namespaces that may be queried only by the
    # incident-scoped event discovery capability.
    auxiliary_event_namespaces: tuple[str, ...] = ()
    # Assess every decision against the evidence-derived onset set (M21 timing contract).
    timing_stability: bool = True

    def __post_init__(self) -> None:
        normalized = tuple(
            sorted({value.strip() for value in self.auxiliary_event_namespaces if value.strip()})
        )
        object.__setattr__(self, "auxiliary_event_namespaces", normalized[:4])


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


@dataclass(frozen=True)
class CaseInputs:
    """The onset-independent, source-derived part of a case.

    Everything here depends only on what the source observed, never on the incident onset,
    so one diagnosis can reuse it to assess the same evidence against several onsets
    (M21 timing contract). Building it once is the expensive step (trace-derived runtime
    evidence); the onset-dependent assembly on top of it is comparatively cheap.
    """

    alerts: tuple[Alert, ...]
    history: Mapping[EntityRef, Sequence[ObjectVersion]]
    events: tuple[ClusterEvent, ...]
    topology: Topology
    trace_index: CanonicalTraceIndex
    runtime_graph: RuntimeGraph
    runtime_evidence: RuntimeEvidence


def prepare_case_inputs(source: ObservationSource) -> CaseInputs:
    """Read the source once and derive every onset-independent structure."""
    alerts = tuple(source.alerts())
    history = source.object_history()
    events = tuple(source.events())
    latest: dict[EntityRef, ObjectVersion] = {
        ref: versions[-1] for ref, versions in history.items()
    }
    topology = Topology(derive_edges(latest, list(events)), latest)
    trace_spans = source.trace_observations()
    trace_index = canonicalize_trace_spans(trace_spans)
    return CaseInputs(
        alerts=alerts,
        history=history,
        events=events,
        topology=topology,
        trace_index=trace_index,
        runtime_graph=derive_runtime_graph_from_index(trace_index),
        runtime_evidence=derive_runtime_evidence(trace_index),
    )


def build_case(
    source: ObservationSource,
    config: EngineConfig | None = None,
    extra_findings: Sequence[Finding] = (),
    reference_onset: datetime | None = None,
    *,
    assessed_onset: datetime | None = None,
    inputs: CaseInputs | None = None,
) -> Case:
    """Run every deterministic stage and return the ranked case.

    ``reference_onset`` is only for an incident-free probe (M19-6.8): with no
    alerts there is no onset, so it stands in for one in the existing
    onset-relative semantics. It is refused when alerts exist, and the probe
    never persists it.

    ``assessed_onset`` re-derives the case as if the incident onset were that value while
    every observation stays as it was (M21 timing contract). It is an assessment of the same
    evidence, never a new onset of record: the result is not persisted and not reported.
    ``inputs`` reuses an earlier :func:`prepare_case_inputs` of the same source.
    """
    config = config or EngineConfig()
    if inputs is None:
        inputs = prepare_case_inputs(source)
    alerts = list(inputs.alerts)
    if reference_onset is not None and alerts:
        raise ValueError("reference_onset is only for an incident-free probe without alerts")
    history = inputs.history
    events = list(inputs.events)
    topology = inputs.topology
    trace_index = inputs.trace_index
    runtime_graph = inputs.runtime_graph
    runtime_evidence = inputs.runtime_evidence
    symptoms = extract_symptoms(alerts, alert_observation_start=source.alert_observation_start())
    if reference_onset is not None:
        symptoms = symptoms.model_copy(
            update={"onset": reference_onset, "reference_time": reference_onset}
        )
    if assessed_onset is not None:
        symptoms = symptoms.model_copy(
            update={"onset": assessed_onset, "reference_time": assessed_onset}
        )
    runtime_propagation = derive_runtime_propagation(
        trace_index,
        history=history,
        incident_onset=symptoms.onset,
    )
    topology = with_runtime_propagation(topology, runtime_propagation)
    entities = symptom_entities(alerts, topology)
    context = Context(
        symptoms=symptoms,
        symptom_entities=entities,
        topology=topology,
        window_end=source.observation_cutoff(),
        tokens=symptom_tokens(symptoms, entities, topology),
    )
    # A query window, not authority: the reference time may anchor it.
    pressure_result = (
        source.resource_pressure(
            sorted(_pods(entities, topology), key=str),
            symptoms.reference_time - config.pressure_baseline_gap,
        )
        if symptoms.reference_time is not None
        else ()
    )
    pressure_records = () if isinstance(pressure_result, ProviderReadFailure) else pressure_result
    findings = [
        *change_findings(history),
        *policy_findings(history, topology, set(symptoms.namespaces), events),
        *autoscaling_findings(history, events, topology),
        *container_findings(history),
        *resource_findings(pressure_records),
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
    runtime_mechanism_bridges = derive_runtime_mechanism_bridges(
        findings,
        runtime_evidence,
        history=history,
    )
    candidates = score_findings(findings, context, config.ranking)
    grouping: GroupingResult = group_candidates(candidates, topology, context, config.ranking)
    hypotheses = list(grouping.hypotheses)
    hypothesis_causal_roles = derive_hypothesis_causal_roles(hypotheses, runtime_propagation)
    episode_evaluations = evaluate_ended_episodes(
        hypotheses,
        history=history,
        pod_statuses=source.pod_status_observations(),
        onset=symptoms.onset,
        grace=config.ranking.verification_onset_grace,
        evaluation_at=context.window_end,
    )
    root_cause_eligibilities = derive_root_cause_eligibilities(
        hypotheses, hypothesis_causal_roles
    ).with_ended_episodes(episode_evaluations.ended_episodes)
    resource_evaluations = evaluate_resource_mechanisms(
        hypotheses,
        history=history,
        findings=findings,
        read_pressure=source.resource_pressure,
        onset=symptoms.onset,
        grace=config.ranking.verification_onset_grace,
        evaluation_at=context.window_end,
    )
    structural_alternatives = list(derive_structural_frontier(context))
    structural_alternatives = list(
        apply_frontier_progress(
            structural_alternatives,
            hypotheses=hypotheses,
            queried_dimensions_by_alternative={},
        )
    )
    material = {
        item.alternative_id: item for item in material_frontier(structural_alternatives, hypotheses)
    }
    structural_alternatives = [
        material.get(item.alternative_id, item) for item in structural_alternatives
    ]
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
        runtime_graph=runtime_graph,
        runtime_evidence=runtime_evidence,
        runtime_propagation=runtime_propagation,
        hypothesis_causal_roles=hypothesis_causal_roles,
        root_cause_eligibilities=root_cause_eligibilities,
        runtime_mechanism_bridges=runtime_mechanism_bridges,
        structural_alternatives=structural_alternatives,
        hypothesis_diagnostics=grouping.diagnostics,
        steps=steps,
        extra_findings=tuple(extra_findings),
        inputs=inputs,
        assessed_onset=assessed_onset,
        mechanism_mismatches=dict(resource_evaluations.mismatches),
        requirement_evaluations=build_requirement_evaluations(
            hypotheses,
            instance_requirements=episode_evaluations.instance_requirements,
            coverage_requirements=resource_evaluations.requirements,
        ),
        rule_preconditions={
            **{
                (hypothesis_id, EPISODE_END_RULE_ID): results
                for hypothesis_id, results in episode_evaluations.preconditions.items()
            },
            **{
                (hypothesis_id, RESOURCE_RULE_ID): results
                for hypothesis_id, results in resource_evaluations.preconditions.items()
            },
        },
        precondition_reasons={
            **{
                (hypothesis_id, EPISODE_END_RULE_ID): reasons
                for hypothesis_id, reasons in episode_evaluations.reasons.items()
            },
            **{
                (hypothesis_id, RESOURCE_RULE_ID): reasons
                for hypothesis_id, reasons in resource_evaluations.reasons.items()
            },
        },
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


def _root_cause_selectable_hypotheses(case: Case) -> tuple[Hypothesis, ...]:
    """Preserve ranking order while excluding only explicit propagated effects."""
    return tuple(
        hypothesis
        for hypothesis in case.hypotheses
        if admitted(hypothesis)
        and case.root_cause_eligibilities.is_root_cause_selectable(hypothesis.hypothesis_id)
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


def _requirement_provenance(case: Case) -> dict[str, Any]:
    """Requirement lifecycle provenance for the diagnosis; no decision reads it."""
    return {
        "requirement_evaluations": case.requirement_evaluations,
        "hypothesis_inventory": hypothesis_inventory(case.hypotheses),
    }


def _resolution_trace(case: Case, config: EngineConfig) -> ResolutionTrace:
    """Resolve a non-empty case: attach signatures, verify, then resolve.

    Resolution compares immutable evidence structures. The same signatures are attached to
    the serialized hypotheses so API consumers can inspect the comparison without
    reconstructing it from entity names.
    """
    case.hypotheses = [
        hypothesis.model_copy(update={"signature": hypothesis_signature(hypothesis)})
        for hypothesis in case.hypotheses
    ]
    verification_traces = {
        hypothesis.hypothesis_id: verification_trace(
            hypothesis_candidate(hypothesis), case.context, config.ranking
        )
        for hypothesis in case.hypotheses
    }
    return resolve_hypotheses(
        case.hypotheses,
        verification_traces=verification_traces,
        onset_grace=config.ranking.verification_onset_grace,
        root_cause_eligibilities=case.root_cause_eligibilities,
        mechanism_mismatches=case.mechanism_mismatches,
        rule_preconditions=case.rule_preconditions,
        precondition_reasons=case.precondition_reasons,
        structural_alternatives=case.structural_alternatives,
        events=case.source.events(),
        runtime_propagation=case.runtime_propagation,
    )


def _without_ended_episodes(
    eligibilities: RootCauseEligibilities, hypothesis_ids: frozenset[str]
) -> RootCauseEligibilities:
    """Eligibilities without the ended-episode exclusion of the given claims."""
    return RootCauseEligibilities(
        eligibilities.assessments,
        {k: v for k, v in eligibilities.ended_episodes.items() if k not in hypothesis_ids},
    )


def assess_case_timing(
    case: Case, config: EngineConfig, trace: ResolutionTrace
) -> TimingAssessment:
    """How far this revision's decisions hold over every onset its evidence admits.

    The admissible onsets come only from the alert captures at or before the revision cutoff.
    Every onset re-derives the same observations (same source, same investigation findings)
    against that onset; no observation changes. Without capture history, or when the case was
    not built at the onset of record, nothing is claimed.
    """
    source = case.source
    uncertainty = derive_onset_uncertainty(
        source_alert_episodes(source),
        alert_observation_start=source.alert_observation_start(),
        cutoff=source.observation_cutoff(),
    )
    if uncertainty.assessable and uncertainty.h0 != case.symptoms.onset:
        uncertainty = uncertainty.model_copy(
            update={"reason": "ONSET_OF_RECORD_MISMATCH", "members": ()}
        )
    if not uncertainty.assessable or uncertainty.h0 is None:
        return TimingAssessment(uncertainty=uncertainty, status=TimingStability.UNASSESSED)
    views = {
        uncertainty.h0: OnsetView(
            diagnosis_status=trace.diagnosis_status,
            claims=claim_views(case.hypotheses, trace),
        )
    }
    for member in uncertainty.members:
        if member.onset == uncertainty.h0:
            continue
        assessed = build_case(
            source,
            config,
            extra_findings=case.extra_findings,
            assessed_onset=member.onset,
            inputs=case.inputs,
        )
        if not assessed.candidates:
            views[member.onset] = OnsetView(diagnosis_status="INSUFFICIENT_EVIDENCE", claims={})
            continue
        assessed_trace = _resolution_trace(assessed, config)
        views[member.onset] = OnsetView(
            diagnosis_status=assessed_trace.diagnosis_status,
            claims=claim_views(assessed.hypotheses, assessed_trace),
        )
    return compute_timing_assessment(uncertainty, views)


def diagnose_case(
    case: Case,
    *,
    investigator: Investigator | None = None,
    config: EngineConfig | None = None,
) -> Diagnosis:
    """Diagnose an already-built case without rereading its observation source."""
    config = config or EngineConfig()
    if not case.candidates:
        resolution_trace = resolve_hypotheses(
            (),
            onset_grace=config.ranking.verification_onset_grace,
            root_cause_eligibilities=case.root_cause_eligibilities,
            mechanism_mismatches=case.mechanism_mismatches,
            rule_preconditions=case.rule_preconditions,
            precondition_reasons=case.precondition_reasons,
            structural_alternatives=case.structural_alternatives,
        )
        information_gaps = derive_information_gaps(
            (),
            resolution_trace,
            case.source,
            structural_alternatives=case.structural_alternatives,
            runtime_context=InformationGapContext(
                causal_roles=case.hypothesis_causal_roles,
                root_cause_eligibilities=case.root_cause_eligibilities,
                runtime_propagation=case.runtime_propagation,
                runtime_mechanism_bridges=case.runtime_mechanism_bridges,
                topology=case.context.topology,
                symptom_entities=frozenset(case.context.symptom_entities),
            ),
            discovery_event_namespaces=authorized_event_namespaces(case, config),
            discovery_change_namespaces=incident_namespaces(case),
        )
        return Diagnosis(
            decision_semantics="m21.v3",
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
            alternative_hypotheses=tuple(case.hypotheses),
            **_requirement_provenance(case),
        )
    resolution_trace = _resolution_trace(case, config)
    timing: TimingAssessment | None = None
    if config.timing_stability and case.assessed_onset is None:
        timing = assess_case_timing(case, config, resolution_trace)
        masks = derive_timing_masks(case.hypotheses, resolution_trace, timing)
        if masks.any():
            # Authority that is not timing-stable is withheld and the case is resolved again
            # under that withholding. Ended-episode ineligibility is dropped for the same
            # claims so root selection below sees them as the resolution does (§5).
            case.root_cause_eligibilities = _without_ended_episodes(
                case.root_cause_eligibilities, masks.ended
            )
            with applied_timing_masks(masks):
                resolution_trace = _resolution_trace(case, config)
            timing = timing.model_copy(update={"withheld": masks.withheld})
    answers = {a.alternative_id: a for a in resolution_trace.frontier_answers}
    case.structural_alternatives = [
        a.model_copy(update={"answer": answers.get(a.alternative_id)})
        for a in case.structural_alternatives
    ]
    # M21 F1: influence-channel coverage records, attached after the decision (audit only).
    resolution_trace = attach_channel_assessments(
        resolution_trace,
        channel_assessments(
            case.hypotheses,
            topology=case.topology,
            history=case.source.object_history(),
            symptom_entities=set(case.context.symptom_entities),
            symptom_services=case.symptoms.services,
            runtime_graph=case.runtime_graph,
            trace_spans=case.source.trace_observations(),
            onset=case.symptoms.onset,
            grace=config.ranking.verification_onset_grace,
            events=case.source.events(),
        ),
    )
    if timing is not None:
        resolution_trace = resolution_trace.model_copy(update={"timing": timing})
    information_gaps = derive_information_gaps(
        case.hypotheses,
        resolution_trace,
        case.source,
        structural_alternatives=case.structural_alternatives,
        runtime_context=InformationGapContext(
            causal_roles=case.hypothesis_causal_roles,
            root_cause_eligibilities=case.root_cause_eligibilities,
            runtime_propagation=case.runtime_propagation,
            runtime_mechanism_bridges=case.runtime_mechanism_bridges,
            topology=case.context.topology,
            symptom_entities=frozenset(case.context.symptom_entities),
        ),
        discovery_event_namespaces=authorized_event_namespaces(case, config),
        discovery_change_namespaces=incident_namespaces(case),
    )
    selectable_hypotheses = tuple(
        h
        for h in _root_cause_selectable_hypotheses(case)
        if h.hypothesis_id not in resolution_trace.eliminated_hypotheses
    )
    if not selectable_hypotheses:
        case.steps.append(
            InvestigationStep(
                actor="engine",
                action="resolution",
                detail=f"{resolution_trace.state.value}: {resolution_trace.rationale}",
            )
        )
        return Diagnosis(
            decision_semantics="m21.v3",
            incident_id=case.incident_id,
            root_cause=None,
            confidence=Confidence.UNVERIFIED,
            resolution=Resolution.INSUFFICIENT_EVIDENCE,
            summary=(
                "Observed hypotheses are either contradicted or supported only as propagated "
                "effects; no root-cause-eligible actor is established."
            ),
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
            alternative_hypotheses=tuple(case.hypotheses),
            **_requirement_provenance(case),
        )
    ranked_top_ineligible = not case.root_cause_eligibilities.is_root_cause_selectable(
        case.hypotheses[0].hypothesis_id
    )
    if resolution_trace.state is Resolution.RESOLVED:
        # The reported root cause is the resolved leader, whatever the rank order.
        leader_ids = resolution_trace.leading_hypothesis_ids
        if len(leader_ids) != 1:
            raise ValueError("RESOLVED resolution requires one leader")
        selected = next(
            hypothesis
            for hypothesis in selectable_hypotheses
            if hypothesis.hypothesis_id == leader_ids[0]
        )
    else:
        supported = [
            h
            for h in selectable_hypotheses
            if h.hypothesis_id in resolution_trace.plausible_hypotheses
        ]
        selected = supported[0] if len(supported) == 1 else selectable_hypotheses[0]
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
                if proposed not in selectable_hypotheses or (
                    resolution_trace.plausible_hypotheses
                    and proposed.hypothesis_id not in resolution_trace.plausible_hypotheses
                ):
                    case.steps.append(
                        InvestigationStep(
                            actor="engine",
                            action="kept",
                            detail=(
                                "rejected context or root-cause-ineligible hypothesis "
                                f"{proposed.hypothesis_id}"
                            ),
                        )
                    )
                else:
                    current_candidate = hypothesis_candidate(selected)
                    proposed_candidate = hypothesis_candidate(proposed)
                    accepted = _accept_override(case, current_candidate, proposed_candidate, config)
                    if accepted.entity == proposed.causal_actor:
                        selected = proposed
    selected_candidate = hypothesis_candidate(selected)
    selectable_index = selectable_hypotheses.index(selected)
    runner_up = (
        hypothesis_candidate(selectable_hypotheses[selectable_index + 1])
        if (ranked_top_ineligible or selected.hypothesis_id == case.hypotheses[0].hypothesis_id)
        and selectable_index + 1 < len(selectable_hypotheses)
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
    )
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
    # Roadmap C10: an unestablished leader whose evidence all lies outside the window is not a cause.
    windows = [finding_in_window(f.at, case.context, config.ranking) for f in selected.findings]
    withheld = (
        resolution_trace.claim_level == "UNESTABLISHED"
        and any(w is False for w in windows)
        and not any(w is True for w in windows)
    )
    return Diagnosis(
        decision_semantics="m21.v3",
        incident_id=case.incident_id,
        root_cause=selected.causal_actor,
        leading_actor_established=not withheld,
        leading_actor_withheld_reason="NO_EVIDENCE_IN_INCIDENT_WINDOW" if withheld else None,
        confidence=confidence,
        resolution=resolution_trace.state,
        summary=(
            f"Observed quota admission rejection by {selected.causal_actor.canonical} explains the declared incident scope. Incident recovery is not assessed."
            if resolution_trace.diagnosis_status == "MECHANISM_VERIFIED_CAUSE"
            else f"Supported possible initiating cause: {selected.causal_actor.canonical}. "
            "Mechanism execution and incident recovery are not established."
            if resolution_trace.diagnosis_status == "SUPPORTED_CAUSE"
            else f"No causal candidate has evidence in the incident window. Nearest observation, outside it, on {selected.causal_actor.canonical}: {selected.findings[0].summary if selected.findings else 'no actor observation'}. Causal investigation remains open: {resolution_trace.rationale}"
            if withheld
            else f"Observed on {selected.causal_actor.canonical}: {selected.findings[0].summary if selected.findings else 'no actor observation'}. Causal investigation remains open: {resolution_trace.rationale}"
        ),
        symptoms=case.symptoms,
        evidence=selected.findings[:5],
        causal_path=selected_candidate.causal_path,
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
        **_requirement_provenance(case),
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
    "RCA_ENGINE_VERSION",
    "Case",
    "Choice",
    "EngineConfig",
    "Investigator",
    "build_case",
    "diagnose",
    "diagnose_case",
]
