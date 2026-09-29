"""Onset uncertainty and timing stability (M21 timing contract ``m21-timing.v1``).

The onset of record (M21 §10.2, ``H0``) is one alert-episode start. It can be a one-snapshot
blip that precedes the incident's persistent episode, so decisions that lean on its exact
position are fragile. This module derives, from alert captures observed at or before the
revision cutoff and nothing else, the set ``O`` of onsets the same evidence admits. No count,
duration or alert value participates, and nothing later than the cutoff can enter.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any

from packages.rca.model import AlertEpisode, OnsetCandidate, OnsetUncertainty
from packages.rca.signals import is_background_alert


def alert_fingerprint(labels: Mapping[str, Any]) -> str:
    """Stable identity of an alert series: every label, order-independent."""
    material = json.dumps({str(k): str(v) for k, v in labels.items()}, sort_keys=True)
    return sha256(material.encode("utf-8")).hexdigest()[:16]


def source_alert_episodes(source: object) -> tuple[AlertEpisode, ...] | None:
    """The source's captured alert episodes, or ``None`` when it cannot provide them.

    A source without capture history (for example a live receiver that keeps no ended
    episodes) makes every timing outcome UNASSESSED; nothing is inferred in its place.
    """
    method = getattr(source, "alert_episodes", None)
    if not callable(method):
        return None
    episodes = method()
    return None if episodes is None else tuple(episodes)


def derive_onset_uncertainty(
    episodes: Sequence[AlertEpisode] | None,
    *,
    alert_observation_start: datetime | None,
    cutoff: datetime | None,
) -> OnsetUncertainty:
    """Derive ``O`` from qualified (non-background, not pre-existing) episodes.

    ``H0`` is the earliest qualified episode start, exactly the §10.2 onset. ``U`` is the
    earliest qualified start that was still firing in the last capture at or before the
    cutoff, or the latest qualified start when none was. ``O`` is every qualified start in
    ``[H0, U]``.
    """
    if episodes is None:
        return OnsetUncertainty(
            reason="NO_ALERT_CAPTURE_HISTORY",
            cutoff=cutoff,
            alert_observation_start=alert_observation_start,
        )
    if alert_observation_start is None:
        return OnsetUncertainty(
            reason="ALERT_COVERAGE_UNKNOWN",
            cutoff=cutoff,
            alert_observation_start=alert_observation_start,
        )
    qualified = sorted(
        (
            e
            for e in episodes
            if not is_background_alert(e.name) and e.starts_at >= alert_observation_start
        ),
        key=lambda e: (e.starts_at, e.fingerprint),
    )
    if not qualified:
        return OnsetUncertainty(
            reason="NO_NEW_EPISODE",
            cutoff=cutoff,
            alert_observation_start=alert_observation_start,
        )
    h0 = qualified[0].starts_at
    firing = [e.starts_at for e in qualified if e.firing_at_cutoff]
    upper = min(firing) if firing else qualified[-1].starts_at
    starts = sorted({e.starts_at for e in qualified if h0 <= e.starts_at <= upper})
    members = tuple(
        OnsetCandidate(
            onset=start,
            is_h0=start == h0,
            is_upper=start == upper,
            episodes=tuple(e for e in qualified if e.starts_at == start),
        )
        for start in starts
    )
    return OnsetUncertainty(
        reason="ASSESSABLE",
        cutoff=cutoff,
        alert_observation_start=alert_observation_start,
        h0=h0,
        upper=upper,
        members=members,
    )


__all__ = ["alert_fingerprint", "derive_onset_uncertainty", "source_alert_episodes"]
