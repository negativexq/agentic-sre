"""Shared actor/instance/episode and incident admission semantics (m21.v2).

Admission is positive structural relevance, not proof of mechanism execution.
No score, name heuristic, group member evidence or missing-data elimination is used.
"""

from __future__ import annotations

from packages.rca.model import CausalHop, EntityRef, Finding, FindingKind, Hypothesis
from packages.rca.topology import RELATION_SEMANTICS

_RELATIONS = frozenset(
    relation
    for semantics in RELATION_SEMANTICS.values()
    for enabled, relation in (
        (semantics.forward, semantics.forward_relation),
        (semantics.backward, semantics.backward_relation),
    )
    if enabled
)


def actor_findings(hypothesis: Hypothesis) -> tuple[Finding, ...]:
    return tuple(
        finding
        for finding in hypothesis.findings
        if finding.entity == hypothesis.causal_actor
        and (
            hypothesis.claim_version == "legacy"
            or finding.entity_instance == hypothesis.actor_instance
        )
        and (hypothesis.episode_onset is None or finding.incident_onset == hypothesis.episode_onset)
    )


def symptom_links(hypothesis: Hypothesis) -> tuple[tuple[EntityRef, tuple[CausalHop, ...]], ...]:
    """Validate actual chain endpoints; presentation strings confer no authority."""
    if hypothesis.claim_version != "m21.v2":
        return ()
    links: list[tuple[EntityRef, tuple[CausalHop, ...]]] = []
    for symptom in sorted(hypothesis.symptom_entities, key=lambda item: item.canonical):
        if symptom == hypothesis.causal_actor:
            links.append((symptom, ()))
        for path in hypothesis.causal_paths:
            if (
                path
                and path[0].source == hypothesis.causal_actor
                and path[-1].target == symptom
                and all(
                    hop.relation in _RELATIONS
                    or (
                        hop.relation == "quota_blocks"
                        and any(
                            f.kind is FindingKind.QUOTA_EXCEEDED
                            and f.evidence_ids
                            and f.details.get("rejected")
                            and hop.source == hypothesis.causal_actor
                            and hop.target in f.related
                            for f in actor_findings(hypothesis)
                        )
                    )
                    for hop in path
                )
                and all(
                    left.target == right.source for left, right in zip(path, path[1:], strict=False)
                )
            ):
                links.append((symptom, path))
    return tuple(links)


def admitted(hypothesis: Hypothesis) -> bool:
    return bool(actor_findings(hypothesis) and symptom_links(hypothesis))


def admission_reasons(hypothesis: Hypothesis) -> tuple[str, ...]:
    if hypothesis.claim_version != "m21.v2":
        return ("LEGACY_CLAIM_REQUIRES_REFORMATION",)
    if not actor_findings(hypothesis):
        return ("NO_ACTOR_INSTANCE_EPISODE_OBSERVATION",)
    return (
        ("ACTOR_TO_INCIDENT_MECHANISM",)
        if symptom_links(hypothesis)
        else ("NO_POSITIVE_INCIDENT_LINK",)
    )
