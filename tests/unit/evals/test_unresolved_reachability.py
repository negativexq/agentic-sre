"""Focused tests for the P2E-0 reachability diagnostics."""

from __future__ import annotations

from packages.evals.unresolved_reachability import (
    DiagnosisEpistemicSnapshot,
    EpisodeReachability,
    EpisodeStateSnapshot,
    _reachability_class,
    _transitions,
    episode_reachability,
)


def _snapshot(actor: str, state: str) -> EpisodeStateSnapshot:
    return EpisodeStateSnapshot(
        actor=actor,
        hypothesis_id=f"hypothesis:{actor}",
        members=(),
        manifestations=(),
        epistemic_state=state,
        reason_codes=(),
        verification_decision=None,
        finding_kinds=(),
        finding_keys=(),
        initiating_finding_keys=(),
        supporting_finding_keys=(),
        contradictory_finding_keys=(),
        causal_path_shape=(),
    )


def _diagnosis(*episodes: EpisodeStateSnapshot) -> DiagnosisEpistemicSnapshot:
    states: dict[str, list[str]] = {"SUPPORTED": [], "UNRESOLVED": [], "CONTRADICTED": []}
    for episode in episodes:
        states[episode.epistemic_state].append(episode.actor)
    return DiagnosisEpistemicSnapshot(
        resolution="AMBIGUOUS",
        supported_actors=tuple(states["SUPPORTED"]),
        unresolved_actors=tuple(states["UNRESOLVED"]),
        contradicted_actors=tuple(states["CONTRADICTED"]),
        leading_actors=tuple(item.actor for item in episodes),
        episodes=episodes,
    )


def test_unresolved_to_supported_and_contradicted_transitions_are_post_rebuild() -> None:
    before = (("ns/Deployment/a", "UNRESOLVED"), ("ns/Deployment/b", "SUPPORTED"))
    after = (("ns/Deployment/a", "SUPPORTED"), ("ns/Deployment/b", "CONTRADICTED"))
    assert _transitions(before, after) == (
        "SUPPORTED_TO_CONTRADICTED",
        "UNRESOLVED_TO_SUPPORTED",
    )


def test_full_unresolved_is_a_semantic_ceiling() -> None:
    assert _reachability_class("UNRESOLVED", "UNRESOLVED", "UNRESOLVED") == (
        "FULL_SEMANTIC_CEILING_UNRESOLVED"
    )


def test_active_more_decisive_than_full_fails_closed() -> None:
    assert _reachability_class("UNRESOLVED", "SUPPORTED", "UNRESOLVED") == (
        "ACTIVE_EXCEEDS_FULL_CONTROL"
    )


def test_missing_full_projection_is_explicitly_absent() -> None:
    seed = _diagnosis(_snapshot("ns/Deployment/a", "UNRESOLVED"))
    active = _diagnosis(_snapshot("ns/Deployment/a", "UNRESOLVED"))
    full = _diagnosis()
    assert episode_reachability(seed, active, full)[0].reachability_class == (
        "FULL_SOURCE_EPISODE_ABSENT"
    )


def test_projection_collision_is_not_attributed_to_one_episode() -> None:
    seed = _diagnosis(_snapshot("ns/Deployment/a", "UNRESOLVED"))
    active = _diagnosis(
        _snapshot("ns/Deployment/a", "UNRESOLVED"),
        _snapshot("ns/Deployment/a", "SUPPORTED"),
    )
    full = _diagnosis(_snapshot("ns/Deployment/a", "SUPPORTED"))
    result = episode_reachability(seed, active, full)
    assert result == (
        EpisodeReachability(
            actor="ns/Deployment/a",
            seed_state="UNRESOLVED",
            active_state=None,
            full_state=None,
            reachability_class="PROJECTION_COLLISION",
        ),
    )
