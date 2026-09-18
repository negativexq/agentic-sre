"""Unit coverage for the P2C first-loss classifier."""

from __future__ import annotations

from packages.evals.causal_completeness import (
    classify_first_loss,
    forbidden_blind_keys,
    plausibility_diagnostic,
)


def _stage(**overrides: object) -> str:
    values: dict[str, object] = {
        "snapshot_matches": ("ns/Deployment/a",),
        "source_matches": ("ns/Deployment/a",),
        "direct_findings": ("finding",),
        "related_findings": (),
        "candidate_matches": ("ns/Deployment/a",),
        "hypothesis_matches": ("h:a",),
        "plausible_matches": ("h:a",),
        "leading_matches": ("h:a",),
        "resolution": "RESOLVED",
    }
    values.update(overrides)
    return classify_first_loss(**values)  # type: ignore[arg-type]


def test_first_loss_classifier_covers_every_stage() -> None:
    assert _stage(snapshot_matches=()) == "SNAPSHOT_ENTITY_NOT_OBSERVABLE"
    assert _stage(source_matches=()) == "SOURCE_ADAPTER_ENTITY_GAP"
    assert _stage(direct_findings=()) == "FINDING_EXTRACTION_GAP"
    assert _stage(direct_findings=(), related_findings=("related",)) == "FINDING_RELATED_ONLY"
    assert _stage(candidate_matches=()) == "CANDIDATE_CREATION_GAP"
    assert _stage(hypothesis_matches=()) == "HYPOTHESIS_GROUPING_GAP"
    assert _stage(hypothesis_matches=("h:a", "h:b")) == "MULTIPLE_MATCHING_HYPOTHESIS_EPISODES"
    assert _stage(plausible_matches=()) == "PLAUSIBILITY_ELIMINATION"
    assert _stage(leading_matches=()) == "PLAUSIBLE_NOT_LEADING"
    assert _stage(resolution="AMBIGUOUS") == "LEADING_AMBIGUOUS"
    assert _stage() == "UNIQUELY_RESOLVED"


def test_plausibility_diagnostic_preserves_multiple_reasons() -> None:
    assert plausibility_diagnostic(("NO_CAUSAL_SYMPTOM_LINK",)) == "LINKAGE_ELIMINATION"
    assert (
        plausibility_diagnostic(("NO_ONSET_CAPABLE_INITIATING_EVIDENCE",))
        == "INITIATING_EVIDENCE_ELIMINATION"
    )
    assert (
        plausibility_diagnostic(("EXPLICIT_TEMPORAL_CONTRADICTION",))
        == "TEMPORAL_CONTRADICTION_ELIMINATION"
    )
    assert (
        plausibility_diagnostic(("NO_CAUSAL_SYMPTOM_LINK", "EXPLICIT_TEMPORAL_CONTRADICTION"))
        == "MULTIPLE_PLAUSIBILITY_FAILURES"
    )


def test_forbidden_blind_fields_are_recursive() -> None:
    assert forbidden_blind_keys({"nested": {"ground_truth": False}}) == ("nested.ground_truth",)
    assert forbidden_blind_keys({"resolution": "AMBIGUOUS"}) == ()


def test_first_loss_invariants_are_monotonic_by_stage() -> None:
    assert _stage(direct_findings=(), candidate_matches=("ignored",)) == "FINDING_EXTRACTION_GAP"
    assert _stage(hypothesis_matches=(), plausible_matches=("h:a",)) == "HYPOTHESIS_GROUPING_GAP"
