"""Deterministic grouping of entity candidates into causal hypotheses."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from packages.rca.model import (
    Candidate,
    CausalHop,
    Diagnosis,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    HypothesisDiagnostics,
)
from packages.rca.ranking import Context, RankingConfig, score_finding
from packages.rca.topology import Topology

_MANIFESTATION_KINDS = frozenset(
    {
        FindingKind.CONTAINER_FAILURE,
        FindingKind.RESOURCE_PRESSURE,
        FindingKind.DEPENDENCY_ERRORS,
        FindingKind.FAILURE_EVENT,
    }
)
_INITIATING_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE,
        FindingKind.OBJECT_CREATED,
        FindingKind.OBJECT_DELETED,
        FindingKind.SPEC_CHANGE,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SCALE_CHANGE,
        FindingKind.ROLLOUT_RESTART,
        FindingKind.FAULT_INJECTION,
        FindingKind.FAULT_SCHEDULE,
        FindingKind.POLICY_CREATED,
        FindingKind.QUOTA_EXCEEDED,
        FindingKind.QUOTA_EXHAUSTED,
        FindingKind.NETWORK_RESTRICTION,
        FindingKind.AUTOSCALING_FAILURE,
        FindingKind.TRAFFIC_INCREASE,
    }
)


@dataclass(frozen=True)
class GroupingResult:
    """Hypotheses plus measurements for one deterministic diagnosis cycle."""

    hypotheses: tuple[Hypothesis, ...]
    diagnostics: HypothesisDiagnostics


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parents = list(range(size))

    def find(self, value: int) -> int:
        parent = self.parents[value]
        if parent != value:
            self.parents[value] = self.find(parent)
        return self.parents[value]

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parents[right_root] = left_root


def _finding_time(finding: Finding) -> datetime | None:
    return finding.at or finding.incident_onset


def _has_initiating_evidence(candidate: Candidate) -> bool:
    return any(
        finding.temporal_role is EvidenceTemporalRole.INITIATING
        and finding.kind in _INITIATING_KINDS
        for finding in candidate.findings
    )


def _has_manifestation_evidence(candidate: Candidate) -> bool:
    return any(
        finding.temporal_role in {EvidenceTemporalRole.SUPPORTING, EvidenceTemporalRole.CONSEQUENCE}
        or finding.kind in _MANIFESTATION_KINDS
        for finding in candidate.findings
    )


def _earliest_initiating(candidate: Candidate) -> str:
    times = [
        finding.at
        for finding in candidate.findings
        if finding.temporal_role is EvidenceTemporalRole.INITIATING and finding.at is not None
    ]
    return min(item.isoformat() for item in times) if times else "9999-12-31T23:59:59+00:00"


def _coherent_pair(
    upstream: Candidate,
    downstream: Candidate,
    path: tuple[CausalHop, ...],
) -> bool:
    """Require causal direction plus an initiating-to-manifestation episode."""
    if not path or not _has_initiating_evidence(upstream):
        return False
    schedule_spawn = (
        any(hop.relation == "spawns" for hop in path)
        and any(finding.kind is FindingKind.FAULT_SCHEDULE for finding in upstream.findings)
        and any(finding.kind is FindingKind.FAULT_INJECTION for finding in downstream.findings)
    )
    if not schedule_spawn and not _has_manifestation_evidence(downstream):
        return False
    upstream_times = [
        finding.at
        for finding in upstream.findings
        if finding.temporal_role is EvidenceTemporalRole.INITIATING and finding.at is not None
    ]
    downstream_times = [
        finding.at
        for finding in downstream.findings
        if finding.at is not None
        and (
            finding.temporal_role
            in {EvidenceTemporalRole.SUPPORTING, EvidenceTemporalRole.CONSEQUENCE}
            or finding.kind in _MANIFESTATION_KINDS
        )
    ]
    if upstream_times and downstream_times and min(downstream_times) < min(upstream_times):
        return False
    return not any(
        finding.temporal_role is EvidenceTemporalRole.CONSEQUENCE
        for finding in upstream.findings
        if finding.kind in _INITIATING_KINDS
    )


def _pair_path(
    left: Candidate, right: Candidate, topology: Topology
) -> tuple[EntityRef, EntityRef, tuple[CausalHop, ...]] | None:
    left_to_right = topology.causal_path(left.entity, {right.entity}, max_depth=6)
    if left_to_right is not None and _coherent_pair(left, right, left_to_right):
        return left.entity, right.entity, left_to_right
    right_to_left = topology.causal_path(right.entity, {left.entity}, max_depth=6)
    if right_to_left is not None and _coherent_pair(right, left, right_to_left):
        return right.entity, left.entity, right_to_left
    return None


def _effective_findings(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Keep all provenance while returning stable findings for presentation."""
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                finding.at.isoformat() if finding.at else "",
                finding.entity.canonical,
                finding.kind.value,
                finding.summary,
            ),
        )
    )


