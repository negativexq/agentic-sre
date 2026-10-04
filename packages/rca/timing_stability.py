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
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from packages.rca.episode_end import RULE_ID as EPISODE_END_RULE_ID
from packages.rca.model import (
    AlertEpisode,
    ClaimTiming,
    Hypothesis,
    HypothesisResolutionAudit,
    OnsetCandidate,
    OnsetOutcome,
    OnsetUncertainty,
    PreconditionStatus,
    RelationTiming,
    ResolutionTrace,
    StatusDriver,
    TimingAssessment,
    TimingStability,
    WithheldAuthority,
)
from packages.rca.signals import is_background_alert

D1_RULE_ID = "m21.support.change-onset-path"
# The strong rule that today proves mechanism execution: an observed quota rejection.
EXECUTION_RULE_IDS = (
    "m21.support.observed-quota-rejection",
    "m21.support.observed-fault-execution",
    "m21.support.observed-rollout-execution",
)
RELATION_D1 = "d1"
RELATION_EXECUTION = "execution"
RELATION_ONSET = "onset_relation"
RELATION_TEMPORAL_CONTRADICTION = "temporal_contradiction"
RELATION_ENDED_EPISODE = "ended_episode"
_TEMPORAL_CONTRADICTION_CODE = "EXPLICIT_TEMPORAL_CONTRADICTION"
_ENDED_EPISODE_CODE = "MANIFESTATION_EPISODE_ENDED_BEFORE_ONSET"
ELIMINATED = "ELIMINATED"
NOT_ELIMINATED = "NONE"
# The rule could not be evaluated under this onset; it neither holds nor fails to hold.
UNASSESSABLE = "UNASSESSABLE"


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
    # SUPPORTED, UNRESOLVED or NONE: whether the claim competes for the root cause.
    standing: str = "NONE"


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
        support = {r.rule_id: r.status.value for r in (audit.root_support if audit else ())}
        d1 = support.get(D1_RULE_ID, "ABSENT")
        own = codes.get(hypothesis.hypothesis_id, set())
        views[key] = ClaimView(
            actor=hypothesis.causal_actor.canonical,
            mechanism=hypothesis.mechanism,
            evidence=frozenset(e for f in hypothesis.findings for e in f.evidence_ids),
            standing=(
                "SUPPORTED"
                if hypothesis.hypothesis_id in trace.plausible_hypotheses
                else "UNRESOLVED"
                if hypothesis.hypothesis_id in trace.unresolved_hypotheses
                else "NONE"
            ),
            relations={
                RELATION_D1: d1,
                RELATION_EXECUTION: next(
                    (support[rule] for rule in EXECUTION_RULE_IDS if rule in support), "ABSENT"
                ),
                RELATION_ONSET: ",".join(audit.onset_relation) if audit else "",
                RELATION_TEMPORAL_CONTRADICTION: (
                    ELIMINATED if _TEMPORAL_CONTRADICTION_CODE in own else NOT_ELIMINATED
                ),
                # Three-valued: an elimination the rule could not even attempt (its evidence
                # window after the deadline is missing) is UNASSESSABLE, not "not eliminated".
                RELATION_ENDED_EPISODE: (
                    ELIMINATED
                    if _ENDED_EPISODE_CODE in own
                    else UNASSESSABLE
                    if _ended_rule_unassessable(audit)
                    else NOT_ELIMINATED
                ),
            },
        )
    return views


def _ended_rule_unassessable(audit: HypothesisResolutionAudit | None) -> bool:
    """Whether the ended-episode rule's preconditions were not met for this claim.

    The rule needs observations after onset plus grace. When the snapshot ends too soon after
    an admissible onset, the rule is blocked: nothing was contradicted, it cannot be judged.
    """
    if audit is None:
        return False
    return any(
        item.rule_id == EPISODE_END_RULE_ID
        and (
            item.reason is not None
            or (item.result is not None and item.result.status is not PreconditionStatus.PASS)
        )
        for item in audit.precondition_audit
    )


_MAX_DRIVERS = 12


def _status_drivers(base: OnsetView, other: OnsetView) -> tuple[StatusDriver, ...]:
    """Claims whose competition standing differs between the onset of record and ``other``.

    Explains a status difference in the words of the evidence: a rule that could not be
    judged is reported as such, not as a contradiction.
    """
    drivers: list[StatusDriver] = []
    for key in sorted(set(base.claims) | set(other.claims)):
        before, after = base.claims.get(key), other.claims.get(key)
        was = before.standing if before else "NONE"
        now = after.standing if after else "NONE"
        if (was == "NONE") == (now == "NONE"):
            continue
        claim = after or before
        assert claim is not None
        if now != "NONE":
            change = "GAINS_COMPETITION"
            if before is None:
                reason = "FORMS_ONLY_UNDER_THIS_ONSET"
            elif after is not None and any(
                before.relations.get(rel) == ELIMINATED and after.relations.get(rel) == UNASSESSABLE
                for rel in (RELATION_ENDED_EPISODE, RELATION_TEMPORAL_CONTRADICTION)
            ):
                reason = "BLOCKED_ENDED_EPISODE_RULE"
            elif after is not None and any(
                before.relations.get(rel) == ELIMINATED
                and after.relations.get(rel) == NOT_ELIMINATED
                for rel in (RELATION_ENDED_EPISODE, RELATION_TEMPORAL_CONTRADICTION)
            ):
                reason = "ELIMINATION_NOT_HOLDING"
            else:
                reason = "OTHER"
        else:
            change = "LEAVES_COMPETITION"
            reason = (
                "NO_LONGER_FORMED" if after is None else "ELIMINATED_OR_EXPLAINED_UNDER_THIS_ONSET"
            )
        drivers.append(
            StatusDriver(hypothesis_key=key, actor=claim.actor, change=change, reason=reason)
        )
    return tuple(drivers[:_MAX_DRIVERS])


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
            # UNASSESSABLE never contradicts anything: instability needs two different
            # values that were each actually assessed (M21 timing contract, relation values).
            assessed = [v for v in values if v != UNASSESSABLE]
            relations.append(
                RelationTiming(
                    relation=name,
                    stability=(
                        TimingStability.SENSITIVE
                        if len(assessed) > 1
                        else TimingStability.STABLE
                        if assessed
                        else TimingStability.UNASSESSED
                    ),
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
            OnsetOutcome(
                onset=onset,
                diagnosis_status=evaluated[onset].diagnosis_status,
                drivers=(
                    _status_drivers(base, evaluated[onset])
                    if evaluated[onset].diagnosis_status != base.diagnosis_status
                    else ()
                ),
            )
            for onset in sorted(evaluated)
        ),
        claims=tuple(claims),
    )


