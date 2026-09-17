"""Explainable candidate scoring and deterministic verification."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from packages.rca.model import Candidate, Confidence, EntityRef, Finding, FindingKind, Symptoms
from packages.rca.topology import Topology

KIND_WEIGHT: dict[FindingKind, float] = {
    FindingKind.CONFIG_CHANGE: 5.0,
    FindingKind.FAULT_INJECTION: 5.0,
    FindingKind.IMAGE_CHANGE: 4.5,
    FindingKind.SPEC_CHANGE: 4.0,
    FindingKind.POLICY_CREATED: 4.0,
    FindingKind.FAULT_SCHEDULE: 3.5,
    FindingKind.DEPENDENCY_ERRORS: 3.0,
    FindingKind.QUOTA_EXCEEDED: 5.0,
    FindingKind.QUOTA_EXHAUSTED: 3.0,
    FindingKind.NETWORK_RESTRICTION: 2.5,
    FindingKind.CONTAINER_FAILURE: 3.0,
    FindingKind.RESOURCE_PRESSURE: 2.5,
    FindingKind.SCALE_CHANGE: 3.0,
    FindingKind.ROLLOUT_RESTART: 1.5,
    FindingKind.FAILURE_EVENT: 1.0,
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
        distance = context.topology.distance(ref, context.symptom_entities)
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
            scored.append((value, finding, reasons))
        scored.sort(key=lambda item: -item[0])
        top_value, _top, top_reasons = scored[0]
        extras: dict[FindingKind, float] = {}
        for value, finding, _ in scored[1:]:
            if finding.kind is not scored[0][1].kind and value > 0:
                extras.setdefault(finding.kind, min(value, top_value))
        total = top_value + config.extra_finding_weight * sum(sorted(extras.values())[-2:])
        reached: set[EntityRef] = set()
        for affected in _affected_entities(scored[0][1], context.topology):
            reached.update(context.topology.reachable(affected))
        linked = sorted(ref.canonical for ref in context.symptom_entities & reached)[:5]
        candidates.append(
            Candidate(
                entity=entity,
                score=round(total, 3),
                findings=tuple(item[1] for item in scored),
                linked_symptoms=tuple(linked),
                reasons=tuple(dict.fromkeys(top_reasons)),
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


def verify(
    candidate: Candidate, context: Context, config: RankingConfig | None = None
) -> tuple[Confidence, str]:
    """Apply rules that tie a candidate's finding to the symptoms."""
    config = config or RankingConfig()
    for finding in candidate.findings:
        window = _in_window(finding.at, context, config)
        # Configuration may reach an alerting component through the workload that
        # uses it and one service call: config - workload - service - caller.
        depth = 3 if finding.kind is FindingKind.CONFIG_CHANGE else 2
        linked = any(
            context.topology.distance(ref, context.symptom_entities, max_depth=depth) is not None
            for ref in _affected_entities(finding, context.topology)
        )
        mentions = _mentions(finding, context.tokens)
        if (
            finding.kind is FindingKind.CONFIG_CHANGE
            and window is not False
            and (linked or mentions)
        ):
            return (
                Confidence.VERIFIED,
                "configuration changed near onset and is used by or names an alerting component",
            )
        if finding.kind is FindingKind.FAULT_INJECTION and linked:
            return Confidence.VERIFIED, "fault injection targets an alerting component"
        if (
            finding.kind
            in {FindingKind.SPEC_CHANGE, FindingKind.IMAGE_CHANGE, FindingKind.SCALE_CHANGE}
            and window is True
            and linked
        ):
            return Confidence.VERIFIED, "workload changed in the incident window next to the alerts"
        if finding.kind is FindingKind.POLICY_CREATED and linked:
            return Confidence.VERIFIED, "restrictive policy applies to alerting pods"
        if finding.kind is FindingKind.QUOTA_EXCEEDED and linked:
            return Confidence.VERIFIED, "quota rejected pods of an alerting workload"
    if candidate.score >= config.verify_score and candidate.linked_symptoms:
        return Confidence.LIKELY, "strong signal with a structural link, no verifying rule"
    return Confidence.UNVERIFIED, "best available candidate without verifying evidence"


__all__ = [
    "KIND_WEIGHT",
    "Context",
    "RankingConfig",
    "collapse_fault_instances",
    "normalize",
    "score_findings",
    "symptom_tokens",
    "verify",
]