def _distinct_scoring_findings(findings: Sequence[Finding]) -> tuple[tuple[Finding, ...], int]:
    """Return one scoring unit per evidence identity and duplicate count."""
    seen_ids: set[str] = set()
    seen_fallbacks: set[tuple[str, str, str, str]] = set()
    distinct: list[Finding] = []
    duplicate_count = 0
    for finding in findings:
        ids = frozenset(finding.evidence_ids)
        if ids:
            if ids <= seen_ids:
                duplicate_count += 1
                continue
            seen_ids.update(ids)
        else:
            fallback = (
                finding.entity.canonical,
                finding.kind.value,
                finding.at.isoformat() if finding.at else "",
                finding.summary,
            )
            if fallback in seen_fallbacks:
                duplicate_count += 1
                continue
            seen_fallbacks.add(fallback)
        distinct.append(finding)
    return tuple(distinct), duplicate_count


def _aggregate_score(
    findings: Sequence[Finding], context: Context, config: RankingConfig
) -> tuple[float, tuple[str, ...], int]:
    distinct, duplicate_count = _distinct_scoring_findings(findings)
    if not distinct:
        return 0.0, (), duplicate_count
    scored = [(*score_finding(finding, context, config), finding) for finding in distinct]
    scored.sort(key=lambda item: (-item[0], item[2].kind.value, item[2].entity.canonical))
    top_value, _top_reasons, _top_finding = scored[0]
    extras_by_kind: dict[FindingKind, float] = {}
    for value, _reasons, finding in scored[1:]:
        if finding.kind is not _top_finding.kind and value > 0:
            extras_by_kind[finding.kind] = max(extras_by_kind.get(finding.kind, 0.0), value)
    total = top_value + config.extra_finding_weight * sum(sorted(extras_by_kind.values())[-2:])
    reasons = tuple(dict.fromkeys(scored[0][1]))
    return round(total, 3), reasons, duplicate_count


def _actor_candidate(
    candidates: Sequence[Candidate], topology: Topology
) -> tuple[Candidate, tuple[tuple[EntityRef, tuple[CausalHop, ...]], ...]]:
    paths_by_actor: dict[EntityRef, list[tuple[EntityRef, tuple[CausalHop, ...]]]] = {}
    for candidate in candidates:
        for other in candidates:
            if other.entity == candidate.entity:
                continue
            path = topology.causal_path(candidate.entity, {other.entity}, max_depth=6)
            if path is not None:
                paths_by_actor.setdefault(candidate.entity, []).append((other.entity, path))

    def key(candidate: Candidate) -> tuple[int, int, int, str, float, str]:
        schedule_actor = int(
            candidate.entity.kind == "Schedule"
            and any(
                path and path[0].relation == "spawns"
                for _target, path in paths_by_actor.get(candidate.entity, ())
            )
        )
        return (
            schedule_actor,
            len(paths_by_actor.get(candidate.entity, ())),
            int(_has_initiating_evidence(candidate)),
            _earliest_initiating(candidate),
            candidate.score,
            candidate.entity.canonical,
        )

    # Reverse only the numeric dimensions; canonical identity remains the
    # deterministic final tie-break, never causal evidence.
    actor = sorted(
        candidates,
        key=lambda candidate: (
            -key(candidate)[0],
            -key(candidate)[1],
            -key(candidate)[2],
            key(candidate)[3],
            -key(candidate)[4],
            key(candidate)[5],
        ),
    )[0]
    return actor, tuple(paths_by_actor.get(actor.entity, ()))


