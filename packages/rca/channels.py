"""Influence-channel coverage of initiated changes (M21 contract §4; F1, audit only).

For each hypothesis that carries an initiated change, this records the change's
closure, the channel catalog's applicability (structural, evidence-independent,
I12) and, for applicable channels, the observed state. It changes no decision:
the records are attached to the hypothesis audits, outside the epistemic digest.

v1 limits, recorded as gap reasons rather than hidden:
- K: the per-kind reference-coverage table is not declared yet (owner decision
  pending), so K is PATH or UNCOVERED, never NO_PATH_COVERED.
- R/M: a trace read is never proven complete (Tempo is BEST_EFFORT), so no
  applicable runtime channel is covered.
- N: a shared node is UNCOVERED (OD-A1).
- C: never covered in v1.
- O: alert provenance is undeclared, so O is UNKNOWN when applicable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from packages.rca.model import (
    ChannelApplicability,
    ChannelAssessment,
    ChannelEvaluation,
    ChannelState,
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
) -> set[EntityRef]:
    members = {actor}
    if actor.kind == "Namespace":
        members |= {ref for ref in latest if ref.namespace == actor.name}
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
) -> ChannelAssessment:
    latest = {ref: versions[-1] for ref, versions in history.items() if versions}
    actor = hypothesis.causal_actor
    closure = _closure(actor, topology, latest)
    kinds = {ref.kind for ref in closure}
    pods = sorted((ref for ref in closure if ref.kind == "Pod"), key=lambda ref: ref.canonical)
    complete = actor in latest and kinds <= _KNOWN_KINDS
    changes = [causal_time(finding) for finding in hypothesis.initiating_findings]
    t_change = min((when for when in changes if when is not None), default=None)
    window_end = onset + grace if onset is not None else None
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

    runtime_members = bool(pods) or "Service" in kinds
    closure_services = _service_names(closure, topology)
    evaluations: list[ChannelEvaluation] = []

    # K — always applicable.
    k_path = any(topology.causal_distance(ref, symptom_entities) is not None for ref in closure)
    evaluations.append(
        ChannelEvaluation(
            channel="K",
            applicability=ChannelApplicability.APPLICABLE,
            applicability_basis="every object has a reference surface",
            state=ChannelState.PATH if k_path else ChannelState.UNCOVERED,
            gap_reason=None if k_path else "K_REFERENCE_COVERAGE_TABLE_UNDECLARED",
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

    # C — cluster control plane.
    control = sorted(kinds & _CONTROL_PLANE_KINDS)
    evaluations.append(
        ChannelEvaluation(
            channel="C",
            applicability=ChannelApplicability.APPLICABLE
            if control
            else ChannelApplicability.NOT_APPLICABLE,
            applicability_basis=f"closure contains {', '.join(control)}"
            if control
            else "closure has no cluster-scoped or control-plane kind",
            state=ChannelState.UNCOVERED if control else None,
            gap_reason="CONTROL_PLANE_NEVER_COVERED_V1" if control else None,
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
