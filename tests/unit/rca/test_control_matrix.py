"""Focused tests for the deterministic control-matrix diagnostics."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

import packages.rca.investigation.control_matrix as control_matrix
from packages.rca.demo import demo_source
from packages.rca.frontier import apply_frontier_progress
from packages.rca.investigation.control_matrix import (
    PromotionAudit,
    _audit_outcome,
    _capabilities_for_finding_kind,
    _finding_promotion_effect,
    _full_only_reason,
    _outcome,
    exhaustive_active_closure,
    run_control_matrix,
    semantic_finding_key,
)
from packages.rca.investigation.environment import initial_view, investigation_backend
from packages.rca.investigation.multi_step_search import (
    MultiStepSearchResult,
    SearchPath,
    SearchStep,
    legal_query_choices,
    query_templates,
)
from packages.rca.investigation.tools import default_tools, make_observation
from packages.rca.model import (
    AuthorizedQuery,
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
    InvestigationObservation,
    InvestigationQuery,
    Lifecycle,
    ObjectVersion,
    Resolution,
    StructuralAlternative,
)


def _alternative() -> StructuralAlternative:
    actor = EntityRef.parse("shop/ConfigMap/checkout-config")
    target = EntityRef.parse("shop/Deployment/checkout")
    return StructuralAlternative(
        alternative_id="alternative:test",
        actor=actor,
        role="configuration_source",
        queryable_dimensions=(GapDimension.CONFIG_DIFFERENCE,),
        observation_targets=(actor, target),
    )


def test_frontier_lifecycle_distinguishes_unexplored_queried_and_promoted() -> None:
    alternative = _alternative()

    queried = apply_frontier_progress(
        (alternative,),
        hypotheses=(),
        queried_dimensions_by_alternative={
            alternative.alternative_id: (GapDimension.CONFIG_DIFFERENCE,)
        },
    )
    assert queried[0].status is FrontierStatus.QUERIED_NO_CAUSAL_FINDING

    promoted = apply_frontier_progress(
        (alternative,),
        hypotheses=(Hypothesis(hypothesis_id="hypothesis:test", causal_actor=alternative.actor),),
        queried_dimensions_by_alternative={},
    )
    assert promoted[0].status is FrontierStatus.PROMOTED

    untouched = apply_frontier_progress(
        (alternative,), hypotheses=(), queried_dimensions_by_alternative={}
    )
    assert untouched[0].status is FrontierStatus.UNEXPLORED


def test_control_matrix_runs_production_four_way_path_without_llm() -> None:
    result = run_control_matrix(demo_source(), max_depth=1, max_states=8)

    assert result.incident_id == "demo-bad-rollout"
    assert result.full_case.findings
    assert result.seed_case.findings
    assert result.exhaustive_case.findings
    assert result.search.initial_diagnosis is not None
    assert result.search.initial_diagnosis.model_calls == 0
    assert result.frontier_state.raw_alternatives >= len(
        result.frontier_state.structural_shape_groups
    )


def test_bounded_depth_results_are_monotonic_within_depth() -> None:
    source = demo_source()
    matrix = run_control_matrix(source, max_depth=1, max_states=1)
    case = matrix.search.initial_case
    diagnosis = matrix.search.initial_diagnosis
    step = SearchStep(
        depth=1,
        gap_id="gap:test",
        dimension="CONFIG_DIFFERENCE",
        capability="history",
        target="shop/ConfigMap/test",
        raw_records=1,
        payload_fields=("versions",),
        returned_refs=("raw:test",),
        new_refs=("raw:test",),
        normalized_findings=(),
        new_findings=(),
        hypotheses_before=1,
        hypotheses_after=1,
        hypotheses_changed=False,
        resolution_before="AMBIGUOUS",
        resolution_after="RESOLVED",
    )
    result = MultiStepSearchResult(
        incident_id="test",
        seed_policy="test",
        initial_case=case,
        initial_diagnosis=diagnosis,
        attempts=(step,),
        solutions=(SearchPath((step,), diagnosis),),
        max_depth=3,
        explored_states=1,
        truncated=False,
    )
    assert result.resolvable_within(1)
    assert result.resolvable_within(2)
    assert result.resolvable_within(3)
    truncated_after_solution = replace(result, truncated=True, truncation_depth=1)
    assert truncated_after_solution.depth_status(1) == "RESOLVED"
    assert truncated_after_solution.depth_status(2) == "RESOLVED"
    assert truncated_after_solution.depth_status(3) == "RESOLVED"


def test_bounded_depth_status_reports_truncation_honestly() -> None:
    source = demo_source()
    matrix = run_control_matrix(source, max_depth=3, max_states=1)
    truncated_at_one = replace(
        matrix.search,
        solutions=(),
        truncated=True,
        truncation_depth=1,
    )
    assert truncated_at_one.depth_status(1) == "NOT_RESOLVED"
    assert truncated_at_one.depth_status(2) == "UNKNOWN_TRUNCATED"
    assert truncated_at_one.depth_status(3) == "UNKNOWN_TRUNCATED"

    truncated_at_two = replace(truncated_at_one, truncation_depth=2)
    assert truncated_at_two.depth_status(1) == "NOT_RESOLVED"
    assert truncated_at_two.depth_status(2) == "NOT_RESOLVED"
    assert truncated_at_two.depth_status(3) == "UNKNOWN_TRUNCATED"

    complete = replace(truncated_at_one, truncated=False, truncation_depth=None)
    assert complete.depth_status(1) == "NOT_RESOLVED"
    assert complete.depth_status(2) == "NOT_RESOLVED"
    assert complete.depth_status(3) == "NOT_RESOLVED"


def test_diagnostic_finding_key_ignores_provenance_and_onset_annotations() -> None:
    entity = EntityRef.parse("shop/Deployment/checkout")
    first = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=entity,
        at=datetime(2026, 1, 1, tzinfo=UTC),
        summary="changed",
        evidence_ids=("raw:a",),
        details={"observation_id": "obs-a", "investigation_gap_id": "gap-a"},
        temporal_role=EvidenceTemporalRole.INITIATING,
    )
    second = first.model_copy(
        update={
            "evidence_ids": ("raw:b",),
            "details": {
                "observation_id": "obs-b",
                "investigation_gap_id": "gap-b",
                "investigation_capability": "history",
            },
        }
    )
    assert semantic_finding_key(first) == semantic_finding_key(second)

    consequence = first.model_copy(update={"temporal_role": EvidenceTemporalRole.CONSEQUENCE})
    assert semantic_finding_key(first) != semantic_finding_key(consequence)


def test_raw_query_outcomes_are_distinct() -> None:
    observation = InvestigationObservation(
        observation_id="obs",
        gap_id=_gap_outcome_fixture(),
        capability="history",
        target=EntityRef.parse("shop/Deployment/checkout"),
        outcome=GapOutcomeKind.UNKNOWN,
        payload={},
    )
    error = observation.model_copy(update={"error": "tool failed"})
    raw = observation.model_copy(update={"payload": {"versions": [{}]}})
    finding = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=observation.target,
        at=None,
        summary="new",
    )
    assert _outcome(error, 0, (), ()) == "TOOL_ERROR"
    assert _outcome(observation, 0, (), ()) == "NO_RAW_RECORDS"
    assert _outcome(raw, 1, (), ()) == "ALREADY_KNOWN_RAW"
    assert _outcome(raw, 1, (), (finding,)) == "FINDING_PRODUCED"
    assert _outcome(raw, 1, ("raw:new",), ()) == "NOVEL_RAW_NO_FINDING"
    assert _outcome(raw, 1, ("raw:new",), (finding,)) == "FINDING_PRODUCED"


def test_builtin_normalizer_capability_map_is_exact() -> None:
    history = (
        FindingKind.OBJECT_CREATED,
        FindingKind.OBJECT_DELETED,
        FindingKind.CONFIG_CHANGE,
        FindingKind.SPEC_CHANGE,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SCALE_CHANGE,
        FindingKind.ROLLOUT_RESTART,
    )
    for kind in history:
        assert _capabilities_for_finding_kind(kind) == ("history",)
    for kind in (FindingKind.FAILURE_EVENT, FindingKind.AUTOSCALING_FAILURE):
        assert _capabilities_for_finding_kind(kind) == ("events",)
    assert _capabilities_for_finding_kind(FindingKind.DEPENDENCY_ERRORS) == ("logs",)
    assert _capabilities_for_finding_kind(FindingKind.RESOURCE_PRESSURE) == ("resource_pressure",)
    assert _capabilities_for_finding_kind(FindingKind.TRAFFIC_INCREASE) == ("traffic",)
    for kind in (
        FindingKind.POLICY_CREATED,
        FindingKind.FAULT_INJECTION,
        FindingKind.CONTAINER_FAILURE,
        FindingKind.NETWORK_RESTRICTION,
    ):
        assert _capabilities_for_finding_kind(kind) == ()


def _promotion_audit(
    *, capability: str = "history", target: str = "shop/ConfigMap/a"
) -> PromotionAudit:
    return PromotionAudit(
        gap_id="gap:test",
        dimension="CONFIG_DIFFERENCE",
        capability=capability,
        target=target,
        alternative_ids=(),
        query_template="full-history",
        observation_identity="observation:test",
        raw_records=1,
        returned_refs=("raw:after",),
        new_refs=("raw:after",),
        already_seen_refs=(),
        normalized_finding_core_keys=(),
        new_finding_core_keys=(),
        post_rebuild_finding_keys=(),
        post_rebuild_roles=(),
        candidate_created=False,
        hypothesis_linked=False,
        resolution_before="AMBIGUOUS",
        resolution_after="AMBIGUOUS",
        attempt_outcome="NOVEL_RAW_NO_FINDING",
        promotion_classification=None,
    )


def test_partial_full_finding_evidence_is_not_normalization_gap() -> None:
    finding = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=EntityRef.parse("shop/ConfigMap/a"),
        at=None,
        summary="changed",
        evidence_ids=("raw:before", "raw:after"),
    )
    audit = _promotion_audit()
    tools: Any = {"history": object()}
    assert (
        _full_only_reason(
            finding,
            active_refs={"raw:after"},
            audits=(audit,),
            tools=tools,
        )
        == "RAW_RECORD_NOT_RETURNED"
    )
    assert (
        _full_only_reason(
            finding,
            active_refs={"raw:before", "raw:after"},
            audits=(audit,),
            tools=tools,
        )
        == "RAW_RETURNED_NORMALIZATION_GAP"
    )


def test_finding_from_known_raw_takes_precedence_in_outcome() -> None:
    observation = InvestigationObservation(
        observation_id="obs-known",
        gap_id=_gap_outcome_fixture(),
        capability="history",
        target=EntityRef.parse("shop/Deployment/checkout"),
        outcome=GapOutcomeKind.UNKNOWN,
        payload={"versions": [{"id": "known"}]},
        evidence_refs=("raw:known",),
    )
    finding = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=observation.target,
        at=None,
        summary="new semantic signal",
    )
    assert _outcome(observation, 1, (), (finding,)) == "FINDING_PRODUCED"


def test_raw_records_without_refs_are_an_audit_defect() -> None:
    observation = InvestigationObservation(
        observation_id="obs-unreferenced",
        gap_id=_gap_outcome_fixture(),
        capability="history",
        target=EntityRef.parse("shop/Deployment/checkout"),
        outcome=GapOutcomeKind.UNKNOWN,
        payload={"versions": [{"id": "missing-ref"}]},
    )
    assert _audit_outcome(observation, 1, (), (), ()) == (
        "AUDIT_DEFECT",
        "UNREFERENCED_RAW_RECORDS",
    )


def test_finding_promotion_effect_is_per_finding_not_global() -> None:
    case = run_control_matrix(demo_source(), max_depth=1, max_states=1).full_case
    finding = case.findings[0]
    core = control_matrix.semantic_finding_core_key(finding)
    base = _finding_promotion_effect(case, core)
    unrelated = Hypothesis(
        hypothesis_id="unrelated",
        causal_actor=EntityRef.parse("shop/Deployment/unrelated"),
    )
    changed = replace(case, hypotheses=[*case.hypotheses, unrelated])
    assert _finding_promotion_effect(changed, core) == base


def test_shared_finding_losing_hypothesis_linkage_is_a_promotion_gap() -> None:
    case = run_control_matrix(demo_source(), max_depth=1, max_states=1).full_case
    finding = case.findings[0]
    core = control_matrix.semantic_finding_core_key(finding)
    candidate = case.candidates[0].model_copy(update={"findings": (finding,)})
    linked = Hypothesis(
        hypothesis_id="linked",
        causal_actor=candidate.entity,
        findings=(finding,),
    )
    full = replace(case, candidates=[candidate], hypotheses=[linked])
    active = replace(case, candidates=[candidate], hypotheses=[])
    assert _finding_promotion_effect(full, core) != _finding_promotion_effect(active, core)


def test_shared_finding_with_different_causal_actor_is_a_promotion_gap() -> None:
    case = run_control_matrix(demo_source(), max_depth=1, max_states=1).full_case
    finding = case.findings[0]
    core = control_matrix.semantic_finding_core_key(finding)
    first = Hypothesis(
        hypothesis_id="first",
        causal_actor=EntityRef.parse("shop/Deployment/a"),
        findings=(finding,),
    )
    second = first.model_copy(
        update={"hypothesis_id": "second", "causal_actor": EntityRef.parse("shop/Deployment/b")}
    )
    assert _finding_promotion_effect(
        replace(case, hypotheses=[first]), core
    ) != _finding_promotion_effect(replace(case, hypotheses=[second]), core)


def test_real_production_path_closure_reaches_a_safe_fixed_point() -> None:
    result = exhaustive_active_closure(demo_source(), max_rounds=16, max_observations=4096)
    assert result.rounds >= 1
    assert result.truncated is False
    assert result.termination_reason == "FIXED_POINT"
    assert result.audits


def test_legal_query_choices_deduplicate_underlying_observations() -> None:
    source = demo_source()
    bounded = initial_view(source)
    diagnosis = run_control_matrix(source, max_depth=1, max_states=1).seed_diagnosis
    choices = legal_query_choices(
        diagnosis,
        default_tools(investigation_backend(source)),
        query_templates(source),
    )
    identities = [choice.observation_identity for choice in choices]
    assert len(identities) == len(set(identities))
    assert all(choice.gap.resolvability.value == "RESOLVABLE" for choice in choices)
    assert bool(getattr(bounded, "initial_observation_bounded", False))


def test_closure_reports_safety_termination_as_truncated() -> None:
    source = demo_source()
    rounds = exhaustive_active_closure(source, max_rounds=0)
    observations = exhaustive_active_closure(source, max_observations=1)

    assert rounds.truncated
    assert rounds.termination_reason == "MAX_ROUNDS"
    assert observations.truncated
    assert observations.termination_reason == "MAX_OBSERVATIONS"


def test_fixed_point_closure_discovers_a_new_query_after_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = demo_source()
    matrix = run_control_matrix(source, max_depth=1, max_states=1)
    base_case = matrix.seed_case
    base_diagnosis = matrix.seed_diagnosis
    first_target = EntityRef.parse("shop/Deployment/first")
    second_target = EntityRef.parse("shop/Deployment/second")

    def gap(gap_id: str, target: EntityRef) -> InformationGap:
        return InformationGap(
            gap_id=gap_id,
            dimension=GapDimension.CONFIG_DIFFERENCE,
            missing_fact="test fact",
            authorized_queries=(AuthorizedQuery(capability="history", target=target),),
            resolvability=GapResolvability.RESOLVABLE,
        )

    gap_a, gap_b = gap("gap:a", first_target), gap("gap:b", second_target)
    version_body = {"kind": "Deployment", "spec": {"replicas": 1}}
    changed_body = {"kind": "Deployment", "spec": {"replicas": 2}}

    class Tool:
        def execute_query(
            self,
            _case: Any,
            request_gap: InformationGap,
            target: EntityRef,
            _query: InvestigationQuery,
        ) -> InvestigationObservation:
            before = ObjectVersion(
                entity=target,
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                body=version_body,
                evidence_id=f"raw:{request_gap.gap_id}:before",
                lifecycle=Lifecycle.UPDATED,
            )
            after = before.model_copy(
                update={
                    "observed_at": datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
                    "body": changed_body,
                    "evidence_id": "raw:shared-after",
                }
            )
            return make_observation(
                gap=request_gap,
                capability="history",
                target=target,
                payload={
                    "versions": [before.model_dump(mode="json"), after.model_dump(mode="json")]
                },
                evidence_refs=(before.evidence_id, after.evidence_id),
            )

    def fake_initial_view(value: Any, policy: Any = None) -> Any:
        del policy
        return value

    def fake_build_case(value: Any, config: Any = None, extra_findings: Any = ()) -> Any:
        del value, config
        annotated = [
            finding.model_copy(
                update={
                    "temporal_role": EvidenceTemporalRole.INITIATING,
                    "incident_onset": datetime(2026, 1, 1, tzinfo=UTC),
                    "onset_delta_seconds": -30.0,
                }
            )
            for finding in extra_findings
        ]
        return replace(
            base_case,
            findings=annotated,
            candidates=[],
            hypotheses=[],
            structural_alternatives=[],
        )

    def fake_diagnose(case: Any) -> Diagnosis:
        next_gap = gap_b if case.findings else gap_a
        return base_diagnosis.model_copy(
            update={
                "resolution": Resolution.AMBIGUOUS,
                "information_gaps": (next_gap,),
            }
        )

    monkeypatch.setattr(control_matrix, "initial_view", fake_initial_view)
    monkeypatch.setattr(control_matrix, "build_case", fake_build_case)
    monkeypatch.setattr(control_matrix, "diagnose_case", fake_diagnose)
    monkeypatch.setattr(control_matrix, "investigation_backend", lambda _source: object())
    monkeypatch.setattr(control_matrix, "default_tools", lambda _backend: {"history": Tool()})
    monkeypatch.setattr(
        control_matrix,
        "query_templates",
        lambda _source: (("test", InvestigationQuery(limit=1)),),
    )

    result = control_matrix.exhaustive_active_closure(
        source,
        max_rounds=4,
        max_observations=4,
    )

    assert len(result.attempted_observations) == 2
    assert len(result.accumulated_findings) == 2
    assert [audit.gap_id for audit in result.audits] == ["gap:a", "gap:b"]
    assert len(result.unique_new_refs) == 3
    assert result.audits[1].already_seen_refs == ("raw:shared-after",)
    assert result.audits[0].post_rebuild_roles == ("INITIATING",)
    assert result.truncated is False


def _gap_outcome_fixture() -> str:
    return "gap:test"
