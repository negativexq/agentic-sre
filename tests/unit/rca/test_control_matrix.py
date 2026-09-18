"""Focused tests for the deterministic control-matrix diagnostics."""

from __future__ import annotations

from packages.rca.demo import demo_source
from packages.rca.frontier import apply_frontier_progress
from packages.rca.investigation.control_matrix import (
    run_control_matrix,
)
from packages.rca.model import (
    EntityRef,
    FrontierStatus,
    GapDimension,
    Hypothesis,
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
    assert result.frontier_materiality.raw_alternatives >= (
        result.frontier_materiality.material_alternatives
    )
