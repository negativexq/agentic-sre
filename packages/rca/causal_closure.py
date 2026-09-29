"""Positive explanation and scoped mechanism execution, separate from D1.

Only a recorded quota rejection proves admission-control execution. Runtime
propagation explains an observed return, never the origin of the remote failure.
Historical mechanism bridges alone intentionally grant no execution authority.
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.rca.claims import actor_findings, admitted, symptom_links
from packages.rca.model import (
    CausalExplanation,
    CausalHop,
    CausalWitness,
    ClusterEvent,
    FrontierAnswer,
    Hypothesis,
    RootSupportRecord,
    RootSupportStatus,
    StructuralAlternative,
)
from packages.rca.runtime_propagation import (
    RuntimeBindingVerificationState,
    RuntimePropagation,
    RuntimePropagationEdge,
)
from packages.rca.signals import _QUOTA_MESSAGE

EXECUTION_RULE = "m21.support.observed-quota-rejection"
EXPLANATION_RULE = "m21.explanation.observed-quota-rejection"


def quota_execution(
    hypothesis: Hypothesis,
    possible: RootSupportRecord,
    events: Sequence[ClusterEvent],
) -> RootSupportRecord:
    """Prove only the rejection actually witnessed at the incident entity.

    A quota-to-service structural path is insufficient. The symptom itself must
    be the rejected event subject. A shared event is one witness, not independent
    confirmation from both the quota Finding and the subject's failure Finding.
    """
    witnesses: list[CausalWitness] = []
    if possible.status is RootSupportStatus.FIRED:
        for origin in actor_findings(hypothesis):
            if (
                origin.kind.value != "QUOTA_EXCEEDED"
                or origin.incident_onset is None
                or not any(w.origin == origin for w in possible.witnesses)
            ):
                continue
            for event in events:
                match = _QUOTA_MESSAGE.search(event.message)
                at = event.first_at or event.last_at
                if (
                    event.type != "Warning"
                    or event.reason != "FailedCreate"
                    or match is None
                    or match.group(1) != hypothesis.causal_actor.name
                    or event.entity.namespace != hypothesis.causal_actor.namespace
                    or event.evidence_id not in origin.evidence_ids
                    or at is None
                    or origin.at != at
                ):
                    continue
                for symptom, path in symptom_links(hypothesis):
                    if symptom != event.entity or not path or path[0].relation != "quota_blocks":
                        continue
                    witnesses.append(
                        CausalWitness(
                            actor=hypothesis.causal_actor,
                            actor_instance=hypothesis.actor_instance,
                            origin=origin,
                            mechanism="QUOTA_ADMISSION_REJECTION",
                            attribution="EXPLICIT_QUOTA_NAMED_IN_REJECTION",
                            symptom=symptom,
                            path=path,
                            onset=origin.incident_onset,
                            evidence_ids=(event.evidence_id,),
                            relation_evidence_ids=(event.evidence_id,),
                            coverage=("EXPLICIT_REJECTED_SUBJECT", "OBSERVED_REJECTION_TIME"),
                            missing=("INCIDENT_RECOVERY_NOT_ASSESSED",),
                            rule_id=EXECUTION_RULE,
                            rule_version="v1",
                            claim_level="OBSERVED_MECHANISM_CAUSE",
                        )
                    )
    return RootSupportRecord(
        rule_id=EXECUTION_RULE,
        rule_version="v1",
        support_kind="OBSERVED_MECHANISM_CAUSE",
        status=RootSupportStatus.FIRED if witnesses else RootSupportStatus.NOT_FIRED,
        reasons=() if witnesses else ("NO_DIRECT_INCIDENT_REJECTION_WITNESS",),
        decisive_evidence_ids=tuple(sorted({e for w in witnesses for e in w.evidence_ids})),
        witnesses=tuple(witnesses),
    )


def _same_episode(left: Hypothesis, right: Hypothesis) -> bool:
    return left.episode_onset is not None and left.episode_onset == right.episode_onset


def explanations(
    hypotheses: Sequence[Hypothesis],
    supported: set[str],
    events: Sequence[ClusterEvent],
    propagation: RuntimePropagation | None,
    excluded_sources: frozenset[str] = frozenset(),
) -> tuple[CausalExplanation, ...]:
    """Explain specific observations; retire a claim only if all its facts are covered."""
    result: list[CausalExplanation] = []
    for source in hypotheses:
        if not admitted(source) or source.hypothesis_id in excluded_sources:
            continue
        for target in hypotheses:
            if source.hypothesis_id == target.hypothesis_id or not _same_episode(source, target):
                continue
            local = actor_findings(target)
            if not local or not admitted(target):
                continue
            for origin in actor_findings(source):
                if origin.kind.value != "QUOTA_EXCEEDED":
                    continue
                covered = []
                for event in events:
                    match = _QUOTA_MESSAGE.search(event.message)
                    if (
                        event.type == "Warning"
                        and event.reason == "FailedCreate"
                        and match is not None
                        and match.group(1) == source.causal_actor.name
                        and event.entity == target.causal_actor
                        and event.entity.namespace == source.causal_actor.namespace
                        and event.evidence_id in origin.evidence_ids
                        and origin.at is not None
                        and (event_at := event.first_at or event.last_at) is not None
                        and event_at >= origin.at
                        and (
                            event.involved_uid
                            == (target.actor_instance.uid if target.actor_instance else None)
                        )
                    ):
                        covered.append(event.evidence_id)
                if not covered:
                    continue
                # Only the specific rejection observations are explained. A restart,
                # unknown-time change, or other error on the same actor survives.
                all_covered = all(
                    f.kind.value == "FAILURE_EVENT"
                    and f.evidence_ids
                    and set(f.evidence_ids).issubset(covered)
                    for f in local
                )
                assert source.episode_onset is not None
                result.append(
                    CausalExplanation(
                        explaining_claim=source.hypothesis_id,
                        explained_claim=target.hypothesis_id,
                        actor=source.causal_actor,
                        actor_instance=source.actor_instance,
                        manifestation=target.causal_actor,
                        manifestation_instance=target.actor_instance,
                        episode_onset=source.episode_onset,
                        mechanism="QUOTA_ADMISSION_REJECTION",
                        path=(
                            CausalHop(
                                source=source.causal_actor,
                                relation="quota_blocks",
                                target=target.causal_actor,
                            ),
                        ),
                        evidence_ids=tuple(sorted(set(covered))),
                        explained_evidence_ids=tuple(sorted(set(covered))),
                        coverage=("EXACT_EVENT_SUBJECT_AND_UID", "ALL_LOCAL_FACTS_CHECKED"),
                        rule_id=EXPLANATION_RULE,
                        consequence="EXPLAINS_CLAIM" if all_covered else "EXPLAINS_OBSERVATION",
                        remaining_uncertainty=(
                            (() if all_covered else ("OTHER_ACTOR_FACTS_REMAIN",))
                            + (
                                ()
                                if source.hypothesis_id in supported
                                else ("OBSERVED_ROLE_DOES_NOT_ESTABLISH_INCIDENT_INITIATION",)
                            )
                        ),
                    )
                )
            if propagation is None or source.hypothesis_id not in supported:
                continue
            for edge in propagation.edges:

                def matches(h: Hypothesis, side: str, edge: RuntimePropagationEdge = edge) -> bool:
                    binding = getattr(edge, f"{side}_binding")
                    if binding is None or binding.namespace != h.causal_actor.namespace:
                        return False
                    kind = h.causal_actor.kind
                    if kind not in {"Pod", "Deployment"}:
                        return False
                    level = "pod" if kind == "Pod" else "deployment"
                    return (
                        getattr(binding, level) == h.causal_actor.name
                        and getattr(edge, f"{side}_{level}_verification")
                        is RuntimeBindingVerificationState.VERIFIED
                        and (
                            kind != "Pod"
                            or (
                                h.actor_instance is not None
                                and h.actor_instance.uid == binding.pod_uid
                            )
                        )
                    )

                if (
                    not matches(source, "source")
                    or not matches(target, "affected")
                    or not edge.evidence_ids
                    or not set(edge.evidence_ids).intersection(
                        e for f in local for e in f.evidence_ids
                    )
                ):
                    continue
                assert source.episode_onset is not None
                # A trace return alone does not explain all other local observations.
                result.append(
                    CausalExplanation(
                        explaining_claim=source.hypothesis_id,
                        explained_claim=target.hypothesis_id,
                        actor=source.causal_actor,
                        actor_instance=source.actor_instance,
                        manifestation=target.causal_actor,
                        manifestation_instance=target.actor_instance,
                        episode_onset=source.episode_onset,
                        mechanism="OBSERVED_NON_SUCCESS_RETURN",
                        path=(
                            CausalHop(
                                source=source.causal_actor,
                                relation="runtime_propagates",
                                target=target.causal_actor,
                            ),
                        ),
                        evidence_ids=edge.evidence_ids,
                        explained_evidence_ids=edge.evidence_ids,
                        coverage=("PAIRED_RUNTIME_RETURN", "VERIFIED_ENDPOINTS"),
                        rule_id="m21.explanation.runtime-return",
                        remaining_uncertainty=("REMOTE_FAILURE_ORIGIN", "OTHER_ACTOR_FACTS_REMAIN"),
                    )
                )
    # Edges in cycles never remove claims. The raw observations remain auditable.
    adjacency: dict[str, set[str]] = {}
    for item in result:
        adjacency.setdefault(item.explaining_claim, set()).add(item.explained_claim)

    def reaches(start: str, end: str) -> bool:
        pending, seen = [start], set()
        while pending:
            node = pending.pop()
            if node == end:
                return True
            if node not in seen:
                seen.add(node)
                pending.extend(adjacency.get(node, ()))
        return False

    return tuple(
        sorted(
            (
                item.model_copy(
                    update={
                        "consequence": "EXPLAINS_OBSERVATION",
                        "remaining_uncertainty": (*item.remaining_uncertainty, "EXPLANATION_CYCLE"),
                    }
                )
                if reaches(item.explained_claim, item.explaining_claim)
                else item
                for item in result
            ),
            key=lambda item: (
                item.explaining_claim,
                item.explained_claim,
                item.rule_id,
                item.evidence_ids,
            ),
        )
    )


def answer_frontier(
    alternatives: Sequence[StructuralAlternative],
    relations: Sequence[CausalExplanation],
    contradicted: set[str],
) -> tuple[FrontierAnswer, ...]:
    """Transfer a positively observed role; absence and query status cannot answer it."""
    answers = []
    for alternative in alternatives:
        relevant = [
            r
            for r in relations
            if r.actor == alternative.actor
            and r.explained_claim in alternative.material_for_hypothesis_ids
        ]
        valid = [
            r
            for r in relevant
            if r.explaining_claim not in contradicted
            and "EXPLANATION_CYCLE" not in r.remaining_uncertainty
        ]
        covered_claims = {r.explained_claim for r in valid}
        complete = bool(valid) and set(alternative.material_for_hypothesis_ids).issubset(
            covered_claims
        )
        answers.append(
            FrontierAnswer(
                alternative_id=alternative.alternative_id,
                question="WHAT_OBSERVED_UPSTREAM_ROLE_AFFECTS_THIS_CLAIM",
                state="ANSWERED_ROLE_TRANSFERRED" if complete else "OPEN",
                evidence_ids=tuple(sorted({e for r in valid for e in r.evidence_ids})),
                affected_claims=tuple(sorted(covered_claims)),
                transferred_claims=tuple(sorted({r.explaining_claim for r in valid})),
                remaining_uncertainty=("TRANSFERRED_CLAIM_STILL_REQUIRES_ADJUDICATION",)
                if complete
                else ("UPSTREAM_ROLE_PARTIALLY_DETERMINED",)
                if valid
                else ("UPSTREAM_ROLE_UNDETERMINED",),
            )
        )
    return tuple(answers)