@dataclass(frozen=True)
class TimingMasks:
    """Authority withheld at the onset of record because it is not timing-stable (§5).

    Identifiers are the hypothesis ids of the revision being resolved. A masked claim is
    never removed and keeps its possible-cause support; only the authority that leaned on an
    unstable relation is not granted, and nothing is upgraded.
    """

    temporal: frozenset[str] = frozenset()
    ended: frozenset[str] = frozenset()
    strong: frozenset[str] = frozenset()
    withheld: tuple[WithheldAuthority, ...] = ()

    def any(self) -> bool:
        return bool(self.temporal or self.ended or self.strong)


_NO_MASKS = TimingMasks()
_MASKS: ContextVar[TimingMasks | None] = ContextVar("timing_masks", default=None)


def current_timing_masks() -> TimingMasks:
    return _MASKS.get() or _NO_MASKS


@contextmanager
def applied_timing_masks(masks: TimingMasks) -> Iterator[None]:
    """Resolve under ``masks``; the resolver reads them wherever it grants temporal authority."""
    token = _MASKS.set(masks)
    try:
        yield
    finally:
        _MASKS.reset(token)


def derive_timing_masks(
    hypotheses: Sequence[Hypothesis], trace: ResolutionTrace, timing: TimingAssessment
) -> TimingMasks:
    """Which authority of the onset-of-record decision the stability outcomes withhold.

    - A temporal or ended-episode *elimination* is negative authority; it stands only when
      that relation is stable over ``O`` (§5.3). A sensitive one leaves the claim unresolved.
    - A *strong* (mechanism-verified) authority relies on the claim's formation, its D1
      support and its execution witness; all three must be stable (§5.2).

    Nothing is derived when the onset was not assessed.
    """
    if not timing.claims:
        return TimingMasks()
    codes: dict[str, set[str]] = defaultdict(set)
    for elimination in trace.eliminations:
        codes[elimination.hypothesis_id].add(elimination.code.value)
    strong_ids = set(trace.mechanism_verified_hypotheses)
    temporal: set[str] = set()
    ended: set[str] = set()
    strong: set[str] = set()
    withheld: list[WithheldAuthority] = []
    for hypothesis in hypotheses:
        claim = timing.claim(hypothesis.hypothesis_key)
        own = codes.get(hypothesis.hypothesis_id, set())
        actor = hypothesis.causal_actor.canonical
        key = hypothesis.hypothesis_key
        if claim is not None:
            for code, relation, authority, bucket in (
                (
                    _TEMPORAL_CONTRADICTION_CODE,
                    RELATION_TEMPORAL_CONTRADICTION,
                    "TEMPORAL_ELIMINATION",
                    temporal,
                ),
                (_ENDED_EPISODE_CODE, RELATION_ENDED_EPISODE, "ENDED_EPISODE_ELIMINATION", ended),
            ):
                if code in own and claim.relation(relation) is TimingStability.SENSITIVE:
                    bucket.add(hypothesis.hypothesis_id)
                    withheld.append(
                        WithheldAuthority(
                            hypothesis_key=key,
                            actor=actor,
                            authority=authority,
                            relations=(relation,),
                        )
                    )
        if hypothesis.hypothesis_id in strong_ids:
            unstable = (
                ["formation"]
                if claim is None or claim.formation is not TimingStability.STABLE
                else []
            ) + [
                relation
                for relation in (RELATION_D1, RELATION_EXECUTION)
                if claim is None or claim.relation(relation) is not TimingStability.STABLE
            ]
            if unstable:
                strong.add(hypothesis.hypothesis_id)
                withheld.append(
                    WithheldAuthority(
                        hypothesis_key=key,
                        actor=actor,
                        authority="STRONG_MECHANISM",
                        relations=tuple(unstable),
                    )
                )
    return TimingMasks(
        temporal=frozenset(temporal),
        ended=frozenset(ended),
        strong=frozenset(strong),
        withheld=tuple(sorted(withheld, key=lambda w: (w.hypothesis_key, w.authority))),
    )


__all__ = [
    "ClaimView",
    "OnsetView",
    "TimingMasks",
    "alert_fingerprint",
    "applied_timing_masks",
    "claim_views",
    "compute_timing_assessment",
    "current_timing_masks",
    "derive_onset_uncertainty",
    "derive_timing_masks",
    "source_alert_episodes",
]
