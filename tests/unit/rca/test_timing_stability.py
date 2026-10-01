"""The evidence-derived onset set O (M21 timing contract §3)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from rca_builders import alert, at

from packages.rca.model import AlertEpisode, OnsetUncertainty
from packages.rca.signals import extract_symptoms
from packages.rca.source import InMemorySource
from packages.rca.timing_stability import derive_onset_uncertainty, source_alert_episodes

W = at(0)
CUTOFF = at(60)


def episode(
    name: str,
    start: float,
    *,
    firing: bool = False,
    fingerprint: str | None = None,
    captures: int = 1,
) -> AlertEpisode:
    return AlertEpisode(
        fingerprint=fingerprint or f"{name}-{start}",
        name=name,
        starts_at=at(start),
        first_capture=at(start),
        last_capture=at(start + captures),
        captures=captures,
        firing_at_cutoff=firing,
        evidence_ids=(f"alerts_at_{start}.json",),
    )


def derive(
    *episodes: AlertEpisode,
    w: datetime | None = W,
    cutoff: datetime | None = CUTOFF,
) -> OnsetUncertainty:
    return derive_onset_uncertainty(episodes, alert_observation_start=w, cutoff=cutoff)


def test_h0_is_the_earliest_qualified_episode_and_upper_the_earliest_still_firing() -> None:
    result = derive(
        episode("RequestErrorRate", 10),
        episode("RequestErrorRate", 14, fingerprint="b"),
        episode("RequestLatency", 20, firing=True),
        episode("RequestErrorRate", 30, firing=True, fingerprint="c"),
    )
    assert result.reason == "ASSESSABLE"
    assert (result.h0, result.upper) == (at(10), at(20))
    assert [m.onset for m in result.members] == [at(10), at(14), at(20)]
    assert [(m.is_h0, m.is_upper) for m in result.members] == [
        (True, False),
        (False, False),
        (False, True),
    ]


def test_upper_is_the_latest_start_when_nothing_is_still_firing() -> None:
    result = derive(
        episode("A", 5), episode("A", 9, fingerprint="b"), episode("A", 12, fingerprint="c")
    )
    assert (result.h0, result.upper) == (at(5), at(12))
    assert len(result.members) == 3


def test_single_episode_gives_a_single_member_and_no_uncertainty() -> None:
    result = derive(episode("A", 7, firing=True))
    assert [m.onset for m in result.members] == [at(7)]
    assert result.members[0].is_h0 and result.members[0].is_upper


def test_background_and_pre_existing_episodes_never_qualify() -> None:
    result = derive(
        episode("Watchdog", 1),
        episode("KubeSchedulerDown", 2),
        episode("Chronic", -30, firing=True),  # began before W: pre-existing
        episode("RequestErrorRate", 8, firing=True),
    )
    assert (result.h0, result.upper) == (at(8), at(8))
    assert [m.onset for m in result.members] == [at(8)]


def test_episodes_sharing_a_start_are_one_member_that_keeps_every_derivation() -> None:
    result = derive(
        episode("A", 4, fingerprint="x"),
        episode("B", 4, fingerprint="y"),
        episode("A", 9, firing=True, fingerprint="z"),
    )
    first = result.members[0]
    assert sorted(e.fingerprint for e in first.episodes) == ["x", "y"]


@pytest.mark.parametrize(
    ("episodes", "w", "reason"),
    [
        (None, W, "NO_ALERT_CAPTURE_HISTORY"),
        ((episode("A", 3),), None, "ALERT_COVERAGE_UNKNOWN"),
        ((episode("A", -5, firing=True),), W, "NO_NEW_EPISODE"),
        ((), W, "NO_NEW_EPISODE"),
    ],
)
def test_unassessable_onsets_have_no_members(
    episodes: tuple[AlertEpisode, ...] | None, w: datetime | None, reason: str
) -> None:
    result = derive_onset_uncertainty(episodes, alert_observation_start=w, cutoff=CUTOFF)
    assert (result.reason, result.members, result.assessable) == (reason, (), False)


def test_input_order_does_not_change_the_set() -> None:
    items = [
        episode("A", 10),
        episode("A", 15, firing=True, fingerprint="b"),
        episode("B", 12, fingerprint="c"),
    ]
    forward = derive(*items)
    backward = derive(*reversed(items))
    assert forward == backward


def test_h0_equals_the_section_10_2_onset() -> None:
    alerts = [alert("RequestErrorRate", "checkout", 11), alert("RequestLatency", "checkout", 18)]
    onset = extract_symptoms(alerts, alert_observation_start=W).onset
    source = InMemorySource(name="x", alert_items=alerts, alert_coverage_start=W, cutoff=CUTOFF)
    result = derive_onset_uncertainty(
        source_alert_episodes(source), alert_observation_start=W, cutoff=CUTOFF
    )
    assert result.h0 == onset == at(11)


def test_in_memory_alerts_are_single_capture_episodes_still_firing() -> None:
    source = InMemorySource(
        name="x", alert_items=[alert("RequestErrorRate", "checkout", 5)], cutoff=CUTOFF
    )
    (only,) = source_alert_episodes(source) or ()
    assert only.firing_at_cutoff and only.starts_at == at(5) and only.last_capture == CUTOFF


def test_explicit_capture_history_is_used_verbatim() -> None:
    history = [episode("A", 3, captures=2)]
    source = InMemorySource(name="x", alert_episode_items=history)
    assert source_alert_episodes(source) == tuple(history)


def test_a_source_without_capture_history_is_unassessed_not_guessed() -> None:
    class Bare:
        def alerts(self) -> list[Any]:
            return []

    assert source_alert_episodes(Bare()) is None
    assert (
        derive_onset_uncertainty(None, alert_observation_start=W, cutoff=None).assessable is False
    )


DATASET = Path(".local/itbench-lite")


@pytest.mark.skipif(not DATASET.exists(), reason="ITBench-Lite snapshots are local only")
def test_snapshot_capture_history_matches_the_engine_onset() -> None:
    from packages.evals.itbench.dataset import ITBenchLiteDataset
    from packages.evals.itbench.source import SnapshotSource

    source = SnapshotSource(ITBenchLiteDataset.open(DATASET).scenario("Scenario-17"))
    episodes = source_alert_episodes(source)
    assert episodes
    onset = extract_symptoms(
        source.alerts(), alert_observation_start=source.alert_observation_start()
    ).onset
    result = derive_onset_uncertainty(
        episodes,
        alert_observation_start=source.alert_observation_start(),
        cutoff=source.observation_cutoff(),
    )
    assert onset is not None and result.h0 == onset
    assert result.upper is not None and result.upper >= onset
    assert all(result.h0 <= m.onset <= result.upper for m in result.members)
    assert timedelta(0) < result.upper - result.h0
