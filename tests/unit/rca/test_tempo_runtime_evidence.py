from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from packages.rca.engine import Case, build_case
from packages.rca.investigation.environment import SourceInvestigationBackend, initial_view
from packages.rca.investigation.evidence import InMemoryEvidenceStore
from packages.rca.investigation.graph import (
    _check_novelty,
    _normalize,
    _rebuild,
    _Runtime,
    build_investigation_state,
)
from packages.rca.investigation.normalizers import normalize_observation
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.investigation.state import InvestigationConfig, InvestigationState
from packages.rca.investigation.tools import RuntimeTracesTool
from packages.rca.model import (
    AuthorizedQuery,
    EntityRef,
    FindingKind,
    GapDimension,
    GapResolvability,
    InformationGap,
    InvestigationObservation,
    InvestigationQuery,
    RuntimeEvidencePillar,
    RuntimeObservationState,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.runtime_evidence import derive_runtime_trace_call_facts
from packages.rca.source import InMemorySource

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class _TempoBackend(SourceInvestigationBackend):
    def __init__(self, source: InMemorySource, spans: tuple[TraceSpanObservation, ...]) -> None:
        super().__init__(source)
        self.tempo = object()
        self.spans = spans

    def query_traces(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...]:
        del target, query
        return self.spans


def _span(
    span_id: str,
    *,
    service: str,
    parent: str | None = None,
    status: TraceSpanStatus = TraceSpanStatus.OK,
    start: int = 0,
    deployment: str | None = None,
) -> TraceSpanObservation:
    attributes = {"http.response.status_code": "503"} if status is TraceSpanStatus.ERROR else {}
    attributes["k8s.namespace.name"] = "shop"
    if deployment is not None:
        attributes["k8s.deployment.name"] = deployment
        attributes["k8s.pod.name"] = f"{deployment}-0"
    return TraceSpanObservation(
        trace_id="trace-001",
        span_id=span_id,
        parent_span_id=parent,
        service=service,
        span_kind="SERVER" if parent is not None else "CLIENT",
        start_at=T0 + timedelta(seconds=start),
        end_at=T0 + timedelta(seconds=start + 2),
        status=status,
        semantic_attributes=attributes,
        evidence_id=f"tempo:trace-001:{span_id}",
    )


def _gap(target: EntityRef) -> InformationGap:
    return InformationGap(
        gap_id="gap:tempo",
        dimension=GapDimension.DEPENDENCY_HEALTH,
        missing_fact="bounded runtime call outcome",
        authorized_queries=(AuthorizedQuery(capability="runtime_traces", target=target),),
        candidate_tools=("runtime_traces",),
        entity_scope=(target,),
        resolvability=GapResolvability.RESOLVABLE,
    )


def _read(
    spans: tuple[TraceSpanObservation, ...],
) -> tuple[Case, InformationGap, InvestigationObservation]:
    target = EntityRef(kind="Deployment", name="payment", namespace="shop")
    source = InMemorySource(name="tempo-runtime", cutoff=T0 + timedelta(seconds=60))
    case = build_case(initial_view(source))
    gap = _gap(target)
    backend = _TempoBackend(source, spans)
    observation = RuntimeTracesTool(backend).execute_query(
        case,
        gap,
        target,
        InvestigationQuery(
            start=T0 - timedelta(seconds=10), end=T0 + timedelta(seconds=10), limit=12
        ),
    )
    return case, gap, observation


def test_tempo_observation_has_bounded_typed_direction_and_error_finding() -> None:
    caller = _span("caller", service="frontend", start=0)
    callee = _span(
        "callee",
        service="payment",
        parent="caller",
        status=TraceSpanStatus.ERROR,
        start=1,
        deployment="payment",
    )
    case, gap, observation = _read((caller, callee))

    assert observation.runtime is not None
    assert observation.runtime.pillar is RuntimeEvidencePillar.TEMPO
    assert observation.runtime.state is RuntimeObservationState.OBSERVED_ABNORMAL
    assert observation.runtime.query.template_id == "tempo.target_traceql.v1"
    fact = observation.payload["trace_call_facts"][0]
    assert fact["caller_service"] == "frontend"
    assert fact["callee_service"] == "payment"
    assert fact["direction_basis"] == "CLIENT_SERVER_SPANS"
    assert fact["callee_outcome"] == "ERROR"
    assert fact["callee_duration_seconds"] == 2.0

    normalized = normalize_observation(observation, case=case, gap=gap)
    assert len(normalized.findings) == 1
    finding = normalized.findings[0]
    assert finding.kind is FindingKind.DEPENDENCY_ERRORS
    assert finding.entity == EntityRef(kind="Pod", name="payment-0", namespace="shop")
    assert finding.related == (EntityRef(kind="Service", name="frontend", namespace="shop"),)
    assert finding.details["runtime_pillar"] == "TEMPO"
    assert finding.details["runtime_normalization_rule_id"] == "tempo.trace_observation.v1"
    assert finding.details["runtime_source_observation_ids"] == observation.evidence_refs
    assert "root_cause" not in finding.details
    assert "elimination" not in finding.details
    assert normalized.observation.outcome.value == "UNKNOWN"


def test_tempo_explicit_success_is_observed_normal_without_finding() -> None:
    caller = _span("caller", service="frontend", status=TraceSpanStatus.OK)
    callee = _span("callee", service="payment", parent="caller", status=TraceSpanStatus.OK)
    case, gap, observation = _read((caller, callee))

    assert observation.runtime is not None
    assert observation.runtime.state is RuntimeObservationState.OBSERVED_NORMAL
    normalized = normalize_observation(observation, case=case, gap=gap)
    assert normalized.findings == ()
    assert normalized.observation.outcome.value == "UNKNOWN"


def test_tempo_no_data_is_distinct_from_observed_normal() -> None:
    case, gap, observation = _read(())

    assert observation.outcome.value == "NO_DATA"
    assert observation.runtime is not None
    assert observation.runtime.state is RuntimeObservationState.NO_DATA
    assert observation.evidence_refs == ()
    assert normalize_observation(observation, case=case, gap=gap).findings == ()


def test_runtime_trace_call_facts_require_explicit_parent_child_direction() -> None:
    root = _span("root", service="frontend")
    child = _span("child", service="payment", parent="root", start=1)
    same_service = _span("same", service="frontend", parent="root", start=2)

    facts = derive_runtime_trace_call_facts((root, child, same_service))

    assert len(facts) == 1
    assert facts[0].caller_service == "frontend"
    assert facts[0].callee_service == "payment"
    assert facts[0].direction_basis == "CLIENT_SERVER_SPANS"
    assert facts[0].evidence_ids == (root.evidence_id, child.evidence_id)


def test_tempo_finding_enters_existing_investigation_rebuild() -> None:
    spans = (
        _span("caller", service="frontend", start=0),
        _span(
            "callee",
            service="payment",
            parent="caller",
            status=TraceSpanStatus.ERROR,
            start=1,
            deployment="payment",
        ),
    )
    target = EntityRef(kind="Deployment", name="payment", namespace="shop")
    source = InMemorySource(name="tempo-rebuild", cutoff=T0 + timedelta(seconds=60))
    base = initial_view(source)
    case = build_case(base)
    gap = _gap(target)
    backend = _TempoBackend(source, spans)
    observation = RuntimeTracesTool(backend).execute_query(
        case,
        gap,
        target,
        InvestigationQuery(
            start=T0 - timedelta(seconds=10), end=T0 + timedelta(seconds=10), limit=12
        ),
    )
    state = build_investigation_state(base, initial_case=case)
    state["current_diagnosis"] = state["current_diagnosis"].model_copy(
        update={"information_gaps": (gap,)}
    )
    state["pending_observation"] = observation
    runtime = _Runtime(
        source=base,
        policy=ScriptedInvestigationPolicy([]),
        tools=None,
        config=InvestigationConfig(),
        initial_case=case,
        rebuild_case=None,
        backend=backend,
        evidence_store=InMemoryEvidenceStore(),
    )

    acquired = _check_novelty(state, runtime)
    after_acquisition = cast(InvestigationState, {**state, **acquired})
    normalized = _normalize(after_acquisition, runtime)
    after_normalization = cast(InvestigationState, {**after_acquisition, **normalized})
    rebuilt = _rebuild(after_normalization, runtime)
    rebuilt_diagnosis = rebuilt["current_diagnosis"]
    runtime_case = runtime.case_for(
        after_normalization["acquired_evidence_refs"],
        rebuilt["investigation_findings"],
    )

    assert len(normalized["pending_findings"]) == 1
    assert normalized["pending_findings"][0].kind is FindingKind.DEPENDENCY_ERRORS
    assert any(
        finding.kind is FindingKind.DEPENDENCY_ERRORS
        and finding.details.get("runtime_pillar") == "TEMPO"
        for finding in rebuilt_diagnosis.evidence
    )
    assert runtime_case.runtime_graph.edges
    assert runtime_case.runtime_propagation.boundaries


def test_runtime_traces_without_tempo_adapter_keep_source_only_normalization() -> None:
    spans = (
        _span("caller", service="frontend", start=0),
        _span(
            "callee",
            service="payment",
            parent="caller",
            status=TraceSpanStatus.ERROR,
            start=1,
            deployment="payment",
        ),
    )
    target = EntityRef(kind="Deployment", name="payment", namespace="shop")
    source = InMemorySource(name="tempo-source-only", trace_items=list(spans))
    case = build_case(initial_view(source))
    gap = _gap(target)
    observation = RuntimeTracesTool(SourceInvestigationBackend(source)).execute_query(
        case,
        gap,
        target,
        InvestigationQuery(
            start=T0 - timedelta(seconds=10), end=T0 + timedelta(seconds=10), limit=12
        ),
    )

    assert observation.runtime is None
    assert normalize_observation(observation, case=case, gap=gap).findings == ()
