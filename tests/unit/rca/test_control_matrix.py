"""Focused tests for the deterministic control-matrix diagnostics."""

from __future__ import annotations

from packages.rca.demo import demo_source
from packages.rca.investigation.control_matrix import (
    PromotionAudit,
    _frontier_after_audit,
    run_control_matrix,
)
from packages.rca.model import (
    EntityRef,
    FrontierStatus,
    StructuralAlternative,
)


def _alternative() -> StructuralAlternative:
    actor = EntityRef.parse("shop/ConfigMap/checkout-config")
    target = EntityRef.parse("shop/Deployment/checkout")
    return StructuralAlternative(
        alternative_id="alternative:test",
        actor=actor,
        role="configuration_source",
        observation_targets=(actor, target),
    )


def _audit(*, target: str, findings: tuple[str, ...] = ()) -> PromotionAudit:
    return PromotionAudit(
        gap_id="gap:test",
        dimension="CONFIG_DIFFERENCE",
        capability="history",
        target=target,
        raw_records=1,
        returned_refs=("journal:test",),
        new_refs=("journal:test",),
        normalized_findings=findings,
        new_finding_keys=(),
        finding_roles=(),
        candidate_created=bool(findings),
        hypothesis_created_or_updated=bool(findings),
        hypothesis_ids_after=(),
        plausibility_after=(),
        resolution_before="AMBIGUOUS",
        resolution_after="AMBIGUOUS",
        classification="HYPOTHESIS_GROUPING_ABSORBED",
    )


def test_frontier_lifecycle_distinguishes_unexplored_queried_and_promoted() -> None:
    alternative = _alternative()
    actor = alternative.actor.canonical
    target = EntityRef.parse("shop/Deployment/checkout").canonical

    queried = _frontier_after_audit((alternative,), (_audit(target=target),))
    assert queried[0].status is FrontierStatus.QUERIED_NO_CAUSAL_FINDING

    promoted = _frontier_after_audit(
        (alternative,), (_audit(target=actor, findings=(f"CONFIG_CHANGE:{actor}:journal:test",)),)
    )
    assert promoted[0].status is FrontierStatus.PROMOTED

    untouched = _frontier_after_audit((alternative,), ())
    assert untouched[0].status is FrontierStatus.UNEXPLORED


def test_control_matrix_runs_production_four_way_path_without_llm() -> None:
    result = run_control_matrix(demo_source(), max_depth=1, max_states=8)

    assert result.incident_id == "demo-bad-rollout"
    assert result.full_case.findings
    assert result.seed_case.findings
    assert result.exhaustive_case.findings
    assert result.search.initial_diagnosis is not None
    assert result.search.initial_diagnosis.model_calls == 0
    assert result.frontier_materiality.raw_alternatives >= (
        result.frontier_materiality.material_alternatives
    )
