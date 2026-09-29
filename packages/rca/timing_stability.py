"""Onset uncertainty and timing stability (M21 timing contract ``m21-timing.v1``).

The onset of record (M21 §10.2, ``H0``) is one alert-episode start. It can be a one-snapshot
blip that precedes the incident's persistent episode, so decisions that lean on its exact
position are fragile. This module derives, from alert captures observed at or before the
revision cutoff and nothing else, the set ``O`` of onsets the same evidence admits, and
computes how far each claim's formation and each temporal relation a decision relied on hold
over that set. No count, duration or alert value participates, and nothing later than the
cutoff can enter.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from packages.rca.model import (
    AlertEpisode,
    ClaimTiming,
    Hypothesis,
    OnsetCandidate,
    OnsetOutcome,
    OnsetUncertainty,
    RelationTiming,
    ResolutionTrace,
    TimingAssessment,
    TimingStability,
)
from packages.rca.signals import is_background_alert

D1_RULE_ID = "m21.support.change-onset-path"
RELATION_D1 = "d1"
RELATION_ONSET = "onset_relation"
RELATION_TEMPORAL_CONTRADICTION = "temporal_contradiction"
RELATION_ENDED_EPISODE = "ended_episode"
_TEMPORAL_CONTRADICTION_CODE = "EXPLICIT_TEMPORAL_CONTRADICTION"
_ENDED_EPISODE_CODE = "MANIFESTATION_EPISODE_ENDED_BEFORE_ONSET"
ELIMINATED = "ELIMINATED"
NOT_ELIMINATED = "NONE"


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


@dataclass(frozen=True)
class ClaimView:
    """What one exact claim looked like under one onset, keyed by its onset-free identity."""

    actor: str
    mechanism: str
    evidence: frozenset[str]
    relations: Mapping[str, str]


@dataclass(frozen=True)
class OnsetView:
    """One admissible onset's diagnosis: its status and every matchable claim."""

    diagnosis_status: str
    claims: Mapping[str, ClaimView]


def claim_views(hypotheses: Sequence[Hypothesis], trace: ResolutionTrace) -> dict[str, ClaimView]:
    """Per-claim relations a decision may rely on, for one onset.

    Claims are matched across onsets by ``hypothesis_key``; a key that repeats in one revision
    is ambiguous, so its claims are left out and never look stable.
    """
    audits = {audit.hypothesis_id: audit for audit in trace.hypothesis_audits}
    codes: dict[str, set[str]] = defaultdict(set)
    for elimination in trace.eliminations:
        codes[elimination.hypothesis_id].add(elimination.code.value)
    repeats = Counter(h.hypothesis_key for h in hypotheses)
    views: dict[str, ClaimView] = {}
    for hypothesis in hypotheses:
        key = hypothesis.hypothesis_key
        if not key or repeats[key] > 1:
            continue
        audit = audits.get(hypothesis.hypothesis_id)
        d1 = next(
            (
                r.status.value
                for r in (audit.root_support if audit else ())
                if r.rule_id == D1_RULE_ID
            ),
            "ABSENT",
        )
        own = codes.get(hypothesis.hypothesis_id, set())
        views[key] = ClaimView(
            actor=hypothesis.causal_actor.canonical,
            mechanism=hypothesis.mechanism,
            evidence=frozenset(e for f in hypothesis.findings for e in f.evidence_ids),
            relations={
                RELATION_D1: d1,
                RELATION_ONSET: ",".join(audit.onset_relation) if audit else "",
                RELATION_TEMPORAL_CONTRADICTION: (
                    ELIMINATED if _TEMPORAL_CONTRADICTION_CODE in own else NOT_ELIMINATED
                ),
                RELATION_ENDED_EPISODE: ELIMINATED
                if _ENDED_EPISODE_CODE in own
                else NOT_ELIMINATED,
            },
        )
    return views


def compute_timing_assessment(
    uncertainty: OnsetUncertainty, views: Mapping[datetime, OnsetView]
) -> TimingAssessment:
    """Formation and adjudication stability of every claim of the onset of record.

    Every admissible onset in ``O`` must have been evaluated; otherwise nothing is claimed.
    With a single member nothing can vary, so everything is STABLE by definition.
    """
    members = [m.onset for m in uncertainty.members]
    if (
        not uncertainty.assessable
        or uncertainty.h0 not in views
        or any(onset not in views for onset in members)
    ):
        return TimingAssessment(uncertainty=uncertainty, status=TimingStability.UNASSESSED)
    evaluated = {onset: views[onset] for onset in members}
    base = evaluated[uncertainty.h0]  # type: ignore[index]
    status = (
        TimingStability.STABLE
        if len({v.diagnosis_status for v in evaluated.values()}) == 1
        else TimingStability.SENSITIVE
    )
    claims = []
    for key in sorted(base.claims):
        present = [v.claims[key] for v in evaluated.values() if key in v.claims]
        formation = (
            TimingStability.STABLE
            if len(present) == len(evaluated)
            and len({c.evidence for c in present}) == 1
            and len({c.mechanism for c in present}) == 1
            else TimingStability.SENSITIVE
        )
        relations = []
        for name in sorted(base.claims[key].relations):
            values = sorted({c.relations.get(name, "ABSENT") for c in present})
            relations.append(
                RelationTiming(
                    relation=name,
                    stability=TimingStability.STABLE
                    if len(values) == 1
                    else TimingStability.SENSITIVE,
                    values=tuple(values),
                )
            )
        claims.append(
            ClaimTiming(
                hypothesis_key=key,
                actor=base.claims[key].actor,
                formation=formation,
                adjudication=(
                    TimingStability.SENSITIVE
                    if any(r.stability is TimingStability.SENSITIVE for r in relations)
                    else TimingStability.STABLE
                ),
                relations=tuple(relations),
            )
        )
    return TimingAssessment(
        uncertainty=uncertainty,
        status=status,
        outcomes=tuple(
            OnsetOutcome(onset=onset, diagnosis_status=evaluated[onset].diagnosis_status)
            for onset in sorted(evaluated)
        ),
        claims=tuple(claims),
    )


__all__ = [
    "ClaimView",
    "OnsetView",
    "alert_fingerprint",
    "claim_views",
    "compute_timing_assessment",
    "derive_onset_uncertainty",
    "source_alert_episodes",
]