def _hypothesis_id(
    actor: EntityRef, members: Sequence[EntityRef], findings: Sequence[Finding]
) -> str:
    evidence = sorted({evidence_id for finding in findings for evidence_id in finding.evidence_ids})
    material = "|".join(
        [actor.canonical, *sorted(member.canonical for member in members), *evidence]
    )
    return f"hypothesis:{sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _signature(hypothesis: Hypothesis) -> tuple[object, ...]:
    relations = tuple(sorted({hop.relation for path in hypothesis.causal_paths for hop in path}))
    return (
        tuple(sorted({finding.kind.value for finding in hypothesis.initiating_findings})),
        tuple(sorted({finding.kind.value for finding in hypothesis.supporting_findings})),
        relations,
        hypothesis.causal_explanation,
    )


def _make_hypothesis(
    candidates: Sequence[Candidate], topology: Topology, context: Context, config: RankingConfig
) -> tuple[Hypothesis, int, bool]:
    actor_candidate, member_paths = _actor_candidate(candidates, topology)
    actor = actor_candidate.entity
    members = tuple(
        sorted((candidate.entity for candidate in candidates), key=lambda item: item.canonical)
    )
    actor_findings = list(actor_candidate.findings)
    other_findings = [
        finding
        for candidate in candidates
        if candidate.entity != actor
        for finding in candidate.findings
    ]
    all_findings = tuple(
        actor_findings
        + [
            finding
            for finding in _effective_findings(other_findings)
            if finding not in actor_findings
        ]
    )
    initiating = tuple(
        finding
        for finding in all_findings
        if finding.temporal_role is EvidenceTemporalRole.INITIATING
        and not (
            actor.kind == "Schedule"
            and finding.entity != actor
            and finding.kind is FindingKind.FAULT_INJECTION
        )
    )
    supporting = tuple(
        finding
        for finding in all_findings
        if finding.temporal_role is EvidenceTemporalRole.SUPPORTING
        or (finding.entity != actor and finding.kind in _MANIFESTATION_KINDS)
        or (
            actor.kind == "Schedule"
            and finding.entity != actor
            and finding.kind is FindingKind.FAULT_INJECTION
        )
    )
    contradictory = tuple(
        finding
        for finding in all_findings
        if finding.temporal_role is EvidenceTemporalRole.CONSEQUENCE
        and (finding.entity == actor or finding.kind in _INITIATING_KINDS)
    )
    manifestations = tuple(
        member
        for member in members
        if member != actor
        and any(finding.entity == member for finding in supporting + contradictory)
    )
    paths: list[tuple[CausalHop, ...]] = []
    primary = topology.causal_path(actor, context.symptom_entities, max_depth=6)
    if primary is not None:
        paths.append(primary)
    for member, path in sorted(member_paths, key=lambda item: item[0].canonical):
        if member == actor or not path or path in paths:
            continue
        paths.append(path)
    linked = tuple(
        sorted({symptom for candidate in candidates for symptom in candidate.linked_symptoms})
    )
    explanation = "PATH" if paths else ("DIRECT" if linked else "UNLINKED")
    score, top_reasons, duplicate_count = _aggregate_score(all_findings, context, config)
    grouping_reasons = list(top_reasons)
    if len(members) > 1:
        relations = sorted({hop.relation for path in paths for hop in path})
        grouping_reasons.append(
            f"grouped {len(members)} entities into one causal episode"
            + (f" via {', '.join(relations[:4])}" if relations else "")
        )
    if duplicate_count:
        grouping_reasons.append(f"deduplicated {duplicate_count} repeated evidence reference(s)")
    hypothesis = Hypothesis(
        hypothesis_id=_hypothesis_id(actor, members, all_findings),
        causal_actor=actor,
        members=members,
        manifestations=manifestations,
        findings=all_findings,
        initiating_findings=initiating,
        supporting_findings=supporting,
        contradictory_findings=contradictory,
        causal_paths=tuple(paths),
        linked_symptoms=linked,
        causal_explanation=explanation,
        score=score,
        reasons=tuple(dict.fromkeys(grouping_reasons)),
    )
    ownership = any(hop.relation in {"owns", "managed_by"} for path in paths for hop in path)
    return hypothesis, duplicate_count, ownership


