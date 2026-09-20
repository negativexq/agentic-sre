from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.investigation.actions import observation_identity
from packages.rca.investigation.candidates import (
    build_observation_candidates,
    resolve_effective_query,
)
from packages.rca.investigation.environment import SourceInvestigationBackend
from packages.rca.investigation.tools import (
    LogsTool,
    ResourcePressureTool,
    RuntimeTracesTool,
    TrafficTool,
)
from packages.rca.model import (
    Alert,
    Diagnosis,
    EntityRef,
    GapDimension,
    GapResolvability,
    InformationGap,
    LogRecord,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)
from packages.rca.source import InMemorySource

ONSET = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind=kind, name=name)


def _case(
    *,
    cutoff: datetime | None = None,
    with_onset: bool = True,
    onset: datetime = ONSET,
) -> Case:
    alerts = [Alert(name="latency", service="payment", starts_at=onset)] if with_onset else []
    return build_case(InMemorySource(name="candidate-case", alert_items=alerts, cutoff=cutoff))


def _gap(
    gap_id: str,
    dimension: GapDimension,
    capability: str,
    target: EntityRef,
    *,
    hypothesis_ids: tuple[str, ...] = (),
    alternative_ids: tuple[str, ...] = (),
) -> InformationGap:
    from packages.rca.model import AuthorizedQuery

    return InformationGap(
        gap_id=gap_id,
        dimension=dimension,
        missing_fact=f"missing {gap_id}",
        hypothesis_ids=hypothesis_ids,
        alternative_ids=alternative_ids,
        authorized_queries=(
            AuthorizedQuery(
                capability=capability,
                target=target,
                alternative_ids=alternative_ids,
            ),
        ),
        resolvability=GapResolvability.RESOLVABLE,
    )


def _diagnosis(case: Case, gaps: tuple[InformationGap, ...]) -> Diagnosis:
    return diagnose_case(case).model_copy(update={"information_gaps": gaps})


def test_effective_query_templates_and_cutoff() -> None:
    cutoff = ONSET + timedelta(minutes=10)
    case = _case(cutoff=cutoff)
    config = EngineConfig()

    history = resolve_effective_query(capability="history", case=case, engine_config=config)
    events = resolve_effective_query(capability="events", case=case, engine_config=config)
    assert history is not None and events is not None
    assert history.start == ONSET - timedelta(hours=2)
    assert history.end == cutoff
    assert events == history

    for capability in ("logs", "runtime_traces", "resource_pressure", "traffic"):
        query = resolve_effective_query(capability=capability, case=case, engine_config=config)
        assert query is not None
        assert query.start == ONSET - timedelta(minutes=30)
        assert query.end == cutoff
        assert query.limit == 32
        if capability != "logs":
            assert query.end - query.start <= timedelta(hours=1)


def test_no_onset_uses_visible_window_metadata_without_wall_clock() -> None:
    anchor = ONSET + timedelta(hours=3)
    case = _case(cutoff=anchor, with_onset=False)
    config = EngineConfig()
    query = resolve_effective_query(capability="traffic", case=case, engine_config=config)
    assert query is not None
    assert query.start == anchor - timedelta(minutes=30)
    assert query.end == anchor

    no_anchor = _case(cutoff=None, with_onset=False)
    assert (
        resolve_effective_query(capability="history", case=no_anchor, engine_config=config) is None
    )


def test_history_candidate_uses_full_ranking_causal_window() -> None:
    case = _case(cutoff=ONSET + timedelta(hours=1))
    query = resolve_effective_query(capability="history", case=case, engine_config=EngineConfig())
    assert query is not None
    assert query.start is not None and query.end is not None
    old_change = ONSET - timedelta(minutes=75)
    assert query.start <= old_change <= query.end
    assert old_change < ONSET - timedelta(minutes=30)


def test_event_candidate_uses_full_ranking_causal_window() -> None:
    case = _case(cutoff=ONSET + timedelta(hours=1))
    query = resolve_effective_query(capability="events", case=case, engine_config=EngineConfig())
    assert query is not None
    assert query.start is not None and query.end is not None
    old_event = ONSET - timedelta(minutes=60)
    assert query.start <= old_event <= query.end


