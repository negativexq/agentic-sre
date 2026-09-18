"""P0 authorization, observation identity, and frontier lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from packages.rca.demo import demo_source
from packages.rca.engine import build_case
from packages.rca.frontier import apply_frontier_progress, covered_frontier_dimensions
from packages.rca.information_gap import (
    _capability_allows_target,
    _gap_for,
)
from packages.rca.investigation.actions import observation_identity, validate_action
from packages.rca.investigation.graph import _frozen, build_investigation_state
from packages.rca.investigation.normalizers import normalize_observation
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.model import (
    AuthorizedQuery,
    Confidence,
    Diagnosis,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    FrontierStatus,
    GapDimension,
    GapOutcomeKind,
    GapResolvability,
    Hypothesis,
    InformationGap,
    InvestigationAction,
    InvestigationObservation,
    InvestigationQuery,
    Resolution,
    StructuralAlternative,
    Symptoms,
)
from packages.rca.source import InMemorySource


def _ref(kind: str, name: str) -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace="shop")


def _alternative(
    alternative_id: str,
    actor: EntityRef,
    dimensions: tuple[GapDimension, ...],
    targets: tuple[EntityRef, ...] | None = None,
) -> StructuralAlternative:
    return StructuralAlternative(
        alternative_id=alternative_id,
        actor=actor,
        role="test",
        queryable_dimensions=dimensions,
        observation_targets=targets or (actor,),
    )


def _gap(
    gap_id: str,
    queries: tuple[AuthorizedQuery, ...],
    *,
    dimension: GapDimension = GapDimension.CONFIG_DIFFERENCE,
) -> InformationGap:
    return InformationGap(
        gap_id=gap_id,
        dimension=dimension,
        missing_fact="test fact",
        authorized_queries=queries,
        candidate_tools=tuple(sorted({item.capability for item in queries})),
        entity_scope=tuple(
            sorted({item.target for item in queries}, key=lambda item: item.canonical)
        ),
        resolvability=GapResolvability.RESOLVABLE,
    )


def _tool(name: str) -> Any:
    return type("Tool", (), {"name": name})()


def test_gap_authorization_is_dimension_specific_and_protected_kinds_fail_closed() -> None:
    config = _alternative(
        "config",
        _ref("ConfigMap", "checkout-config"),
        (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
        (_ref("ConfigMap", "checkout-config"), _ref("Deployment", "checkout")),
    )
    dependency = _alternative(
        "dependency",
        _ref("Service", "payment"),
        (GapDimension.DEPENDENCY_HEALTH, GapDimension.LOG_ERROR_PATTERN),
    )
    hpa = _alternative(
        "hpa",
        _ref("HorizontalPodAutoscaler", "checkout"),
        (GapDimension.AUTOSCALING_TARGET_STATE, GapDimension.EVENT_SEQUENCE),
    )
    secret = _alternative(
        "secret",
        _ref("Secret", "checkout-secret"),
        (GapDimension.CONFIG_DIFFERENCE,),
    )
    role = _alternative(
        "role",
        _ref("Role", "checkout-reader"),
        (GapDimension.CONFIG_DIFFERENCE,),
    )
    source = InMemorySource(name="authorization")
    config_gap = _gap_for(
        GapDimension.CONFIG_DIFFERENCE,
        (),
        (config, dependency, hpa, secret, role),
        source,
    )
    structural_ids = {
        item.alternative_ids[0] for item in config_gap.authorized_queries if item.alternative_ids
    }
    assert structural_ids == {"config"}
    assert all(item.target.kind != "Secret" for item in config_gap.authorized_queries)
    assert all(
        item.target.kind not in {"Role", "RoleBinding"} for item in config_gap.authorized_queries
    )
    assert all(
        item.capability == "history" or item.capability == "describe"
        for item in config_gap.authorized_queries
    )

    dependency_gap = _gap_for(GapDimension.DEPENDENCY_HEALTH, (), (dependency,), source)
    assert (
        AuthorizedQuery(
            capability="logs", target=_ref("Service", "payment"), alternative_ids=("dependency",)
        )
        in dependency_gap.authorized_queries
    )
    assert _capability_allows_target("logs", _ref("ConfigMap", "checkout-config")) is False
    assert _capability_allows_target("resource_pressure", _ref("Deployment", "checkout")) is False
    assert _capability_allows_target("resource_pressure", _ref("Pod", "checkout-0")) is True
    assert _capability_allows_target("history", _ref("Secret", "checkout-secret")) is False
    assert _capability_allows_target("events", _ref("Role", "checkout-reader")) is False


def test_hypothesis_scope_authorizes_without_structural_alternative_id() -> None:
    actor = _ref("Service", "payment")
    hypothesis = Hypothesis(hypothesis_id="h1", causal_actor=actor, members=(actor,))
    gap = _gap_for(
        GapDimension.DEPENDENCY_HEALTH,
        (hypothesis,),
        (),
        InMemorySource(name="hypothesis-scope"),
    )
    assert any(
        item.capability == "logs" and item.target == actor and item.alternative_ids == ()
        for item in gap.authorized_queries
    )


def test_validation_requires_exact_authorized_capability_target_pair() -> None:
    target = _ref("ConfigMap", "checkout-config")
    gap = _gap(
        "gap:config",
        (AuthorizedQuery(capability="history", target=target),),
    )
    valid = validate_action(
        InvestigationAction(
            action="inspect", gap_id=gap.gap_id, capability="history", target=target
        ),
        gaps=(gap,),
        tools={"history": _tool("history")},
        attempted_actions=(),
        tool_calls=0,
        config=InvestigationConfig(),
    )
    invalid = validate_action(
        InvestigationAction(action="inspect", gap_id=gap.gap_id, capability="logs", target=target),
        gaps=(gap,),
        tools={"logs": _tool("logs")},
        attempted_actions=(),
        tool_calls=0,
        config=InvestigationConfig(),
    )
    assert valid.valid
    assert not invalid.valid
    assert invalid.reason == "capability/target pair is not authorized for this gap"


def test_observation_identity_is_gap_independent_and_canonicalizes_unordered_filters() -> None:
    target = _ref("Pod", "checkout-0")
    left = InvestigationQuery(
        reasons=("Warning", "BackOff", "Warning"), contains=("timeout", "error")
    )
    right = InvestigationQuery(reasons=("BackOff", "Warning"), contains=("error", "timeout"))
    assert observation_identity("events", target, left) == observation_identity(
        "events", target, right
    )

    gap_a = _gap(
        "gap:a",
        (AuthorizedQuery(capability="events", target=target),),
    )
    gap_b = _gap(
        "gap:b",
        (AuthorizedQuery(capability="events", target=target),),
    )
    action_a = InvestigationAction(
        action="inspect", gap_id="gap:a", capability="events", target=target, query=left
    )
    action_b = action_a.model_copy(update={"gap_id": "gap:b", "query": right})
    first = validate_action(
        action_a,
        gaps=(gap_a, gap_b),
        tools={"events": _tool("events")},
        attempted_actions=(),
        tool_calls=0,
        config=InvestigationConfig(),
    )
    second = validate_action(
        action_b,
        gaps=(gap_a, gap_b),
        tools={"events": _tool("events")},
        attempted_actions=("gap:a|events|shop/Pod/checkout-0|",),
        attempted_observations=(observation_identity("events", target, left),),
        tool_calls=1,
        config=InvestigationConfig(),
    )
    assert first.valid
    assert not second.valid
    assert second.reason == "the same telemetry observation was already attempted"


def test_one_read_covers_multiple_frontier_dimensions() -> None:
    target = _ref("ConfigMap", "checkout-config")
    alternative_id = "alternative:config"
    queries = (
        AuthorizedQuery(capability="history", target=target, alternative_ids=(alternative_id,)),
    )
    diagnosis = Diagnosis(
        incident_id="coverage",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        resolution=Resolution.AMBIGUOUS,
        summary="ambiguous",
        symptoms=Symptoms(
            onset=None,
            last_seen=None,
            services=(),
            namespaces=(),
            alert_names=(),
        ),
        information_gaps=(
            _gap("gap:config", queries, dimension=GapDimension.CONFIG_DIFFERENCE),
            _gap("gap:timing", queries, dimension=GapDimension.CHANGE_TIMING),
        ),
    )
    covered = covered_frontier_dimensions(diagnosis, capability="history", target=target)
    assert covered == {alternative_id: (GapDimension.CHANGE_TIMING, GapDimension.CONFIG_DIFFERENCE)}


def test_frontier_progress_is_partial_until_fully_covered_and_promotes_only_hypotheses() -> None:
    actor = _ref("Deployment", "checkout")
    alternative = _alternative(
        "alternative:checkout",
        actor,
        (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    )
    partial = apply_frontier_progress(
        (alternative,),
        hypotheses=(),
        queried_dimensions_by_alternative={
            alternative.alternative_id: (GapDimension.CONFIG_DIFFERENCE,)
        },
    )[0]
    assert partial.status is FrontierStatus.UNEXPLORED
    assert partial.queried_dimensions == (GapDimension.CONFIG_DIFFERENCE,)

    exhausted = apply_frontier_progress(
        (alternative,),
        hypotheses=(),
        queried_dimensions_by_alternative={
            alternative.alternative_id: (
                GapDimension.CONFIG_DIFFERENCE,
                GapDimension.CHANGE_TIMING,
            )
        },
    )[0]
    assert exhausted.status is FrontierStatus.QUERIED_NO_CAUSAL_FINDING

    promoted = apply_frontier_progress(
        (alternative,),
        hypotheses=(Hypothesis(hypothesis_id="h1", causal_actor=actor),),
        queried_dimensions_by_alternative={},
    )[0]
    assert promoted.status is FrontierStatus.PROMOTED

    manifestation = apply_frontier_progress(
        (alternative,),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h2",
                causal_actor=_ref("Service", "checkout"),
                members=(actor,),
            ),
        ),
        queried_dimensions_by_alternative={},
    )[0]
    assert manifestation.status is FrontierStatus.PROMOTED


def test_normalized_findings_keep_signal_provenance_separate_from_observation_refs() -> None:
    case = build_case(demo_source())
    first = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=_ref("ConfigMap", "checkout-config"),
        at=datetime(2026, 1, 1, tzinfo=UTC),
        temporal_role=EvidenceTemporalRole.INITIATING,
        summary="first change",
        evidence_ids=("raw:first",),
    )
    second = first.model_copy(
        update={
            "entity": _ref("Deployment", "checkout"),
            "evidence_ids": ("raw:second",),
            "summary": "second change",
        }
    )
    gap = _gap("gap:provenance", ())
    observation = InvestigationObservation(
        observation_id="observation:history",
        gap_id=gap.gap_id,
        capability="history",
        target=first.entity,
        outcome=GapOutcomeKind.UNKNOWN,
        payload={"findings": [first.model_dump(mode="json"), second.model_dump(mode="json")]},
        evidence_refs=("raw:first", "raw:second", "raw:unrelated"),
    )
    normalized = normalize_observation(observation, case=case, gap=gap)
    assert [finding.evidence_ids for finding in normalized.findings] == [
        ("raw:first",),
        ("raw:second",),
    ]
    assert set(observation.evidence_refs) == {"raw:first", "raw:second", "raw:unrelated"}


def test_new_frontier_and_observation_state_round_trip_as_plain_data() -> None:
    source = InMemorySource(name="checkpoint-contract")
    state = build_investigation_state(source)
    state["attempted_observations"] = ("events|shop/Pod/checkout-0|{}",)
    state["frontier_queried_dimensions"] = (
        ("alternative:config", (GapDimension.CONFIG_DIFFERENCE.value,)),
    )
    serde = JsonPlusSerializer(pickle_fallback=False)
    for key in ("attempted_observations", "frontier_queried_dimensions"):
        restored = serde.loads_typed(serde.dumps_typed(state[key]))
        assert _frozen(restored) == _frozen(state[key])
