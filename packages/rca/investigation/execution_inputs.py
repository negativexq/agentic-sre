"""Planning input goals from existing execution rules; no decision authority."""

from __future__ import annotations

from collections.abc import Collection
from hashlib import sha256

from packages.rca.causal_closure import (
    EXECUTION_RULE,
    FAILED_POD_REASONS,
    FAILED_ROLLOUT_RULE,
    OLD_REVISION_REMOVAL,
    ROLLOUT_EXECUTION_RULE,
    ROLLOUT_ORIGIN_KINDS,
    ROLLOUT_REPLICASET_WINDOW,
    _owner_uid,
)
from packages.rca.claims import actor_findings
from packages.rca.engine import Case
from packages.rca.model import (
    AuthorizedQuery,
    Diagnosis,
    EntityRef,
    FindingKind,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InvestigationEvidenceTarget,
    Lifecycle,
    RootSupportStatus,
)
from packages.rca.resolution import CHANGE_ONSET_PATH_RULE
from packages.rca.service_effect import SERVICE_EFFECT, baseline_before, call_pairs
from packages.rca.signals import _QUOTA_MESSAGE

REASONS = {
    ROLLOUT_EXECUTION_RULE: "NO_ROLLOUT_EXECUTION_WITH_INCIDENT_EFFECT_WITNESS",
    FAILED_ROLLOUT_RULE: "NO_FAILED_ROLLOUT_WITH_INCIDENT_EFFECT_WITNESS",
    EXECUTION_RULE: "NO_DIRECT_INCIDENT_REJECTION_WITNESS",
}


