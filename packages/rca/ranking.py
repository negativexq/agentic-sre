"""Explainable candidate scoring and deterministic verification."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from packages.rca.model import (
    Candidate,
    CausalHop,
    Confidence,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    PredicateStatus,
    Symptoms,
    VerificationPredicate,
    VerificationTrace,
)
from packages.rca.temporal import causal_time
from packages.rca.topology import Topology

KIND_WEIGHT: dict[FindingKind, float] = {
    FindingKind.CONFIG_CHANGE: 5.0,
    FindingKind.FAULT_INJECTION: 5.0,
    FindingKind.IMAGE_CHANGE: 4.5,
    FindingKind.SPEC_CHANGE: 4.0,
    FindingKind.POLICY_CREATED: 4.0,
    FindingKind.FAULT_SCHEDULE: 3.5,
    FindingKind.DEPENDENCY_ERRORS: 3.0,
    FindingKind.OBJECT_DELETED: 4.5,
    FindingKind.OBJECT_CREATED: 3.0,
    FindingKind.QUOTA_EXCEEDED: 5.0,
    FindingKind.QUOTA_EXHAUSTED: 3.0,
    FindingKind.NETWORK_RESTRICTION: 2.5,
    FindingKind.CONTAINER_FAILURE: 3.0,
    FindingKind.RESOURCE_PRESSURE: 2.5,
    FindingKind.SCALE_CHANGE: 3.0,
    FindingKind.ROLLOUT_RESTART: 1.5,
    FindingKind.FAILURE_EVENT: 1.0,
    FindingKind.AUTOSCALING_FAILURE: 3.5,
    FindingKind.TRAFFIC_INCREASE: 4.0,
}
STRONG_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE,
        FindingKind.FAULT_INJECTION,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SPEC_CHANGE,
        FindingKind.POLICY_CREATED,
        FindingKind.SCALE_CHANGE,
        FindingKind.QUOTA_EXCEEDED,
    }
)


@dataclass(frozen=True)
class RankingConfig:
    """Scoring knobs. Changing them is a tracked configuration change."""

    lookback: timedelta = timedelta(hours=2)
    grace: timedelta = timedelta(minutes=30)
    distance_bonus: tuple[float, ...] = (3.0, 2.5, 2.0, 1.0)
    unlinked_penalty: float = 2.0
    namespace_bonus: float = 1.5
    mention_bonus: float = 2.0
    in_window_bonus: float = 1.0
    extra_finding_weight: float = 0.3
    verify_score: float = 7.0
    symptom_event_factor: float = 0.5
    # A change shortly after alert onset can be part of the onset transition,
    # but a late incident evolution must not be presented as its original cause.
    verification_onset_grace: timedelta = timedelta(minutes=15)


@dataclass
class Context:
    """Incident facts that scoring needs."""

    symptoms: Symptoms
    symptom_entities: set[EntityRef]
    topology: Topology
    window_end: datetime | None = None
    tokens: set[str] = field(default_factory=set)


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def symptom_tokens(
    symptoms: Symptoms, entities: Iterable[EntityRef], topology: Topology
) -> set[str]:
    """Normalized names under which config content could mention a symptom."""
    tokens = {normalize(service) for service in symptoms.services}
    for entity in entities:
        tokens.update(normalize(name) for name in topology.service_names(entity))
    return {token for token in tokens if len(token) >= 3}


def _mentions(finding: Finding, tokens: set[str]) -> list[str]:
    text = normalize(repr(finding.details))
    return sorted(token for token in tokens if token in text)


def _in_window(at: datetime | None, context: Context, config: RankingConfig) -> bool | None:
    onset = context.symptoms.onset
    if at is None or onset is None:
        return None
    end = context.window_end or context.symptoms.last_seen or onset
    return onset - config.lookback <= at <= end + config.grace


def _affected_entities(finding: Finding, topology: Topology) -> set[EntityRef]:
    """Where a finding acts: fault targets and their workloads, or the entity itself."""
    affected = {finding.entity, *finding.related}
    for ref in list(affected):
        workload = topology.workload_of(ref)
        if workload is not None:
            affected.add(workload)
    return affected


def _link(finding: Finding, context: Context, config: RankingConfig) -> tuple[float, list[str]]:
    reasons: list[str] = []
    best: int | None = None
    for ref in _affected_entities(finding, context.topology):
        distance = context.topology.causal_distance(ref, context.symptom_entities)
        if distance is not None and (best is None or distance < best):
            best = distance
    score = 0.0
    if best is not None and best < len(config.distance_bonus):
        # Warnings on or next to an alerting component mostly restate the symptom.
        factor = config.symptom_event_factor if finding.kind is FindingKind.FAILURE_EVENT else 1.0
        score += factor * config.distance_bonus[best]
        reasons.append(f"{best} hop(s) from an alerting component")
    namespaces = {ref.namespace for ref in _affected_entities(finding, context.topology)}
    if namespaces & set(context.symptoms.namespaces):
        score += config.namespace_bonus
        reasons.append("acts in an alerting namespace")
    mentions = _mentions(finding, context.tokens)
    if mentions and finding.kind in STRONG_KINDS:
        score += config.mention_bonus
        reasons.append(f"change mentions {', '.join(mentions[:3])}")
    if not reasons:
        score -= config.unlinked_penalty
        reasons.append("no link to the alerting components")
    return score, reasons


def score_finding(
    finding: Finding, context: Context, config: RankingConfig | None = None
) -> tuple[float, tuple[str, ...]]:
    """Return one finding's existing ranking contribution and reasons.

    Hypothesis aggregation reuses this primitive so it can deduplicate
    evidence before applying the same scoring mechanics.  It deliberately
    does not introduce a hypothesis-specific bonus.
    """
    config = config or RankingConfig()
    value = KIND_WEIGHT[finding.kind]
    if finding.kind is FindingKind.FAILURE_EVENT:
        value += min(1.5, 0.25 * float(finding.details.get("count", 1)) ** 0.5)
    link, reasons = _link(finding, context, config)
    value += link
    window = _in_window(finding.at, context, config)
    if window is True:
        value += config.in_window_bonus
        reasons.append("happened in the incident window")
    elif window is False:
        value -= config.in_window_bonus
        reasons.append("outside the incident window")
    return value, tuple(reasons)


def score_findings(
    findings: Iterable[Finding], context: Context, config: RankingConfig | None = None
) -> list[Candidate]:
    """Group findings per entity and rank entities by their strongest linked finding."""
    config = config or RankingConfig()
    grouped: dict[EntityRef, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.entity, []).append(finding)
    candidates: list[Candidate] = []
    for entity, items in grouped.items():
        scored: list[tuple[float, Finding, list[str]]] = []
        for finding in items:
            value, reasons = score_finding(finding, context, config)
            scored.append((value, finding, list(reasons)))
        scored.sort(key=lambda item: -item[0])
        top_value, _top, top_reasons = scored[0]
        extras: dict[FindingKind, float] = {}
        for value, finding, _ in scored[1:]:
            if finding.kind is not scored[0][1].kind and value > 0:
                extras.setdefault(finding.kind, min(value, top_value))
        total = top_value + config.extra_finding_weight * sum(sorted(extras.values())[-2:])
        reached: set[EntityRef] = set()
        causal_path: tuple[CausalHop, ...] = ()
        for affected in _affected_entities(scored[0][1], context.topology):
            reached.update(context.topology.causal_reachable(affected))
            path = context.topology.causal_path(affected, context.symptom_entities)
            if path is not None and (not causal_path or len(path) < len(causal_path)):
                causal_path = path
        linked = sorted(ref.canonical for ref in context.symptom_entities & reached)[:5]
        candidates.append(
            Candidate(
                entity=entity,
                score=round(total, 3),
                findings=tuple(item[1] for item in scored),
                linked_symptoms=tuple(linked),
                reasons=tuple(dict.fromkeys(top_reasons)),
                causal_path=causal_path,
                causal_explanation=(
                    "PATH" if causal_path else ("DIRECT" if linked else "UNLINKED")
                ),
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.entity.canonical))
    return candidates


def collapse_fault_instances(
    candidates: list[Candidate], topology: Topology, onset: datetime | None = None
) -> list[Candidate]:
    """Keep one experiment per chaos schedule.

    The kept experiment is the latest one that started at or before the onset,
    or the earliest one when all started later. It takes the place of the
    group's best-scoring member.
    """
    parents = {edge.target: edge.source for edge in topology.edges if edge.relation == "spawns"}
    groups: dict[EntityRef, list[Candidate]] = {}
    for candidate in candidates:
        parent = parents.get(candidate.entity)
        if parent is not None:
            groups.setdefault(parent, []).append(candidate)

    def started(candidate: Candidate) -> datetime | None:
        return min((f.at for f in candidate.findings if f.at), default=None)

    chosen: dict[EntityRef, Candidate] = {}
    for parent, members in groups.items():
        timed = [(started(m), m) for m in members if started(m) is not None]
        before = [(t, m) for t, m in timed if onset is None or (t is not None and t <= onset)]
        if before:
            pick = max(before, key=lambda item: (item[0], item[1].score))[1]
        elif timed:
            pick = min(timed, key=lambda item: (item[0], -item[1].score))[1]
        else:
            pick = members[0]
        best_score = max(m.score for m in members)
        chosen[parent] = pick.model_copy(update={"score": best_score})
    result: list[Candidate] = []
    placed: set[EntityRef] = set()
    for candidate in candidates:
        parent = parents.get(candidate.entity)
        if parent is None:
            result.append(candidate)
        elif parent not in placed:
            result.append(chosen[parent])
            placed.add(parent)
    return result


_INITIATING_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE,
        FindingKind.OBJECT_CREATED,
        FindingKind.OBJECT_DELETED,
        FindingKind.SPEC_CHANGE,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SCALE_CHANGE,
        FindingKind.FAULT_INJECTION,
        FindingKind.FAULT_SCHEDULE,
        FindingKind.POLICY_CREATED,
        FindingKind.QUOTA_EXCEEDED,
        FindingKind.NETWORK_RESTRICTION,
        FindingKind.AUTOSCALING_FAILURE,
        FindingKind.TRAFFIC_INCREASE,
    }
)
_SUPPORTING_KINDS = frozenset(
    {
        FindingKind.CONTAINER_FAILURE,
        FindingKind.RESOURCE_PRESSURE,
        FindingKind.DEPENDENCY_ERRORS,
        FindingKind.FAILURE_EVENT,
    }
)
_VERIFIABLE_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE,
        FindingKind.FAULT_INJECTION,
        FindingKind.FAULT_SCHEDULE,
        FindingKind.SPEC_CHANGE,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SCALE_CHANGE,
        FindingKind.OBJECT_DELETED,
        FindingKind.POLICY_CREATED,
        FindingKind.QUOTA_EXCEEDED,
        FindingKind.AUTOSCALING_FAILURE,
        FindingKind.TRAFFIC_INCREASE,
    }
)


def annotate_temporal_roles(
    findings: Iterable[Finding], onset: datetime | None, grace: timedelta
) -> list[Finding]:
    """Attach onset deltas and conservative temporal roles to observations.

    This is deliberately separate from scoring.  A role describes how an
    observation may be used in verification; it does not create or remove a
    candidate.
    """
    annotated: list[Finding] = []
    for finding in findings:
        delta = (
            (finding.at - onset).total_seconds()
            if finding.at is not None and onset is not None
            else None
        )
        role = EvidenceTemporalRole.AMBIGUOUS
        if delta is not None:
            if finding.kind in _INITIATING_KINDS:
                role = (
                    EvidenceTemporalRole.INITIATING
                    if delta <= grace.total_seconds()
                    else EvidenceTemporalRole.CONSEQUENCE
                )
            elif finding.kind in _SUPPORTING_KINDS and delta >= 0:
                role = EvidenceTemporalRole.SUPPORTING
        annotated.append(
            finding.model_copy(
                update={
                    "incident_onset": onset,
                    "onset_delta_seconds": delta,
                    "temporal_role": role,
                }
            )
        )
    return annotated


def _causal_time(finding: Finding) -> datetime | None:
    """Compatibility wrapper for the shared causal-time helper."""
    return causal_time(finding)


def _linked(finding: Finding, context: Context) -> bool:
    depth = 3 if finding.kind in {FindingKind.CONFIG_CHANGE, FindingKind.OBJECT_DELETED} else 2
    return any(
        context.topology.causal_distance(ref, context.symptom_entities, max_depth=depth) is not None
        for ref in _affected_entities(finding, context.topology)
    )


def verification_trace(
    candidate: Candidate,
    context: Context,
    config: RankingConfig | None = None,
    runner_up: Candidate | None = None,
) -> VerificationTrace:
    """Apply explicit deterministic predicates without consulting ground truth."""
    config = config or RankingConfig()
    relevant = [finding for finding in candidate.findings if _linked(finding, context)]
    linked_ids = tuple(evidence for finding in relevant for evidence in finding.evidence_ids)
    initiating: list[Finding] = []
    contradictions: list[Finding] = []
    for finding in relevant:
        when = _causal_time(finding)
        if finding.kind in _INITIATING_KINDS:
            delta = (
                (when - context.symptoms.onset).total_seconds()
                if when is not None and context.symptoms.onset is not None
                else None
            )
            if delta is not None and delta <= config.verification_onset_grace.total_seconds():
                initiating.append(finding)
            elif delta is not None and delta > config.verification_onset_grace.total_seconds():
                contradictions.append(finding)

    predicates: list[VerificationPredicate] = [
        VerificationPredicate(
            name="candidate_linked_to_symptom",
            status=PredicateStatus.PASS if relevant else PredicateStatus.FAIL,
            evidence_ids=linked_ids[:8],
            detail=(
                "candidate reaches an alerting entity through the causal graph"
                if relevant
                else "no bounded causal path reaches an alerting entity"
            ),
        ),
        VerificationPredicate(
            name="initiating_evidence_near_onset",
            status=(
                PredicateStatus.PASS
                if initiating
                else PredicateStatus.FAIL
                if contradictions or context.symptoms.onset is not None
                else PredicateStatus.UNKNOWN
            ),
            evidence_ids=tuple(
                evidence for finding in initiating for evidence in finding.evidence_ids
            )[:8],
            detail=(
                "an initiating observation is at or shortly after symptom onset"
                if initiating
                else "no linked initiating observation is temporally consistent with onset"
            ),
        ),
    ]
    if contradictions:
        predicates.append(
            VerificationPredicate(
                name="late_change_contradiction",
                status=PredicateStatus.FAIL,
                evidence_ids=tuple(
                    evidence for finding in contradictions for evidence in finding.evidence_ids
                )[:8],
                detail=(
                    "linked change was observed after the onset grace and cannot explain "
                    "the original incident onset"
                ),
            )
        )
    margin = runner_up.score if runner_up is not None else None
    score_margin = candidate.score - margin if margin is not None else None
    if runner_up is not None and score_margin is not None and score_margin <= 1.0:
        predicates.append(
            VerificationPredicate(
                name="candidate_dominance",
                status=PredicateStatus.WEAK,
                detail=f"score margin over runner-up is only {score_margin:.3f}",
            )
        )
    elif runner_up is not None:
        predicates.append(
            VerificationPredicate(
                name="candidate_dominance",
                status=PredicateStatus.PASS,
                detail=f"score margin over runner-up is {score_margin:.3f}",
            )
        )

    def temporally_valid(finding: Finding) -> bool:
        when = _causal_time(finding)
        onset = context.symptoms.onset
        return onset is None or (
            when is not None and when <= onset + config.verification_onset_grace
        )

    config_mention_findings = [
        finding
        for finding in candidate.findings
        if finding.kind is FindingKind.CONFIG_CHANGE
        and _mentions(finding, context.tokens)
        and temporally_valid(finding)
    ]
    if config_mention_findings:
        initiating.extend(item for item in config_mention_findings if item not in initiating)
    verified_kind = any(
        finding.kind in _VERIFIABLE_KINDS and _linked(finding, context) for finding in initiating
    )
    config_mention = any(
        finding.kind is FindingKind.CONFIG_CHANGE and _mentions(finding, context.tokens)
        for finding in candidate.findings
    )
    # Configuration mentions can identify a candidate even when the topology
    # is incomplete, but they still need a temporally valid change.
    if not verified_kind and config_mention and config_mention_findings:
        verified_kind = True
    dominance_weak = any(
        predicate.name == "candidate_dominance" and predicate.status is PredicateStatus.WEAK
        for predicate in predicates
    )
    weak_downstream_only = bool(relevant) and all(
        finding.kind
        in {
            FindingKind.CONTAINER_FAILURE,
            FindingKind.RESOURCE_PRESSURE,
            FindingKind.FAILURE_EVENT,
        }
        for finding in relevant
    )
    if weak_downstream_only:
        predicates.append(
            VerificationPredicate(
                name="initiating_signal_required",
                status=PredicateStatus.FAIL,
                evidence_ids=linked_ids[:8],
                detail="downstream failure observations do not establish an initiating cause",
            )
        )
    if verified_kind and (relevant or config_mention) and not contradictions and not dominance_weak:
        decision = Confidence.VERIFIED
        rationale = "linked initiating evidence is temporally consistent with symptom onset"
    elif weak_downstream_only:
        decision = Confidence.UNVERIFIED
        rationale = (
            "only downstream failure observations were found; the initiating cause is unknown"
        )
    elif candidate.score >= config.verify_score and relevant:
        decision = Confidence.LIKELY
        rationale = (
            "strong linked signal, but onset evidence is missing, late, contradictory, or ambiguous"
        )
    else:
        decision = Confidence.UNVERIFIED
        rationale = (
            "best available candidate without sufficient deterministic verification evidence"
        )
    onset_delta = None
    if candidate.findings:
        timed = [
            finding.onset_delta_seconds
            for finding in candidate.findings
            if finding.onset_delta_seconds is not None
        ]
        onset_delta = min(timed, key=abs) if timed else None
    return VerificationTrace(
        candidate=candidate.entity,
        decision=decision,
        predicates=tuple(predicates),
        onset_delta_seconds=onset_delta,
        supporting_evidence=linked_ids[:12],
        contradictory_evidence=tuple(
            evidence for finding in contradictions for evidence in finding.evidence_ids
        )[:12],
        score_margin=score_margin,
        rationale=rationale,
    )


def verify(
    candidate: Candidate, context: Context, config: RankingConfig | None = None
) -> tuple[Confidence, str]:
    """Backward-compatible tuple API for callers that only need the decision."""
    trace = verification_trace(candidate, context, config)
    assert trace.decision is not None
    return trace.decision, trace.rationale


__all__ = [
    "KIND_WEIGHT",
    "Context",
    "RankingConfig",
    "collapse_fault_instances",
    "annotate_temporal_roles",
    "normalize",
    "score_findings",
    "score_finding",
    "symptom_tokens",
    "verify",
    "verification_trace",
]