def group_candidates(
    candidates: Sequence[Candidate], topology: Topology, context: Context, config: RankingConfig
) -> GroupingResult:
    """Group only evidence-coherent, directionally connected candidates."""
    if not candidates:
        return GroupingResult(
            hypotheses=(),
            diagnostics=HypothesisDiagnostics(),
        )
    disjoint = _DisjointSet(len(candidates))
    for left_index, left in enumerate(candidates):
        for right_index in range(left_index + 1, len(candidates)):
            if _pair_path(left, candidates[right_index], topology) is not None:
                disjoint.union(left_index, right_index)
    components: dict[int, list[Candidate]] = {}
    for index, candidate in enumerate(candidates):
        components.setdefault(disjoint.find(index), []).append(candidate)
    built = [_make_hypothesis(items, topology, context, config) for items in components.values()]
    hypotheses = tuple(
        sorted(
            (item[0] for item in built),
            key=lambda item: (-item.score, item.causal_actor.canonical, item.hypothesis_id),
        )
    )
    duplicate_count = sum(item[1] for item in built)
    ownership_count = sum(int(item[2] and len(item[0].members) > 1) for item in built)
    ties = sum(
        1
        for left_index, left in enumerate(hypotheses)
        for right in hypotheses[left_index + 1 :]
        if left.score == right.score
    )
    top = hypotheses[:5]
    signatures = [_signature(item) for item in top]
    similar = sum(
        1
        for left_index, signature in enumerate(signatures)
        for right in signatures[left_index + 1 :]
        if signature == right
    )
    diagnostics = HypothesisDiagnostics(
        raw_candidate_count=len(candidates),
        hypothesis_count=len(hypotheses),
        multi_entity_hypotheses=sum(len(item.members) > 1 for item in hypotheses),
        ownership_chains_collapsed=ownership_count,
        duplicate_evidence_ids_removed=duplicate_count,
        exact_score_ties=ties,
        structurally_similar_top_hypotheses=similar,
    )
    return GroupingResult(hypotheses=hypotheses, diagnostics=diagnostics)


def build_hypotheses(
    candidates: Sequence[Candidate], topology: Topology, context: Context, config: RankingConfig
) -> tuple[Hypothesis, ...]:
    """Convenience API returning only deterministically ordered hypotheses."""
    return group_candidates(candidates, topology, context, config).hypotheses


def summarize_diagnostics(diagnostics: Iterable[HypothesisDiagnostics]) -> dict[str, int]:
    """Aggregate grouping measurements without reading benchmark ground truth."""
    items = tuple(diagnostics)
    return {
        "diagnoses": len(items),
        "raw_candidate_count": sum(item.raw_candidate_count for item in items),
        "hypothesis_count": sum(item.hypothesis_count for item in items),
        "multi_entity_hypotheses": sum(item.multi_entity_hypotheses for item in items),
        "ownership_chains_collapsed": sum(item.ownership_chains_collapsed for item in items),
        "duplicate_evidence_ids_removed": sum(
            item.duplicate_evidence_ids_removed for item in items
        ),
        "exact_score_ties": sum(item.exact_score_ties for item in items),
        "structurally_similar_top_hypotheses": sum(
            item.structurally_similar_top_hypotheses for item in items
        ),
    }


def summarize_diagnoses(diagnoses: Iterable[Diagnosis]) -> dict[str, int]:
    """Aggregate stored diagnosis grouping measurements for a diagnostic report."""
    return summarize_diagnostics(
        diagnosis.hypothesis_diagnostics
        for diagnosis in diagnoses
        if diagnosis.hypothesis_diagnostics is not None
    )


def hypothesis_candidate(hypothesis: Hypothesis) -> Candidate:
    """Adapt a grouped hypothesis to the existing verification/remediation APIs."""
    primary_path = hypothesis.causal_paths[0] if hypothesis.causal_paths else ()
    return Candidate(
        entity=hypothesis.causal_actor,
        score=hypothesis.score,
        findings=hypothesis.findings,
        linked_symptoms=hypothesis.linked_symptoms,
        reasons=hypothesis.reasons,
        causal_path=primary_path,
        causal_explanation=hypothesis.causal_explanation,
    )


__all__ = [
    "GroupingResult",
    "build_hypotheses",
    "group_candidates",
    "hypothesis_candidate",
    "summarize_diagnoses",
    "summarize_diagnostics",
]