def project_execution_input_gaps(
    case: Case, diagnosis: Diagnosis, capabilities: Collection[str]
) -> Diagnosis:
    """Refresh scoped goals from visible inputs, without reading the source."""
    original = tuple(
        gap for gap in diagnosis.information_gaps if gap.execution_input_target is None
    )
    trace = diagnosis.resolution_trace
    if trace is None or case.inputs is None:
        return diagnosis.model_copy(update={"information_gaps": original})
    hypotheses = {
        hypothesis.hypothesis_id: hypothesis
        for hypothesis in (
            *((diagnosis.hypothesis,) if diagnosis.hypothesis else ()),
            *diagnosis.alternative_hypotheses,
            *diagnosis.ambiguous_hypotheses,
        )
    }
    goals: dict[str, InformationGap] = {}
    for audit in trace.hypothesis_audits:
        if audit.hypothesis_id in {*trace.explained_hypotheses, *trace.eliminated_hypotheses}:
            continue
        hypothesis = hypotheses.get(audit.hypothesis_id)
        if (
            hypothesis is None
            or hypothesis.episode_onset is None
            or not audit.plausible
            or not hypothesis.causal_actor.namespace
        ):
            continue
        if any(
            support.status is RootSupportStatus.FIRED
            and support.support_kind == "OBSERVED_MECHANISM_CAUSE"
            for support in audit.root_support
        ):
            continue
        onset = hypothesis.episode_onset
        if onset.utcoffset() is None:
            continue
        for support in audit.root_support:
            reason = REASONS.get(support.rule_id)
            if (
                support.status is not RootSupportStatus.NOT_FIRED
                or support.rule_version != "v1"
                or reason is None
                or reason not in support.reasons
            ):
                continue
            possible_origins = {
                witness.origin.evidence_ids
                for possible in audit.root_support
                if possible.rule_id == CHANGE_ONSET_PATH_RULE[0]
                and possible.rule_version == CHANGE_ONSET_PATH_RULE[1]
                and possible.status is RootSupportStatus.FIRED
                for witness in possible.witnesses
            }
            origins = [
                finding
                for finding in actor_findings(hypothesis)
                if finding.at is not None
                and finding.at.utcoffset() is not None
                and finding.incident_onset is not None
                and finding.evidence_ids in possible_origins
                and finding.at <= onset
                and (
                    finding.kind in ROLLOUT_ORIGIN_KINDS
                    if support.rule_id != EXECUTION_RULE
                    else finding.kind is FindingKind.QUOTA_EXCEEDED
                )
            ]
            if not origins:
                continue
            origin = min(origins, key=lambda finding: finding.at or onset)
            assert origin.at is not None
            # A target is scope, not evidence of ownership or execution.
            members = set(hypothesis.members)
            targets: list[tuple[str, EntityRef, str, tuple[str, ...]]] = []
            if (
                support.rule_id == EXECUTION_RULE
                and hypothesis.causal_actor.kind == "ResourceQuota"
            ):
                for subject in hypothesis.symptom_entities:
                    if subject.namespace != hypothesis.causal_actor.namespace:
                        continue
                    known = any(
                        event.entity == subject
                        and event.type == "Warning"
                        and event.reason == "FailedCreate"
                        and event.evidence_id in origin.evidence_ids
                        and (event.first_at or event.last_at) == origin.at
                        and (match := _QUOTA_MESSAGE.search(event.message)) is not None
                        and match.group(1) == hypothesis.causal_actor.name
                        for event in case.inputs.events
                    )
                    if not known:
                        targets.append(
                            (
                                "events",
                                subject,
                                "quota_rejection",
                                (
                                    "Warning FailedCreate at the exact incident subject naming the quota",
                                    "origin time and evidence identity agreement; structural path alone is insufficient",
                                ),
                            )
                        )
            elif hypothesis.causal_actor.kind == "Deployment":
                uid = hypothesis.actor_instance.uid if hypothesis.actor_instance else None
                if not uid and not any(
                    version.instance_uid and version.evidence_id in origin.evidence_ids
                    for version in case.inputs.history.get(hypothesis.causal_actor, ())
                ):
                    targets.append(
                        (
                            "history",
                            hypothesis.causal_actor,
                            "actor_uid",
                            ("exact Deployment UID associated with the initiating change",),
                        )
                    )
                rs_uids = {
                    version.instance_uid
                    for ref, versions in case.inputs.history.items()
                    if ref.kind == "ReplicaSet"
                    and ref.namespace == hypothesis.causal_actor.namespace
                    for version in versions
                    if uid
                    and _owner_uid(version) == uid
                    and version.lifecycle is Lifecycle.CREATED
                    and abs(version.observed_at - origin.at) <= ROLLOUT_REPLICASET_WINDOW
                    and version.instance_uid
                }
                all_owned_rs_uids = {
                    version.instance_uid
                    for ref, versions in case.inputs.history.items()
                    if ref.kind == "ReplicaSet"
                    and ref.namespace == hypothesis.causal_actor.namespace
                    for version in versions
                    if uid and _owner_uid(version) == uid and version.instance_uid
                }
                pods = []
                for member in sorted(members, key=lambda item: item.canonical):
                    if member.namespace != hypothesis.causal_actor.namespace:
                        continue
                    versions = case.inputs.history.get(member, ())
                    if (
                        support.rule_id == FAILED_ROLLOUT_RULE
                        and member.kind == "Pod"
                        and any(
                            _owner_uid(version) in all_owned_rs_uids - rs_uids
                            for version in versions
                        )
                    ):
                        if not any(
                            version.lifecycle is Lifecycle.DELETED
                            and origin.at <= version.observed_at <= origin.at + OLD_REVISION_REMOVAL
                            for version in versions
                        ):
                            targets.append(
                                (
                                    "history",
                                    member,
                                    "old_revision_removal",
                                    (
                                        "DELETED old Pod record owned by another ReplicaSet of the exact Deployment UID",
                                        "removal time inside the existing old-revision-removal window",
                                    ),
                                )
                            )
                        continue
                    if member.kind in {"ReplicaSet", "Pod"}:
                        parent_uids = (
                            ({uid} if uid else set()) if member.kind == "ReplicaSet" else rs_uids
                        )
                        created = next(
                            (
                                version
                                for version in versions
                                if version.lifecycle is Lifecycle.CREATED
                                and version.instance_uid
                                and _owner_uid(version) in parent_uids
                                and version.observed_at <= onset
                                and (
                                    member.kind != "ReplicaSet"
                                    or abs(version.observed_at - origin.at)
                                    <= ROLLOUT_REPLICASET_WINDOW
                                )
                            ),
                            None,
                        )
                        if created is None:
                            targets.append(
                                (
                                    "history",
                                    member,
                                    "instance_creation",
                                    (
                                        "CREATED record with exact instance UID and controller owner UID",
                                        "ReplicaSet-to-Deployment and Pod-to-ReplicaSet UID chain and origin timing",
                                    ),
                                )
                            )
                        elif member.kind == "Pod":
                            pods.append(created)
                            if support.rule_id == FAILED_ROLLOUT_RULE and not any(
                                event.entity == member
                                and event.involved_uid == created.instance_uid
                                and event.type == "Warning"
                                and event.reason in FAILED_POD_REASONS
                                for event in case.inputs.events
                            ):
                                targets.append(
                                    (
                                        "events",
                                        member,
                                        "failed_pod_event",
                                        (
                                            "failed Pod Warning with exact involved UID and recorded time",
                                            "old revision removal and caller effect remain separate requirements",
                                        ),
                                    )
                                )
                if support.rule_id == ROLLOUT_EXECUTION_RULE:
                    spans = tuple(case.inputs.trace_index.spans.values())
                    for pod in pods:
                        service = pod.entity.name.rsplit("-", 2)[0]
                        baseline = baseline_before(origin.at)
                        for symptom in hypothesis.symptom_entities:
                            if (
                                symptom.kind != "Service"
                                or symptom.namespace != pod.entity.namespace
                            ):
                                continue
                            pairs = call_pairs(spans, symptom.name, service)
                            fault = [
                                pair
                                for pair in pairs
                                if pair.target_pod == pod.entity.name
                                and pod.observed_at <= pair.started <= onset
                            ]
                            calm = [
                                pair for pair in pairs if baseline[0] <= pair.started <= baseline[1]
                            ]
                            if (
                                len(fault) < SERVICE_EFFECT.calls
                                or len(calm) < SERVICE_EFFECT.calls
                            ):
                                targets.append(
                                    (
                                        "runtime_traces",
                                        hypothesis.causal_actor,
                                        "service_effect_pairs",
                                        (
                                            f"direct caller CLIENT/callee SERVER pairs at {pod.entity.canonical}@{pod.instance_uid}",
                                            f"caller scope: {symptom.canonical}",
                                            "explicit Deployment span binding permits retrieval of old/new revisions for baseline comparison",
                                            "existing service-effect call count and pre-change baseline window",
                                            "effect, ownership and complete symptom coverage remain assessed by existing rules",
                                        ),
                                    )
                                )
            for capability, target, slot, required in targets:
                # Planned windows are constrained again against each physical candidate.
                identity = f"{hypothesis.hypothesis_id}|{support.rule_id}|{slot}|{target.canonical}|{required}"
                gap_id = "execution-input:" + sha256(identity.encode()).hexdigest()[:24]
                contract = InvestigationEvidenceTarget(
                    contract_id=f"investigation.execution-input.{support.rule_id}.{slot}.v1",
                    source_gap_ids=(gap_id,),
                    source_hypothesis_ids=(hypothesis.hypothesis_id,),
                    source_rule_reason=reason,
                    input_scope="RULE_INPUT",
                    acquisition_class="PARTIAL",
                    required_start=baseline_before(origin.at)[0]
                    if capability == "runtime_traces"
                    else origin.at,
                    required_end=onset,
                    kind="RELATION" if capability == "runtime_traces" else "OBSERVATION",
                    fact_family=slot,
                    required_observations=required,
                    consumer_ids=(support.rule_id,),
                )
                goals[gap_id] = InformationGap(
                    gap_id=gap_id,
                    dimension=GapDimension.FAILURE_ONSET,
                    hypothesis_ids=(hypothesis.hypothesis_id,),
                    entity_scope=(target,),
                    missing_fact=f"{support.rule_id}: {slot}",
                    required_relation="existing execution/effect rule input",
                    evidence_refs=origin.evidence_ids,
                    discriminating_outcomes=(
                        GapOutcome(
                            kind=GapOutcomeKind.SUPPORTS,
                            hypothesis_ids=(hypothesis.hypothesis_id,),
                            condition="positive scoped input observation",
                            implication="input acquired; complete witness remains separately assessed",
                        ),
                        GapOutcome(
                            kind=GapOutcomeKind.NO_DATA,
                            hypothesis_ids=(hypothesis.hypothesis_id,),
                            condition="no observation returned",
                            implication="condition remains open; no exclusion",
                        ),
                    ),
                    authorized_queries=(AuthorizedQuery(capability=capability, target=target),),
                    candidate_tools=(capability,),
                    resolvability=GapResolvability.RESOLVABLE
                    if capability in capabilities
                    else GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS,
                    rationale="Read-only projection of a recorded composite rule deficit; partial input route.",
                    execution_input_target=contract,
                )
    return diagnosis.model_copy(
        update={"information_gaps": (*original, *(goals[key] for key in sorted(goals)))}
    )
