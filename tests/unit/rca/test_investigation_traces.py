from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from packages.rca.engine import build_case
from packages.rca.information_gap import _capability_allows_target
from packages.rca.investigation.actions import observation_identity, validate_action
from packages.rca.investigation.environment import (
    SourceInvestigationBackend,
    _trace_matches_target,
    initial_view,
)
from packages.rca.investigation.evidence import InMemoryEvidenceStore
from packages.rca.investigation.graph import _check_novelty, _Runtime, build_investigation_state
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.investigation.tools import RuntimeTracesTool
from packages.rca.model import (
    AuthorizedQuery,
    EntityRef,
    GapDimension,
    GapResolvability,
    InformationGap,
    InvestigationAction,
    InvestigationQuery,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.source import InMemorySource

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _entity(kind: str, name: str, namespace: str = "shop") -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace=namespace)


def _span(
    span_id: str,
    *,
    service: str = "payment",
    start: int = 0,
    parent: str | None = None,
    namespace: str | None = "shop",
    deployment: str | None = "payment",
    pod: str | None = "payment-0",
    trace_id: str = "trace-1",
) -> TraceSpanObservation:
    attributes = {}
    if namespace is not None:
        attributes["k8s.namespace.name"] = namespace
    if deployment is not None:
        attributes["k8s.deployment.name"] = deployment
    if pod is not None:
        attributes["k8s.pod.name"] = pod
    return TraceSpanObservation(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        service=service,
        span_kind="SERVER" if parent else "CLIENT",
        start_at=T0 + timedelta(seconds=start),
        end_at=T0 + timedelta(seconds=start + 1),
        status=TraceSpanStatus.ERROR if parent else TraceSpanStatus.UNSET,
        semantic_attributes=attributes,
        evidence_id=f"trace:{trace_id}:{span_id}",
    )


def _gap(target: EntityRef) -> InformationGap:
    return InformationGap(
        gap_id="gap:traces",
        dimension=GapDimension.DEPENDENCY_HEALTH,
        missing_fact="runtime call evidence",
        authorized_queries=(AuthorizedQuery(capability="runtime_traces", target=target),),
        candidate_tools=("runtime_traces",),
        entity_scope=(target,),
        resolvability=GapResolvability.RESOLVABLE,
    )


def test_exact_kubernetes_target_matching_and_no_service_fallback() -> None:
    deployment = _entity("Deployment", "payment")
    span = _span("seed")
    assert _trace_matches_target(span, deployment)
    assert not _trace_matches_target(span, _entity("Deployment", "pay"))
    assert not _trace_matches_target(span, _entity("Deployment", "payment", "other"))
    assert not _trace_matches_target(
        span.model_copy(update={"semantic_attributes": {"k8s.namespace.name": "shop"}}),
        deployment,
    )
    service_only = span.model_copy(
        update={
            "semantic_attributes": {"k8s.namespace.name": "shop"},
            "service": "payment",
        }
    )
    assert not _trace_matches_target(service_only, deployment)


def test_legacy_source_trace_capability_is_independent_of_current_evidence() -> None:
    source = InMemorySource(name="source-only-no-traces")
    backend = SourceInvestigationBackend(source)
    target = _entity("Pod", "payment-0")
    query = InvestigationQuery(start=T0 - timedelta(minutes=1), end=T0 + timedelta(minutes=1))

    assert backend.supports("runtime_traces")
    assert backend.query_traces(target, query) == ()

    source.trace_items.append(_span("seed"))
    assert backend.supports("runtime_traces")
    assert backend.query_traces(target, query)


@pytest.mark.parametrize(
    "kind", ("Service", "ConfigMap", "NetworkPolicy", "HorizontalPodAutoscaler")
)
def test_runtime_traces_rejects_unsupported_target_kinds(kind: str) -> None:
    assert not _capability_allows_target("runtime_traces", _entity(kind, "payment"))


def test_runtime_trace_backend_returns_windowed_one_hop_context_only() -> None:
    grandparent = _span(
        "grandparent", start=-3, parent=None, service="frontend", deployment=None, pod=None
    )
    parent = _span(
        "parent",
        start=-2,
        parent="grandparent",
        service="frontend",
        deployment=None,
        pod=None,
    )
    seed = _span("seed", start=0, parent="parent")
    child = _span("child", start=1, parent="seed", service="orders", deployment=None, pod=None)
    grandchild = _span(
        "grandchild", start=2, parent="child", service="orders", deployment=None, pod=None
    )
    unrelated = _span(
        "unrelated", start=0, service="unrelated", trace_id="trace-1", deployment=None, pod=None
    )
    before = _span("before", start=-10)
    after = _span("after", start=10)
    source = InMemorySource(
        name="trace-window",
        trace_items=[after, unrelated, child, before, grandchild, seed, parent, grandparent],
        cutoff=T0 + timedelta(seconds=5),
    )
    result = SourceInvestigationBackend(source).query_traces(
        _entity("Deployment", "payment"),
        InvestigationQuery(start=T0 - timedelta(seconds=2), end=T0 + timedelta(seconds=3)),
    )
    assert tuple(item.span_id for item in result) == ("parent", "seed", "child")
    assert "grandparent" not in {item.span_id for item in result}
    assert "grandchild" not in {item.span_id for item in result}
    assert "unrelated" not in {item.span_id for item in result}
    assert "before" not in {item.span_id for item in result}
    assert "after" not in {item.span_id for item in result}