def test_resolvable_logical_gaps_coalesce_with_union_provenance() -> None:
    case = _case(cutoff=ONSET + timedelta(minutes=30))
    target = _entity("Deployment", "payment")
    gaps = (
        _gap(
            "gap-a",
            GapDimension.CHANGE_TIMING,
            "history",
            target,
            hypothesis_ids=("h-a",),
            alternative_ids=("alt-a",),
        ),
        _gap(
            "gap-b",
            GapDimension.CONFIG_DIFFERENCE,
            "history",
            target,
            hypothesis_ids=("h-b",),
            alternative_ids=("alt-b",),
        ),
    )
    candidates = build_observation_candidates(
        case=case,
        diagnosis=_diagnosis(case, gaps),
        engine_config=EngineConfig(),
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.query is not None
    assert candidate.gap_ids == ("gap-a", "gap-b")
    assert candidate.dimensions == (
        GapDimension.CHANGE_TIMING,
        GapDimension.CONFIG_DIFFERENCE,
    )
    assert candidate.hypothesis_ids == ("h-a", "h-b")
    assert candidate.alternative_ids == ("alt-a", "alt-b")


def test_candidate_identity_is_gap_independent_and_query_sensitive() -> None:
    case = _case(cutoff=ONSET + timedelta(minutes=30))
    target = _entity("Deployment", "payment")
    first_gap = _gap("first", GapDimension.CHANGE_TIMING, "history", target)
    second_gap = _gap("second", GapDimension.CONFIG_DIFFERENCE, "history", target)
    first = build_observation_candidates(
        case=case,
        diagnosis=_diagnosis(case, (first_gap,)),
        engine_config=EngineConfig(),
    )[0]
    second = build_observation_candidates(
        case=case,
        diagnosis=_diagnosis(case, (second_gap,)),
        engine_config=EngineConfig(),
    )[0]
    assert first.candidate_id == second.candidate_id
    assert first.candidate_id == (
        "candidate:"
        + sha256(observation_identity("history", target, first.query).encode()).hexdigest()[:20]
    )
    changed_case = _case(
        cutoff=ONSET + timedelta(hours=1),
        onset=ONSET + timedelta(minutes=5),
    )
    changed = build_observation_candidates(
        case=changed_case,
        diagnosis=_diagnosis(changed_case, (first_gap,)),
        engine_config=EngineConfig(),
    )[0]
    assert changed.candidate_id != first.candidate_id


def test_defensive_non_temporal_capabilities_produce_no_candidate() -> None:
    case = _case(cutoff=ONSET + timedelta(minutes=30))
    target = _entity("Deployment", "payment")
    gap = _gap("describe", GapDimension.ENTITY_STATE, "describe", target)
    assert (
        build_observation_candidates(
            case=case,
            diagnosis=_diagnosis(case, (gap,)),
            engine_config=EngineConfig(),
        )
        == ()
    )


def test_explicit_provider_windows_match_legacy_default_execution() -> None:
    pod = _entity("Pod", "payment-0")
    service = _entity("Service", "payment")
    source = InMemorySource(
        name="execution-equivalence",
        alert_items=[Alert(name="latency", service="payment", starts_at=ONSET)],
        cutoff=ONSET + timedelta(minutes=30),
        error_items=[
            LogRecord(
                service="payment",
                at=ONSET,
                severity="ERROR",
                message="timeout",
                evidence_id="log:one",
            )
        ],
        pressure_items=[
            ResourcePressure(
                pod=pod,
                container="app",
                resource="memory",
                baseline=0.2,
                peak=0.9,
                at=ONSET,
                evidence_id="pressure:one",
            )
        ],
        traffic_items=[
            TrafficObservation(
                entity=service,
                metric="http_requests_per_second",
                at=ONSET,
                value=10.0,
                evidence_id="traffic:one",
            )
        ],
        trace_items=[
            TraceSpanObservation(
                trace_id="trace-one",
                span_id="span-one",
                service="payment",
                start_at=ONSET,
                semantic_attributes={
                    "k8s.namespace.name": "shop",
                    "k8s.pod.name": "payment-0",
                },
                evidence_id="trace:one",
            )
        ],
    )
    case = build_case(source)
    backend = SourceInvestigationBackend(source)
    tools = {
        "logs": (LogsTool(backend), service),
        "runtime_traces": (RuntimeTracesTool(backend), pod),
        "resource_pressure": (ResourcePressureTool(backend), pod),
        "traffic": (TrafficTool(backend), service),
    }
    for capability, (tool, target) in tools.items():
        gap = _gap(f"{capability}-gap", GapDimension.DEPENDENCY_HEALTH, capability, target)
        explicit = resolve_effective_query(
            capability=capability, case=case, engine_config=EngineConfig()
        )
        assert explicit is not None
        implicit_observation = tool.execute_query(case, gap, target, None)
        explicit_observation = tool.execute_query(case, gap, target, explicit)
        assert explicit_observation.evidence_refs == implicit_observation.evidence_refs
