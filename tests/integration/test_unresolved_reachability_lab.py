"""Production-path smoke coverage for the P2E-0 controls."""

from __future__ import annotations

from packages.evals.unresolved_reachability import build_blind_audit
from packages.rca.demo import demo_source


def test_real_source_produces_full_seed_and_exhaustive_controls() -> None:
    audit = build_blind_audit(demo_source())

    assert audit.full.episodes
    assert audit.seed.resolution in {"RESOLVED", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE"}
    assert audit.exhaustive_fixed_point
    assert audit.fixed_point_legal_unseen == 0
    assert audit.exhaustive_active.episodes


def test_query_effects_are_not_inferred_from_normalizer_labels_only() -> None:
    audit = build_blind_audit(demo_source())

    assert audit.query_effects
    assert all(item.prebuild_outcome for item in audit.query_effects)
    assert all(item.effect_class for item in audit.query_effects)
