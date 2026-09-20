from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.investigation.actions import observation_identity
from packages.rca.investigation.candidates import ObservationCandidate
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import (
    DeterministicIntentPolicy,
    InvestigationIntentKind,
    InvestigationPhase,
    build_observation_bundles,
    classify_observation_candidate,
    derive_investigation_phase,
    select_observation_intent_candidate,
)
from packages.rca.model import (
    Alert,
    AuthorizedQuery,
    Diagnosis,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    GapDimension,
    GapResolvability,
    Hypothesis,
    InformationGap,
    InvestigationQuery,
    Resolution,
    ResolutionTrace,
)
from packages.rca.source import InMemorySource

ONSET = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind=kind, name=name)


def _case() -> Case:
    return build_case(
        InMemorySource(
            name="intents",
            alert_items=[Alert(name="latency", service="payment", starts_at=ONSET)],
            cutoff=ONSET + timedelta(minutes=30),
        )
    )


def _candidate(
    candidate_id: str,
    capability: str,
    dimension: GapDimension,
    *,
    hypothesis_ids: tuple[str, ...] = (),
) -> ObservationCandidate:
    target = _entity("Deployment", candidate_id)
    return ObservationCandidate(
        candidate_id=candidate_id,
        capability=capability,
        target=target,
        query=InvestigationQuery(
            start=ONSET - timedelta(minutes=30), end=ONSET + timedelta(minutes=30)
        ),
        gap_ids=(f"gap-{candidate_id}",),
        dimensions=(dimension,),
        hypothesis_ids=hypothesis_ids,
        alternative_ids=(),
    )


def _gap(
    candidate: ObservationCandidate,
    dimension: GapDimension,
    *,
    hypothesis_ids: tuple[str, ...] = (),
) -> InformationGap:
    return InformationGap(
        gap_id=candidate.gap_ids[0],
        dimension=dimension,
        hypothesis_ids=hypothesis_ids,
        missing_fact="missing",
        authorized_queries=(
            AuthorizedQuery(capability=candidate.capability, target=candidate.target),
        ),
        resolvability=GapResolvability.RESOLVABLE,
    )


def _diagnosis(case: Case, gaps: tuple[InformationGap, ...], **updates: object) -> Diagnosis:
    return diagnose_case(case).model_copy(update={"information_gaps": gaps, **updates})


def test_capability_mapping_is_explicit_and_history_is_contextual() -> None:
    cases = {
        "incident_events": InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY,
        "incident_changes": InvestigationIntentKind.RECENT_SOURCE_CHANGE,
        "logs": InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION,
        "runtime_traces": InvestigationIntentKind.RUNTIME_DISCRIMINATION,
        "events": InvestigationIntentKind.ACTOR_STATE_INSPECTION,
        "resource_pressure": InvestigationIntentKind.METRIC_STATE_INSPECTION,
        "traffic": InvestigationIntentKind.METRIC_STATE_INSPECTION,
    }
    for capability, intent in cases.items():
        dimension = (
            GapDimension.CONFIG_DIFFERENCE if capability == "history" else GapDimension.ENTITY_STATE
        )
        candidate = _candidate(capability, capability, dimension)
        if capability == "history":
            assert (
                classify_observation_candidate(candidate)
                is InvestigationIntentKind.ACTOR_STATE_INSPECTION
            )
        else:
            assert classify_observation_candidate(candidate) is intent
    assert (
        classify_observation_candidate(
            _candidate("history-change", "history", GapDimension.CHANGE_TIMING)
        )
        is InvestigationIntentKind.RECENT_SOURCE_CHANGE
    )