def test_runtime_trace_backend_deduplicates_and_applies_limit() -> None:
    seed = _span("seed")
    child = _span("child", start=1, parent="seed", service="orders")
    source = InMemorySource(name="trace-dedup", trace_items=[child, seed])
    result = SourceInvestigationBackend(source).query_traces(
        _entity("Deployment", "payment"),
        InvestigationQuery(start=T0 - timedelta(seconds=1), end=T0 + timedelta(seconds=2), limit=1),
    )
    assert len(result) == 1
    assert result[0].span_id == "seed"


def test_runtime_trace_action_validation_rejects_filters_and_large_limits() -> None:
    target = _entity("Pod", "payment-0")
    gap = _gap(target)
    tool = RuntimeTracesTool()
    filtered = InvestigationAction(
        action="inspect",
        gap_id=gap.gap_id,
        capability="runtime_traces",
        target=target,
        query=InvestigationQuery(reasons=("Error",)),
    )
    oversized = filtered.model_copy(update={"query": InvestigationQuery(limit=33)})
    for action in (filtered, oversized):
        result = validate_action(
            action,
            gaps=(gap,),
            tools={"runtime_traces": tool},
            attempted_actions=(),
            attempted_observations=(),
            tool_calls=0,
            config=InvestigationConfig(),
        )
        assert not result.valid
    assert observation_identity(
        "runtime_traces", target, InvestigationQuery()
    ) == observation_identity(
        "runtime_traces", target, InvestigationQuery(reasons=("ignored",), contains=("ignored",))
    )


def test_runtime_trace_tool_payload_and_no_data() -> None:
    target = _entity("Deployment", "payment")
    span = _span("seed")
    source = InMemorySource(name="tool", trace_items=[span])
    case = build_case(initial_view(source))
    gap = _gap(target)
    tool = RuntimeTracesTool(SourceInvestigationBackend(source))
    observation = tool.execute_query(
        case,
        gap,
        target,
        InvestigationQuery(start=T0 - timedelta(seconds=1), end=T0 + timedelta(seconds=1)),
    )
    assert tuple(observation.evidence_refs) == (span.evidence_id,)
    assert tuple(item["evidence_id"] for item in observation.payload["traces"]) == (
        span.evidence_id,
    )
    missing = tool.execute_query(case, gap, _entity("Deployment", "missing"), InvestigationQuery())
    assert missing.payload["traces"] == []
    assert missing.evidence_refs == ()
    assert missing.outcome.value == "NO_DATA"


def test_trace_observation_enters_a1_store_and_rebuilds_runtime_products() -> None:
    target = _entity("Deployment", "payment")
    parent = _span(
        "parent",
        service="frontend",
        deployment="frontend",
        pod="frontend-0",
    ).model_copy(update={"status": TraceSpanStatus.ERROR})
    seed = _span("seed", parent="parent")
    source = InMemorySource(name="graph-ingestion", trace_items=[parent, seed])
    base = initial_view(source)
    case = build_case(base)
    gap = _gap(target)
    observation = RuntimeTracesTool(SourceInvestigationBackend(source)).execute_query(
        case,
        gap,
        target,
        InvestigationQuery(start=T0 - timedelta(seconds=1), end=T0 + timedelta(seconds=2)),
    )
    state = build_investigation_state(base, initial_case=case)
    state["pending_observation"] = observation
    runtime = _Runtime(
        source=base,
        policy=ScriptedInvestigationPolicy([]),
        tools={},
        config=InvestigationConfig(),
        initial_case=case,
        rebuild_case=None,
        backend=SourceInvestigationBackend(source),
        evidence_store=InMemoryEvidenceStore(),
    )
    acquired = _check_novelty(state, runtime)
    assert acquired["pending_new_evidence_refs"] == (parent.evidence_id, seed.evidence_id)
    acquired_state = {**state, **acquired}
    rebuilt = runtime.case_for(acquired_state["acquired_evidence_refs"], ())
    assert rebuilt.runtime_graph.edges
    assert rebuilt.runtime_evidence.service_outcomes
    assert rebuilt.runtime_propagation.edges
    assert acquired_state["investigation_findings"] == ()
