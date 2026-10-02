"""The shadow of m21 §11: who stays eligible to lead among competing supported candidates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.evals.temporal_relevance import shadow_leadership

ONSET = datetime(2026, 10, 1, 13, 29, tzinfo=UTC)
W = timedelta(minutes=5)


def at(minutes: float) -> str:
    return (ONSET + timedelta(minutes=minutes)).isoformat()


def chaos(name: str, applied: float, recovered: float | None) -> list[dict[str, Any]]:
    events = [
        {
            "reason": "Applied",
            "involvedObject": {"kind": "NetworkChaos", "name": name},
            "firstTimestamp": at(applied),
        }
    ]
    if recovered is not None:
        events.append(
            {
                "reason": "Recovered",
                "involvedObject": {"kind": "NetworkChaos", "name": name},
                "firstTimestamp": at(recovered),
            }
        )
    return events


def hypothesis(
    kind: str, name: str, findings: list[dict[str, Any]], linked: bool = True
) -> dict[str, Any]:
    return {
        "causal_actor": {"kind": kind, "name": name, "namespace": "shop"},
        "episode_onset": ONSET.isoformat(),
        "initiating_findings": findings,
        "linked_symptoms": ["shop/Deployment/web"] if linked else [],
        "causal_paths": [],
    }


def spec(minutes: float, old: str, new: str) -> dict[str, Any]:
    return {
        "kind": "SPEC_CHANGE",
        "at": at(minutes),
        "summary": f"spec changed: [api].env[DELAY].value: {old} -> {new}",
    }


def document(*hyps: dict[str, Any]) -> dict[str, Any]:
    return {
        "leading_actor_display": "COMPETING",
        "leading_actor_tier": "SUPPORTED",
        "leading_actor_candidates": [h["causal_actor"] for h in hyps],
        "hypothesis": hyps[0],
        "alternative_hypotheses": list(hyps[1:]),
    }


def test_an_experiment_recovered_long_before_yields_to_a_change_in_effect_at_onset() -> None:
    doc = document(
        hypothesis("Deployment", "api", [spec(-1, "unset", "1000")]),
        hypothesis("NetworkChaos", "old", [{"kind": "FAULT_INJECTION", "at": at(-90)}]),
    )
    shadow = shadow_leadership(doc, chaos("old", -90, -88), W)
    assert shadow.display == "SINGLE" and shadow.eligible == (("Deployment", "api"),)


def test_an_old_cause_still_in_effect_is_not_demoted_for_its_age() -> None:
    doc = document(
        hypothesis("NetworkChaos", "slow", [{"kind": "FAULT_INJECTION", "at": at(-40)}]),
        hypothesis("Deployment", "api", [spec(-2, "unset", "1000")]),
    )
    shadow = shadow_leadership(doc, chaos("slow", -40, None), W)  # never recovered: still in effect
    assert not shadow.acted and shadow.display == "COMPETING"


def test_without_a_connected_linked_candidate_nothing_changes() -> None:
    doc = document(
        hypothesis("NetworkChaos", "a", [{"kind": "FAULT_INJECTION", "at": at(-90)}]),
        hypothesis("NetworkChaos", "b", [{"kind": "FAULT_INJECTION", "at": at(-45)}]),
    )
    events = chaos("a", -90, -88) + chaos("b", -45, -43)
    shadow = shadow_leadership(doc, events, W)
    assert not shadow.acted and shadow.display == "COMPETING"  # never NOT_ESTABLISHED


def test_a_recent_candidate_not_linked_to_the_incident_does_not_take_the_lead() -> None:
    doc = document(
        hypothesis("Deployment", "unrelated", [spec(-1, "1", "2")], linked=False),
        hypothesis("NetworkChaos", "old", [{"kind": "FAULT_INJECTION", "at": at(-90)}]),
    )
    shadow = shadow_leadership(doc, chaos("old", -90, -88), W)
    assert not shadow.acted


def test_a_reverted_change_has_ended_and_an_unreverted_one_has_not() -> None:
    reverted = hypothesis(
        "Deployment", "api", [spec(-60, "unset", "1000"), spec(-58, "1000", "unset")]
    )
    live = hypothesis("NetworkChaos", "now", [{"kind": "FAULT_INJECTION", "at": at(-1)}])
    shadow = shadow_leadership(document(reverted, live), chaos("now", -1, None), W)
    assert shadow.eligible == (("NetworkChaos", "now"),) and shadow.demoted == (
        ("Deployment", "api"),
    )


def test_the_window_decides_what_counts_as_ended() -> None:
    doc = document(
        hypothesis("Deployment", "api", [spec(-1, "unset", "1000")]),
        hypothesis("NetworkChaos", "recent", [{"kind": "FAULT_INJECTION", "at": at(-12)}]),
    )
    events = chaos("recent", -12, -10)  # ended 10 minutes before the onset
    assert shadow_leadership(doc, events, timedelta(minutes=5)).display == "SINGLE"
    assert shadow_leadership(doc, events, timedelta(minutes=15)).display == "COMPETING"


def test_a_single_or_strong_display_is_left_alone() -> None:
    doc = document(hypothesis("Deployment", "api", [spec(-1, "unset", "1000")]))
    doc["leading_actor_display"] = "SINGLE"
    assert not shadow_leadership(doc, [], W).acted
