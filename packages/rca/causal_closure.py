"""Positive explanation and scoped mechanism execution, separate from D1.

Only a recorded quota rejection proves admission-control execution. Runtime
propagation explains an observed return, never the origin of the remote failure.
A schedule instance explains the experiment instances its controller named by UID: those
are executions of one recurring fault, not independent root-cause alternatives. That claims
nothing about the incident and moves no frontier answer.
Historical mechanism bridges alone intentionally grant no execution authority.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from packages.rca.claims import actor_findings, admitted, symptom_links
from packages.rca.model import (
    CausalExplanation,
    CausalHop,
    CausalWitness,
    ClusterEvent,
    EntityRef,
    Finding,
    FindingKind,
    FrontierAnswer,
    Hypothesis,
    RootSupportRecord,
    RootSupportStatus,
    StructuralAlternative,
    TraceSpanObservation,
)
from packages.rca.runtime_propagation import (
    RuntimeBindingVerificationState,
    RuntimePropagation,
    RuntimePropagationEdge,
)
from packages.rca.service_effect import (
    BASELINE_FROM,
    BASELINE_TO,
    SERVICE_EFFECT,
    call_pairs,
    fault_calls,
    service_effect,
)
from packages.rca.signals import _QUOTA_MESSAGE
from packages.rca.topology import is_chaos_kind

EXECUTION_RULE = "m21.support.observed-quota-rejection"
EXPLANATION_RULE = "m21.explanation.observed-quota-rejection"
SPAWN_EXPLANATION_RULE = "m21.explanation.controller-spawn"
FAULT_EXECUTION_RULE = "m21.support.observed-fault-execution"
EXECUTION_RULES = (EXECUTION_RULE, FAULT_EXECUTION_RULE)
# An execution interval may end this long before the incident began and still be its execution.
EXECUTION_END_GRACE = timedelta(minutes=5)
# Event timestamps have one-second resolution, so an `Applied` this close after the onset is not later.
EVENT_RESOLUTION = timedelta(seconds=1)
_EFFECT_KINDS = frozenset(
    {
        FindingKind.CONTAINER_FAILURE,
        FindingKind.RESOURCE_PRESSURE,
        FindingKind.DEPENDENCY_ERRORS,
        FindingKind.FAILURE_EVENT,
    }
)


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


def _instant(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _fault_executions(holder: Hypothesis, hypotheses: Sequence[Hypothesis]) -> list[Finding]:
    """The injection findings whose witness this holder may carry.

    A Schedule instance carries exactly the experiments its controller named by UID. An
    experiment carries its own execution, unless an admitted Schedule claim is its parent.
    """
    if holder.causal_actor.kind == "Schedule":
        instance = holder.actor_instance
        return [
            finding
            for child in hypotheses
            if instance is not None
            and child.hypothesis_id != holder.hypothesis_id
            and is_chaos_kind(child.causal_actor.kind)
            and child.causal_actor.kind != "Schedule"
            and child.actor_instance is not None
            and _same_episode(holder, child)
            for finding in actor_findings(child)
            if finding.kind is FindingKind.FAULT_INJECTION
            and finding.details.get("schedule") == holder.causal_actor.canonical
            and finding.details.get("schedule_uid") == instance.uid
            and finding.details.get("spawn_evidence_ids")
        ]
    parents = {
        h.actor_instance.uid
        for h in hypotheses
        if h.causal_actor.kind == "Schedule" and admitted(h) and h.actor_instance is not None
    }
    return [
        finding
        for finding in actor_findings(holder)
        if finding.kind is FindingKind.FAULT_INJECTION
        and finding.details.get("schedule_uid") not in parents
    ]


def _effect_times(finding: Finding, events: Sequence[ClusterEvent]) -> list[datetime]:
    """Every instant this observation covers, not just its latest one.

    Repeated events of one reason merge into a single finding stamped with the latest, so
    the raw events behind its evidence ids give the earlier occurrences.
    """
    ids = set(finding.evidence_ids)
    times = [
        t for e in events if e.evidence_id in ids for t in (e.first_at, e.last_at) if t is not None
    ]
    return [*times, *([finding.at] if finding.at is not None else [])]


def _service_effect_witness(
    holder: Hypothesis,
    injection: Finding,
    target_pod: EntityRef,
    applied: datetime,
    recovered: datetime,
    spans: Sequence[TraceSpanObservation],
) -> list[CausalWitness]:
    """m21 contract §12: the target is not itself a symptom, but a declared symptom service's calls to the exact
    target pod show the effect (non-success, or latency far above the baseline). The witness covers that service."""
    assert holder.episode_onset is not None
    onset = holder.episode_onset
    target_service = target_pod.name.rsplit("-", 2)[0]
    witnesses = []
    for symptom in holder.symptom_entities:
        if symptom.kind != "Service" or symptom.name == target_service:
            continue
        if symptom.namespace != target_pod.namespace:
            continue
        pairs = call_pairs(spans, symptom.name, target_service)
        holds = service_effect(
            pairs,
            target_pod=target_pod.name,
            start=applied,
            end=recovered,
            baseline=(onset - BASELINE_FROM, onset - BASELINE_TO),
            parameters=SERVICE_EFFECT,
        )
        if holds is not True:
            continue
        calls = fault_calls(pairs, target_pod=target_pod.name, start=applied, end=recovered)
        spawned = tuple(injection.details.get("spawn_evidence_ids", ()))
        witnesses.append(
            CausalWitness(
                actor=holder.causal_actor,
                actor_instance=holder.actor_instance,
                origin=injection,
                mechanism="FAULT_EXECUTION_EFFECT_AT_CALLER",
                attribution=(
                    "EXPERIMENT_EXECUTION_CARRIED_BY_SPAWN_RECORD"
                    if spawned
                    else "EXPERIMENT_EXECUTION"
                ),
                symptom=symptom,
                path=(
                    CausalHop(
                        source=holder.causal_actor, relation="fault_targets", target=target_pod
                    ),
                    CausalHop(source=target_pod, relation="called_by", target=symptom),
                ),
                onset=onset,
                evidence_ids=tuple(
                    sorted({*injection.evidence_ids, *(e for c in calls for e in c.evidence_ids)})
                ),
                relation_evidence_ids=spawned,
                coverage=(
                    "EXACT_EXPERIMENT_INSTANCE",
                    "EXECUTION_INTERVAL_CLOSED",
                    "SERVICE_LEVEL_EFFECT_AT_EXACT_POD",
                    "BASELINE_BEFORE_ONSET",
                    *(("EXACT_SPAWN_RECORD_UIDS",) if spawned else ()),
                ),
                missing=("FAULT_ACTION_NOT_OBSERVED", "INCIDENT_RECOVERY_NOT_ASSESSED"),
                rule_id=FAULT_EXECUTION_RULE,
                rule_version="v1",
                claim_level="OBSERVED_MECHANISM_CAUSE",
            )
        )
    return witnesses


def fault_execution(
    holder: Hypothesis,
    possible: RootSupportRecord,
    hypotheses: Sequence[Hypothesis],
    events: Sequence[ClusterEvent],
    spans: Sequence[TraceSpanObservation] = (),
) -> RootSupportRecord:
    """Execution witness plus incident effect at the exact target, never Spawned/Applied alone."""
    witnesses: list[CausalWitness] = []
    if (
        possible.status is RootSupportStatus.FIRED
        and holder.actor_instance is not None
        and holder.episode_onset is not None
        and is_chaos_kind(holder.causal_actor.kind)
    ):
        pods = {
            (h.causal_actor.namespace, h.causal_actor.name): h
            for h in hypotheses
            if h.causal_actor.kind == "Pod"
            and h.actor_instance is not None
            and _same_episode(holder, h)
            and h.causal_actor in holder.symptom_entities
        }
        for injection in _fault_executions(holder, hypotheses):
            for target in injection.details.get("execution_targets", ()):
                namespace, _, rest = str(target["target"]).partition("/")
                pod = pods.get((namespace, rest.partition("/")[0]))
                applied, recovered = (
                    _instant(target["applied_at"]),
                    _instant(target["recovered_at"]),
                )
                if applied is None or recovered is None:
                    continue
                onset = holder.episode_onset
                if applied > onset + EVENT_RESOLUTION or recovered < onset - EXECUTION_END_GRACE:
                    continue
                if pod is None:
                    # not itself a symptom: the effect may still show at a symptom service (§12)
                    name = rest.partition("/")[0]
                    if spans and name:
                        witnesses += _service_effect_witness(
                            holder,
                            injection,
                            EntityRef(kind="Pod", name=name, namespace=namespace),
                            applied,
                            recovered,
                            spans,
                        )
                    continue
                effects = [
                    (f, _effect_times(f, events))
                    for f in actor_findings(pod)
                    if f.kind in _EFFECT_KINDS
                ]
                inside = [
                    f for f, times in effects if any(applied <= t <= recovered for t in times)
                ]
                if not inside or any(t < applied for _, times in effects for t in times):
                    continue
                spawned = tuple(injection.details.get("spawn_evidence_ids", ()))
                effect_ids = tuple(sorted({e for f in inside for e in f.evidence_ids}))
                witnesses.append(
                    CausalWitness(
                        actor=holder.causal_actor,
                        actor_instance=holder.actor_instance,
                        origin=injection,
                        mechanism="FAULT_EXECUTION_ON_INCIDENT_SYMPTOM",
                        attribution=(
                            "EXPERIMENT_EXECUTION_CARRIED_BY_SPAWN_RECORD"
                            if spawned
                            else "EXPERIMENT_EXECUTION"
                        ),
                        symptom=pod.causal_actor,
                        path=(
                            CausalHop(
                                source=holder.causal_actor,
                                relation="fault_targets",
                                target=pod.causal_actor,
                            ),
                        ),
                        onset=holder.episode_onset,
                        evidence_ids=tuple(sorted({*injection.evidence_ids, *effect_ids})),
                        relation_evidence_ids=spawned,
                        coverage=(
                            "EXACT_EXPERIMENT_INSTANCE",
                            "EXECUTION_INTERVAL_CLOSED",
                            "EFFECT_INSIDE_INTERVAL",
                            "NO_EFFECT_BEFORE_APPLY",
                            *(("EXACT_SPAWN_RECORD_UIDS",) if spawned else ()),
                        ),
                        missing=("FAULT_ACTION_NOT_OBSERVED", "INCIDENT_RECOVERY_NOT_ASSESSED"),
                        rule_id=FAULT_EXECUTION_RULE,
                        rule_version="v1",
                        claim_level="OBSERVED_MECHANISM_CAUSE",
                    )
                )
    return RootSupportRecord(
        rule_id=FAULT_EXECUTION_RULE,
        rule_version="v1",
        support_kind="OBSERVED_MECHANISM_CAUSE",
        status=RootSupportStatus.FIRED if witnesses else RootSupportStatus.NOT_FIRED,
        reasons=() if witnesses else ("NO_EXECUTION_WITH_INCIDENT_EFFECT_WITNESS",),
        decisive_evidence_ids=tuple(sorted({e for w in witnesses for e in w.evidence_ids})),
        witnesses=tuple(witnesses),
    )


def _same_episode(left: Hypothesis, right: Hypothesis) -> bool:
    return left.episode_onset is not None and left.episode_onset == right.episode_onset


def _spawn_explanations(
    hypotheses: Sequence[Hypothesis],
    supported: set[str],
    excluded_sources: frozenset[str],
) -> list[CausalExplanation]:
    """A supported schedule instance explains the experiments its controller named by UID.

    Witness: the ``Spawned`` controller records that name the experiment instance, kept on
    the experiment's finding. The experiment claim leaves competition only when every one of
    its own local facts is that execution (an execution of one recurring fault is not an
    independent root cause). The schedule itself must be supported: an unresolved schedule
    never retires a supported experiment. Nothing here says the fault caused the incident.
    """
    result: list[CausalExplanation] = []
    for source in hypotheses:
        instance = source.actor_instance
        if (
            source.causal_actor.kind != "Schedule"
            or instance is None
            or source.episode_onset is None
            or source.hypothesis_id not in supported
            or source.hypothesis_id in excluded_sources
            or not admitted(source)
        ):
            continue
        for target in hypotheses:
            if (
                target.hypothesis_id == source.hypothesis_id
                or target.causal_actor.kind == "Schedule"
                or not is_chaos_kind(target.causal_actor.kind)
                or target.actor_instance is None
                or not admitted(target)
                or not _same_episode(source, target)
            ):
                continue
            local = actor_findings(target)
            spawned: list[str] = []
            covered: list[str] = []
            executions = 0
            for finding in local:
                details = finding.details
                if (
                    finding.kind is FindingKind.FAULT_INJECTION
                    and details.get("schedule") == source.causal_actor.canonical
                    and details.get("schedule_uid") == instance.uid
                    and details.get("spawn_evidence_ids")
                ):
                    executions += 1
                    spawned.extend(details["spawn_evidence_ids"])
                    covered.extend(finding.evidence_ids)
            if not executions:
                continue
            all_covered = executions == len(local)
            result.append(
                CausalExplanation(
                    explaining_claim=source.hypothesis_id,
                    explained_claim=target.hypothesis_id,
                    actor=source.causal_actor,
                    actor_instance=instance,
                    manifestation=target.causal_actor,
                    manifestation_instance=target.actor_instance,
                    episode_onset=source.episode_onset,
                    mechanism="CONTROLLER_SPAWNED_EXECUTION",
                    path=(
                        CausalHop(
                            source=source.causal_actor,
                            relation="spawns",
                            target=target.causal_actor,
                        ),
                    ),
                    evidence_ids=tuple(sorted(set(spawned))),
                    explained_evidence_ids=tuple(sorted(set(covered))),
                    coverage=("EXACT_SPAWN_RECORD_UIDS", "ALL_LOCAL_FACTS_CHECKED"),
                    rule_id=SPAWN_EXPLANATION_RULE,
                    consequence="EXPLAINS_CLAIM" if all_covered else "EXPLAINS_OBSERVATION",
                    remaining_uncertainty=(
                        "SPAWN_DOES_NOT_ESTABLISH_INCIDENT_INITIATION",
                        *(() if all_covered else ("OTHER_ACTOR_FACTS_REMAIN",)),
                    ),
                )
            )
    return result


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
    result.extend(_spawn_explanations(hypotheses, supported, excluded_sources))
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
            and r.rule_id != SPAWN_EXPLANATION_RULE
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