def test_source_discovery_excludes_structural_runtime_traces() -> None:
    case = _case()
    event = _candidate("event", "incident_events", GapDimension.EVENT_SEQUENCE)
    change = _candidate("change", "incident_changes", GapDimension.CHANGE_TIMING)
    trace = _candidate("trace", "runtime_traces", GapDimension.DEPENDENCY_HEALTH)
    diagnosis = _diagnosis(
        case, tuple(_gap(item, item.dimensions[0]) for item in (event, change, trace))
    )

    bundles = build_observation_bundles(
        case=case, diagnosis=diagnosis, candidates=(event, change, trace)
    )

    assert derive_investigation_phase(case, diagnosis) is InvestigationPhase.SOURCE_DISCOVERY
    assert all(
        bundle.intent is not InvestigationIntentKind.RUNTIME_DISCRIMINATION for bundle in bundles
    )
    assert {candidate_id for bundle in bundles for candidate_id in bundle.candidate_ids} == {
        "event",
        "change",
    }


def test_attempted_physical_reads_are_removed_without_killing_bundle() -> None:
    case = _case()
    first = _candidate("first", "logs", GapDimension.LOG_ERROR_PATTERN)
    second = _candidate("second", "logs", GapDimension.LOG_ERROR_PATTERN)
    diagnosis = _diagnosis(
        case, (_gap(first, first.dimensions[0]), _gap(second, second.dimensions[0]))
    )
    attempted = observation_identity(first.capability, first.target, first.query)
    bundles = build_observation_bundles(
        case=case,
        diagnosis=diagnosis,
        candidates=(first, second),
        attempted_observations=(attempted,),
    )
    assert bundles[0].candidate_ids == ("second",)


def test_direct_blocker_beats_larger_unrelated_bundle() -> None:
    case = _case()
    log = _candidate("log", "logs", GapDimension.LOG_ERROR_PATTERN)
    events = tuple(
        _candidate(f"event-{index}", "events", GapDimension.ENTITY_STATE) for index in range(20)
    )
    gaps = (_gap(log, log.dimensions[0]),) + tuple(
        _gap(event, event.dimensions[0]) for event in events
    )
    diagnosis = _diagnosis(case, gaps)
    selected = select_observation_intent_candidate(
        case=case,
        diagnosis=diagnosis,
        engine_config=EngineConfig(),
    )
    assert selected is not None
    assert (
        selected.scored_bundle.bundle.intent is InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION
    )
    assert selected.physical.candidate.capability == "logs"


def test_runtime_discrimination_requires_real_relevant_hypothesis() -> None:
    case = _case()
    actor = _entity("Deployment", "payment")
    finding = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=actor,
        at=ONSET,
        summary="changed",
        temporal_role=EvidenceTemporalRole.INITIATING,
    )
    hypothesis = Hypothesis(
        hypothesis_id="h-payment",
        causal_actor=actor,
        findings=(finding,),
        initiating_findings=(finding,),
    )
    case.hypotheses = [hypothesis]
    trace_candidate = _candidate(
        "trace", "runtime_traces", GapDimension.DEPENDENCY_HEALTH, hypothesis_ids=("h-payment",)
    )
    diagnosis = _diagnosis(
        case,
        (_gap(trace_candidate, trace_candidate.dimensions[0], hypothesis_ids=("h-payment",)),),
        hypothesis=hypothesis,
        resolution_trace=ResolutionTrace(
            state=Resolution.AMBIGUOUS,
            leading_hypothesis_ids=("h-payment",),
            unresolved_hypotheses=("h-payment",),
            unresolved_dimensions=("runtime_propagation",),
        ),
    )
    selected = select_observation_intent_candidate(
        case=case, diagnosis=diagnosis, engine_config=EngineConfig()
    )
    assert selected is not None
    assert (
        derive_investigation_phase(case, diagnosis) is InvestigationPhase.HYPOTHESIS_DISCRIMINATION
    )
    assert selected.scored_bundle.bundle.intent is InvestigationIntentKind.RUNTIME_DISCRIMINATION


def test_intent_policy_is_deterministic_and_uses_no_model() -> None:
    from packages.rca.demo import demo_source

    result = investigate_diagnosis(demo_source(), policy=DeterministicIntentPolicy())
    assert result.model_calls == 0
    assert result.tool_calls > 0
    assert len(result.ledger) == result.tool_calls
