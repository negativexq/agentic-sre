"""Influence-channel coverage of initiated changes (M21 contract §4; F1, audit only).

For each hypothesis that carries an initiated change, this records the change's
closure, the channel catalog's applicability (structural, evidence-independent,
I12) and, for applicable channels, the observed state. It changes no decision:
the records are attached to the hypothesis audits, outside the epistemic digest.

v1 limits, recorded as gap reasons rather than hidden:
- K: `m21.k-reference-coverage.v1` (§4.5, amendment 5) records K1–K6; it is
  fail-closed by construction (every profile kind INCOMPLETE, no journal-coverage
  fact, no Kubernetes audit evidence), so K is never NO_PATH_COVERED today.
- R/M: a trace read is never proven complete (Tempo is BEST_EFFORT), so no
  applicable runtime channel is covered.
- N: a shared node is UNCOVERED (OD-A1).
- C: never covered in v1; applicable also for a preempting closure Pod or a
  control-plane identity access to the closure (§4.5.4).
- O: alert provenance is undeclared, so O is UNKNOWN when applicable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from packages.rca.k_coverage import RULE_ID as K_RULE_ID
from packages.rca.k_coverage import RULE_VERSION as K_RULE_VERSION
from packages.rca.k_coverage import ApiAuditEvidence, JournalCoverageFact, evaluate_k
from packages.rca.model import (
    ChannelApplicability,
    ChannelAssessment,
    ChannelEvaluation,
    ChannelState,
    ClusterEvent,
    EntityRef,
    FindingKind,
    Hypothesis,
    ObjectVersion,
    ResolutionTrace,
    RootSupportStatus,
    TraceSpanObservation,
)
from packages.rca.runtime_graph import RuntimeGraph
from packages.rca.temporal import causal_time
from packages.rca.topology import Topology

# Edges are stored child→parent and consumer→config; a closure follows them backwards.
_CLOSURE_OUTGOING = ("spawns", "scales")
_CLOSURE_INCOMING = ("owned_by", "uses_config")
# Kinds the relation extractor knows; anything else makes the closure incomplete.
_KNOWN_KINDS = frozenset(
    {
        "Namespace",
        "ConfigMap",
        "Secret",
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "ReplicaSet",
        "Job",
        "CronJob",
        "Pod",
        "Service",
        "HorizontalPodAutoscaler",
        "NetworkPolicy",
        "ResourceQuota",
        "LimitRange",
        "Schedule",
        "NetworkChaos",
        "PodChaos",
        "StressChaos",
        "JVMChaos",
        "HTTPChaos",
        "IOChaos",
        "DNSChaos",
        "TimeChaos",
        "KernelChaos",
        "Endpoints",
        "EndpointSlice",
        "ServiceAccount",
        "PersistentVolumeClaim",
    }
)
# m21.channel-catalog.v1 channel C: cluster-scoped or control-plane kinds.
_CONTROL_PLANE_KINDS = frozenset(
    {
        "Namespace",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "PriorityClass",
        "ValidatingWebhookConfiguration",
        "MutatingWebhookConfiguration",
        "StorageClass",
        "Node",
        "PersistentVolume",
        "APIService",
    }
)
_MAX_MEMBERS = 64


def _closure(
    actor: EntityRef,
    topology: Topology,
    latest: Mapping[EntityRef, ObjectVersion],
    extra: Iterable[EntityRef] = (),
) -> set[EntityRef]:
    members = {actor, *extra}
    for ref in list(members):
        if ref.kind == "Namespace":
            members |= {other for other in latest if other.namespace == ref.name}
    frontier = list(members)
    while frontier:
        ref = frontier.pop()
        following = [nxt for rel in _CLOSURE_OUTGOING for nxt in topology.outgoing(ref, rel)]
        following += [nxt for rel in _CLOSURE_INCOMING for nxt in topology.incoming(ref, rel)]
        for nxt in following:
            if nxt not in members:
                members.add(nxt)
                frontier.append(nxt)
    return members


def _service_names(refs: Iterable[EntityRef], topology: Topology) -> set[str]:
    names: set[str] = set()
    for ref in refs:
        if ref.kind in {"Service", "Deployment", "StatefulSet", "DaemonSet"}:
            names.add(ref.name)
        workload = topology.workload_of(ref)
        if workload is not None:
            names.add(workload.name)
    return names


def _evaluate(
    hypothesis: Hypothesis,
    *,
    topology: Topology,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    symptom_entities: set[EntityRef],
    symptom_services: set[str],
    runtime_graph: RuntimeGraph,
    traced_pods: set[str],
    onset: datetime | None,
    grace: timedelta,
    events: Sequence[ClusterEvent],
    journal_coverage: JournalCoverageFact | None,
    api_audit: ApiAuditEvidence | None,
) -> ChannelAssessment:
    latest = {ref: versions[-1] for ref, versions in history.items() if versions}
    actor = hypothesis.causal_actor
    changes = [causal_time(finding) for finding in hypothesis.initiating_findings]
    t_change = min((when for when in changes if when is not None), default=None)
    window_end = onset + grace if onset is not None else None
    # §4.5.3: actual readers, writers and targets expand the closure to a fixed point;
    # the persisted graph is finite and members only grow, so this terminates.
    extra: set[EntityRef] = set()
    while True:
        closure = _closure(actor, topology, latest, extra)
        window_start = t_change
        if t_change is not None:
            previous = [
                version.observed_at
                for ref in closure
                for version in history.get(ref, ())
                if version.observed_at < t_change
            ]
            if previous:
                window_start = max(previous)
        k = evaluate_k(
            closure,
            history=history,
            events=events,
            topology=topology,
            symptom_entities=symptom_entities,
            window_start=window_start,
            window_end=window_end,
            journal_coverage=journal_coverage,
            api_audit=api_audit,
        )
        if not k.expansion - closure:
            break
        extra |= k.expansion
    kinds = {ref.kind for ref in closure}
    pods = sorted((ref for ref in closure if ref.kind == "Pod"), key=lambda ref: ref.canonical)
    complete = actor in latest and kinds <= _KNOWN_KINDS

    runtime_members = bool(pods) or "Service" in kinds
    closure_services = _service_names(closure, topology)
    evaluations: list[ChannelEvaluation] = []

    # K — always applicable (I12); its state follows §4.5.
    evaluations.append(
        ChannelEvaluation(
            channel="K",
            applicability=ChannelApplicability.APPLICABLE,
            applicability_basis="every object has a reference surface",
            state=k.state,
            gap_reason=k.gap_reason,
            evidence_ids=k.evidence_ids,
            rule_id=f"{K_RULE_ID}.{K_RULE_VERSION}",
            preconditions=k.preconditions,
        )
    )

    # R — synchronous runtime.
    if runtime_members:
        edges = [
            edge
            for edge in runtime_graph.strict_edges()
            if edge.client_server_pairs
            and (
                (
                    edge.caller_service in closure_services
                    and edge.callee_service in symptom_services
                )
                or (
                    edge.callee_service in closure_services
                    and edge.caller_service in symptom_services
                )
            )
        ]
        uninstrumented = [pod for pod in pods if pod.name not in traced_pods]
        evaluations.append(
            ChannelEvaluation(
                channel="R",
                applicability=ChannelApplicability.APPLICABLE,
                applicability_basis="closure contains a Pod or Service",
                state=ChannelState.PATH if edges else ChannelState.UNCOVERED,
                gap_reason=None
                if edges
                else (
                    "UNINSTRUMENTED_POD" if uninstrumented else "TRACE_READ_COMPLETENESS_UNPROVEN"
                ),
                evidence_ids=tuple(sorted({e for edge in edges for e in edge.evidence_ids}))[:12],
            )
        )
    else:
        evaluations.append(
            ChannelEvaluation(
                channel="R",
                applicability=ChannelApplicability.NOT_APPLICABLE,
                applicability_basis="closure has no Pod or Service",
            )
        )

    # M — asynchronous messaging.
    if pods:
        edges = [
            edge
            for edge in runtime_graph.edges
            if edge.producer_consumer_pairs
            and (
                (
                    edge.caller_service in closure_services
                    and edge.callee_service in symptom_services
                )
                or (
                    edge.callee_service in closure_services
                    and edge.caller_service in symptom_services
                )
            )
        ]
        evaluations.append(
            ChannelEvaluation(
                channel="M",
                applicability=ChannelApplicability.APPLICABLE,
                applicability_basis="closure contains a Pod",
                state=ChannelState.PATH if edges else ChannelState.UNCOVERED,
                gap_reason=None if edges else "MESSAGING_COVERAGE_UNPROVEN",
                evidence_ids=tuple(sorted({e for edge in edges for e in edge.evidence_ids}))[:12],
            )
        )
    else:
        evaluations.append(
            ChannelEvaluation(
                channel="M",
                applicability=ChannelApplicability.NOT_APPLICABLE,
                applicability_basis="closure has no Pod",
            )
        )

    # N — shared node.
    placements = {
        pod: (latest[pod].body.get("spec") or {}).get("nodeName") for pod in pods if pod in latest
    }
    if pods:
        symptom_nodes = {
            (version.body.get("spec") or {}).get("nodeName")
            for ref, version in latest.items()
            if ref.kind == "Pod" and ref in symptom_entities
        } - {None}
        unknown = [pod for pod in pods if not placements.get(pod)]
        shared = [pod for pod, node in placements.items() if node and node in symptom_nodes]
        if unknown:
            state, reason = ChannelState.UNKNOWN, "POD_PLACEMENT_UNKNOWN"
        elif shared:
            state, reason = ChannelState.UNCOVERED, "SHARED_NODE_WITH_SYMPTOM_POD"
        else:
            state, reason = ChannelState.NO_PATH_COVERED, None
        evaluations.append(
            ChannelEvaluation(
                channel="N",
                applicability=ChannelApplicability.APPLICABLE,
                applicability_basis="closure contains a Pod scheduled in the window",
                state=state,
                gap_reason=reason,
            )
        )
    else:
        evaluations.append(
            ChannelEvaluation(
                channel="N",
                applicability=ChannelApplicability.NOT_APPLICABLE,
                applicability_basis="closure has no Pod",
            )
        )

    # C — cluster control plane (§4.1, plus the §4.5.4 additions).
    control = sorted(kinds & _CONTROL_PLANE_KINDS)
    preemption = _preemption(pods, latest, symptom_entities, topology)
    bases = [f"closure contains {', '.join(control)}"] if control else []
    if preemption is not None:
        bases.append(preemption[1])
    if k.control_plane_access:
        bases.append("control-plane identity accessed the closure")
    if bases:
        missing_fact = (
            not control
            and not k.control_plane_access
            and preemption
            == (
                ChannelState.UNKNOWN,
                "preemption facts missing",
            )
        )
        evaluations.append(
            ChannelEvaluation(
                channel="C",
                applicability=ChannelApplicability.APPLICABLE,
                applicability_basis="; ".join(bases),
                state=ChannelState.UNKNOWN if missing_fact else ChannelState.UNCOVERED,
                gap_reason="C_PREEMPTION_FACT_MISSING"
                if missing_fact
                else "CONTROL_PLANE_NEVER_COVERED_V1",
                evidence_ids=k.control_plane_access[:12],
            )
        )
    else:
        evaluations.append(
            ChannelEvaluation(
                channel="C",
                applicability=ChannelApplicability.NOT_APPLICABLE,
                applicability_basis="closure has no cluster-scoped or control-plane kind, "
                "no preempting Pod and no control-plane identity access",
            )
        )

    # O — observation path.
    traffic = any(finding.kind is FindingKind.TRAFFIC_INCREASE for finding in hypothesis.findings)
    o_applicable = traffic or runtime_members
    evaluations.append(
        ChannelEvaluation(
            channel="O",
            applicability=ChannelApplicability.APPLICABLE
            if o_applicable
            else ChannelApplicability.NOT_APPLICABLE,
            applicability_basis=(
                "candidate is a TRAFFIC_INCREASE"
                if traffic
                else "closure contains a Pod or Service"
                if runtime_members
                else "closure has no Pod or Service and no traffic finding"
            ),
            state=ChannelState.UNKNOWN if o_applicable else None,
            gap_reason="ALERT_PROVENANCE_UNDECLARED" if o_applicable else None,
        )
    )

    outcome, reason = _closure_outcome(evaluations, complete, window_start, window_end)
    ordered = sorted(closure, key=lambda ref: ref.canonical)
    return ChannelAssessment(
        closure_members=tuple(ref.canonical for ref in ordered[:_MAX_MEMBERS]),
        closure_member_count=len(ordered),
        closure_complete=complete,
        window_start=window_start,
        window_end=window_end,
        evaluations=tuple(evaluations),
        closure_outcome=outcome,
        outcome_reason=reason,
    )


def _preemption(
    pods: Sequence[EntityRef],
    latest: Mapping[EntityRef, ObjectVersion],
    symptom_entities: set[EntityRef],
    topology: Topology,
) -> tuple[ChannelState, str] | None:
    """§4.5.4: a closure Pod able to preempt a symptom-side Pod makes C applicable.

    A required but missing priority fact is never NOT_APPLICABLE (§4.2a).
    """
    if not pods:
        return None
    symptom_pods = [
        ref
        for ref in latest
        if ref.kind == "Pod"
        and (ref in symptom_entities or topology.workload_of(ref) in symptom_entities)
    ]

    def priority(ref: EntityRef) -> int | None:
        value = (latest[ref].body.get("spec") or {}).get("priority") if ref in latest else None
        return value if isinstance(value, int) else None

    victims = [priority(ref) for ref in symptom_pods]
    missing = not symptom_pods or None in victims
    for pod in pods:
        spec = (latest[pod].body.get("spec") or {}) if pod in latest else {}
        own, policy = priority(pod), spec.get("preemptionPolicy")
        if own is None or policy is None:
            missing = True
            continue
        if policy != "Never" and any(v is not None and own > v for v in victims):
            return (
                ChannelState.UNCOVERED,
                f"closure Pod {pod.canonical} can preempt a symptom-side Pod",
            )
    return (ChannelState.UNKNOWN, "preemption facts missing") if missing else None


def _closure_outcome(
    evaluations: Sequence[ChannelEvaluation],
    complete: bool,
    window_start: datetime | None,
    window_end: datetime | None,
) -> tuple[RootSupportStatus, str]:
    """The §4.3 table, preceded by the closure (§4.2) and window (§4.4) preconditions."""
    if not complete:
        return RootSupportStatus.INAPPLICABLE, "CLOSURE_INCOMPLETE"
    if window_start is None or window_end is None:
        return RootSupportStatus.INAPPLICABLE, "WINDOW_UNKNOWN"
    applicable = [e for e in evaluations if e.applicability is ChannelApplicability.APPLICABLE]
    if not applicable:
        return RootSupportStatus.INAPPLICABLE, "NO_APPLICABLE_CHANNEL"
    if any(e.state is ChannelState.PATH for e in applicable):
        return RootSupportStatus.NOT_FIRED, "PATH"
    if any(e.state in {ChannelState.UNCOVERED, ChannelState.UNKNOWN} for e in applicable):
        return RootSupportStatus.INAPPLICABLE, "UNCOVERED_OR_UNKNOWN_CHANNEL"
    return RootSupportStatus.FIRED, "ALL_APPLICABLE_NO_PATH_COVERED"


def channel_assessments(
    hypotheses: Sequence[Hypothesis],
    *,
    topology: Topology,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    symptom_entities: set[EntityRef],
    symptom_services: Iterable[str],
    runtime_graph: RuntimeGraph,
    trace_spans: Sequence[TraceSpanObservation],
    onset: datetime | None,
    grace: timedelta,
    events: Sequence[ClusterEvent] = (),
    journal_coverage: JournalCoverageFact | None = None,
    api_audit: ApiAuditEvidence | None = None,
) -> dict[str, ChannelAssessment]:
    """One assessment per hypothesis that carries an initiated change."""
    traced_pods = {
        name for span in trace_spans if (name := span.semantic_attributes.get("k8s.pod.name"))
    }
    services = set(symptom_services) | _service_names(symptom_entities, topology)
    return {
        hypothesis.hypothesis_id: _evaluate(
            hypothesis,
            topology=topology,
            history=history,
            symptom_entities=symptom_entities,
            symptom_services=services,
            runtime_graph=runtime_graph,
            traced_pods=traced_pods,
            onset=onset,
            grace=grace,
            events=events,
            journal_coverage=journal_coverage,
            api_audit=api_audit,
        )
        for hypothesis in hypotheses
        if hypothesis.initiating_findings
    }


def attach_channel_assessments(
    trace: ResolutionTrace, assessments: Mapping[str, ChannelAssessment]
) -> ResolutionTrace:
    """Attach assessments to the audits they belong to; decisions are untouched."""
    audits = tuple(
        audit.model_copy(update={"channel_assessment": assessments.get(audit.hypothesis_id)})
        for audit in trace.hypothesis_audits
    )
    return trace.model_copy(update={"hypothesis_audits": audits})


__all__ = ["attach_channel_assessments", "channel_assessments"]
