"""Deterministic descriptions of evidence missing between hypotheses.

This module describes what an eventual investigator could inspect.  It does
not execute a capability, select a tool dynamically, or call a model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256

from packages.rca.causal_roles import HypothesisCausalRole, HypothesisCausalRoles
from packages.rca.mechanism_bridge import RuntimeMechanismBridges
from packages.rca.model import (
    AuthorizedQuery,
    EntityRef,
    FindingKind,
    FrontierStatus,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    Hypothesis,
    InformationGap,
    InformationGapOrigin,
    Resolution,
    ResolutionTrace,
    StructuralAlternative,
    ToolCapability,
)
from packages.rca.resolution import hypothesis_signature
from packages.rca.root_cause_eligibility import (
    RootCauseEligibilities,
    RootCauseEligibilityState,
)
from packages.rca.runtime_propagation import RuntimePropagation
from packages.rca.signals import ACCESS_KINDS
from packages.rca.source import ObservationSource
from packages.rca.topology import WORKLOAD_KINDS, Topology

CAPABILITIES: tuple[ToolCapability, ...] = (
    ToolCapability(
        name="describe",
        dimensions=(
            GapDimension.ENTITY_STATE,
            GapDimension.CONFIG_DIFFERENCE,
            GapDimension.AUTOSCALING_TARGET_STATE,
        ),
        required_inputs=("entity",),
        evidence_sources=("kubernetes_object_snapshot",),
    ),
    ToolCapability(
        name="history",
        dimensions=(
            GapDimension.CHANGE_TIMING,
            GapDimension.CONFIG_DIFFERENCE,
            GapDimension.FAILURE_ONSET,
        ),
        required_inputs=("entity", "incident_window"),
        evidence_sources=("object_journal", "normalized_findings"),
    ),
    ToolCapability(
        name="events",
        dimensions=(
            GapDimension.EVENT_SEQUENCE,
            GapDimension.FAILURE_ONSET,
            GapDimension.AUTOSCALING_TARGET_STATE,
        ),
        required_inputs=("entity", "incident_window"),
        evidence_sources=("event_journal",),
    ),
    ToolCapability(
        name="incident_events",
        dimensions=(GapDimension.EVENT_SEQUENCE,),
        required_inputs=("namespace", "incident_window"),
        evidence_sources=("event_journal",),
    ),
    ToolCapability(
        name="incident_changes",
        dimensions=(GapDimension.CHANGE_TIMING, GapDimension.CONFIG_DIFFERENCE),
        required_inputs=("namespace", "incident_window"),
        evidence_sources=("object_journal",),
    ),
    ToolCapability(
        name="neighbors",
        dimensions=(
            GapDimension.TOPOLOGY_RELATION,
            GapDimension.DEPENDENCY_HEALTH,
            GapDimension.AUTOSCALING_TARGET_STATE,
        ),
        required_inputs=("entity",),
        evidence_sources=("causal_topology",),
    ),
    ToolCapability(
        name="logs",
        dimensions=(GapDimension.LOG_ERROR_PATTERN, GapDimension.DEPENDENCY_HEALTH),
        required_inputs=("service", "incident_window"),
        evidence_sources=("captured_log_observations",),
    ),
    ToolCapability(
        name="resource_pressure",
        dimensions=(GapDimension.RESOURCE_PRESSURE, GapDimension.METRIC_BASELINE),
        required_inputs=("pod", "incident_window"),
        evidence_sources=("resource_pressure_observations",),
    ),
    ToolCapability(
        name="traffic",
        dimensions=(GapDimension.METRIC_CHANGE, GapDimension.METRIC_BASELINE),
        required_inputs=("entity", "incident_window"),
        evidence_sources=("traffic_observations",),
    ),
    ToolCapability(
        name="runtime_traces",
        dimensions=(GapDimension.FAILURE_ONSET, GapDimension.DEPENDENCY_HEALTH),
        required_inputs=("entity", "incident_window"),
        evidence_sources=("distributed_trace_spans",),
    ),
)


_FINDING_DIMENSIONS: dict[FindingKind, tuple[GapDimension, ...]] = {
    FindingKind.AUTOSCALING_FAILURE: (GapDimension.AUTOSCALING_TARGET_STATE,),
    FindingKind.CONFIG_CHANGE: (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    FindingKind.SPEC_CHANGE: (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    FindingKind.IMAGE_CHANGE: (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    FindingKind.ROLLOUT_RESTART: (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    FindingKind.DEPENDENCY_ERRORS: (GapDimension.DEPENDENCY_HEALTH,),
    FindingKind.FAILURE_EVENT: (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET),
    FindingKind.CONTAINER_FAILURE: (GapDimension.FAILURE_ONSET, GapDimension.EVENT_SEQUENCE),
    FindingKind.RESOURCE_PRESSURE: (GapDimension.RESOURCE_PRESSURE, GapDimension.METRIC_BASELINE),
    FindingKind.TRAFFIC_INCREASE: (GapDimension.METRIC_CHANGE, GapDimension.METRIC_BASELINE),
    FindingKind.NETWORK_RESTRICTION: (GapDimension.TOPOLOGY_RELATION, GapDimension.ENTITY_STATE),
}

_STRUCTURAL_DIMENSIONS: dict[str, tuple[GapDimension, ...]] = {
    "Deployment": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    "StatefulSet": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    "DaemonSet": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    "Job": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    "CronJob": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    "ConfigMap": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    "Secret": (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    "HorizontalPodAutoscaler": (
        GapDimension.AUTOSCALING_TARGET_STATE,
        GapDimension.EVENT_SEQUENCE,
    ),
    "NetworkPolicy": (GapDimension.TOPOLOGY_RELATION, GapDimension.ENTITY_STATE),
    "Pod": (GapDimension.FAILURE_ONSET, GapDimension.EVENT_SEQUENCE),
    "Service": (GapDimension.DEPENDENCY_HEALTH, GapDimension.EVENT_SEQUENCE),
    "StressChaos": (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET),
    "NetworkChaos": (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET),
    "Schedule": (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET),
    "Workflow": (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET),
}

_RELATIONS: dict[GapDimension, str] = {
    GapDimension.AUTOSCALING_TARGET_STATE: "scales",
    GapDimension.CONFIG_DIFFERENCE: "configures",
    GapDimension.DEPENDENCY_HEALTH: "dependency_of",
    GapDimension.TOPOLOGY_RELATION: "causal_path",
}

# These dimensions can be extended by a targeted investigation query when the
# deterministic pass was built from ``InitialObservationView``.  Topology and
# latest object state are intentionally absent: the initial view already
# exposes those facts and re-reading them is not an information gap.
_BOUNDED_EXPANDABLE_DIMENSIONS = frozenset(
    {
        GapDimension.AUTOSCALING_TARGET_STATE,
        GapDimension.CHANGE_TIMING,
        GapDimension.CONFIG_DIFFERENCE,
        GapDimension.DEPENDENCY_HEALTH,
        GapDimension.EVENT_SEQUENCE,
        GapDimension.FAILURE_ONSET,
        GapDimension.LOG_ERROR_PATTERN,
        GapDimension.METRIC_BASELINE,
        GapDimension.METRIC_CHANGE,
        GapDimension.RESOURCE_PRESSURE,
    }
)


def capabilities_for(dimension: GapDimension) -> tuple[ToolCapability, ...]:
    """Return actual bounded read-only capabilities that can inspect a dimension."""
    return tuple(capability for capability in CAPABILITIES if dimension in capability.dimensions)


def _capability_allows_target(capability: str, target: EntityRef) -> bool:
    """Return whether one semantic capability may inspect one target kind."""
    if target.kind == "Secret" or target.kind in ACCESS_KINDS:
        return False
    if capability == "resource_pressure":
        return target.kind == "Pod"
    if capability == "logs":
        return target.kind in {"Service", "Pod", *WORKLOAD_KINDS}
    if capability in {"history", "events"}:
        return bool(target.kind)
    if capability == "traffic":
        return target.kind == "Service"
    if capability in {"incident_events", "incident_changes"}:
        return target.kind == "Namespace"
    if capability == "runtime_traces":
        return target.kind in {"Pod", "Deployment", "StatefulSet", "DaemonSet"}
    if capability in {"describe", "neighbors"}:
        return bool(target.kind)
    return False


@dataclass(frozen=True)
class InformationGapContext:
    """Deterministic causal products used to derive information needs."""

    causal_roles: HypothesisCausalRoles
    root_cause_eligibilities: RootCauseEligibilities
    runtime_propagation: RuntimePropagation
    runtime_mechanism_bridges: RuntimeMechanismBridges
    topology: Topology | None = None
    symptom_entities: frozenset[EntityRef] = frozenset()


@dataclass(frozen=True)
class _InformationNeed:
    """Private causal predicate mapped to an exact bounded read surface."""

    dimension: GapDimension
    hypothesis_ids: tuple[str, ...] = ()
    alternative_ids: tuple[str, ...] = ()
    authorized_queries: tuple[AuthorizedQuery, ...] = ()
    missing_fact: str = ""
    rationale: str = ""
    origin: InformationGapOrigin = InformationGapOrigin.CAUSAL


def _stable_gap_id(
    dimension: GapDimension,
    hypotheses: Sequence[Hypothesis],
    alternatives: Sequence[StructuralAlternative],
    entity_scope: Sequence[str],
) -> str:
    payload = {
        "dimension": dimension.value,
        "hypotheses": sorted(hypothesis.hypothesis_id for hypothesis in hypotheses),
        "alternatives": sorted(item.alternative_id for item in alternatives),
        "entities": sorted(entity_scope),
    }
    digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    return f"gap:{dimension.value.lower()}:{digest}"


def _evidence_refs(hypotheses: Iterable[Hypothesis]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                evidence_id
                for hypothesis in hypotheses
                for finding in hypothesis.findings
                for evidence_id in finding.evidence_ids
            }
        )
    )[:16]


def _dimensions(
    hypotheses: Sequence[Hypothesis], alternatives: Sequence[StructuralAlternative] = ()
) -> tuple[GapDimension, ...]:
    dimensions = {
        dimension
        for hypothesis in hypotheses
        for finding in hypothesis.findings
        for dimension in _FINDING_DIMENSIONS.get(finding.kind, ())
    }
    dimensions.update(
        dimension for alternative in alternatives for dimension in alternative.queryable_dimensions
    )
    if not dimensions:
        dimensions.add(GapDimension.TOPOLOGY_RELATION)
    return tuple(sorted(dimensions, key=lambda item: item.value))


def _json_value(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _dimension_facts(hypothesis: Hypothesis, dimension: GapDimension) -> tuple[str, ...]:
    """Return only facts that can answer this particular gap dimension."""
    facts: list[str] = []
    for finding in hypothesis.findings:
        details = finding.details
        if dimension is GapDimension.CHANGE_TIMING:
            if finding.at is not None:
                facts.append(
                    f"{finding.kind.value}:{finding.at.isoformat()}:{finding.temporal_role.value}"
                )
        elif dimension is GapDimension.FAILURE_ONSET:
            if finding.at is not None and finding.kind in {
                FindingKind.CONTAINER_FAILURE,
                FindingKind.FAILURE_EVENT,
                FindingKind.DEPENDENCY_ERRORS,
            }:
                facts.append(f"{finding.kind.value}:{finding.at.isoformat()}")
        elif dimension is GapDimension.EVENT_SEQUENCE:
            if finding.kind is FindingKind.FAILURE_EVENT and finding.at is not None:
                facts.append(
                    f"{details.get('reason', '')}:{finding.at.isoformat()}:{details.get('count', '')}"
                )
        elif dimension is GapDimension.CONFIG_DIFFERENCE:
            changed = details.get("changed_paths") or details.get("changed_keys")
            before = details.get("before")
            after = details.get("after")
            if changed or before is not None or after is not None:
                facts.append(f"{finding.kind.value}:{_json_value((changed, before, after))}")
        elif dimension is GapDimension.AUTOSCALING_TARGET_STATE:
            conditions = details.get("conditions") or details.get("failure_reasons")
            targets = details.get("targets")
            if conditions or targets:
                facts.append(f"conditions={_json_value(conditions)}:targets={_json_value(targets)}")
        elif dimension is GapDimension.DEPENDENCY_HEALTH:
            service = details.get("service")
            errors = details.get("errors")
            if service or errors is not None:
                facts.append(f"service={service}:errors={errors}")
        elif dimension is GapDimension.LOG_ERROR_PATTERN:
            if finding.kind is FindingKind.DEPENDENCY_ERRORS:
                facts.append(finding.summary)
        elif dimension is GapDimension.RESOURCE_PRESSURE:
            if finding.kind is FindingKind.RESOURCE_PRESSURE:
                facts.append(_json_value(details))
        elif dimension is GapDimension.METRIC_BASELINE:
            baseline = details.get("baseline")
            if baseline is not None:
                facts.append(f"baseline={baseline}")
        elif dimension is GapDimension.METRIC_CHANGE:
            ratio = details.get("ratio")
            if ratio is not None:
                facts.append(f"ratio={ratio}")
        elif dimension is GapDimension.TOPOLOGY_RELATION:
            signature = hypothesis_signature(hypothesis)
            facts.extend(f"path={_json_value(path)}" for path in signature.causal_path_shape)
        elif dimension is GapDimension.ENTITY_STATE:
            if finding.details:
                facts.append(f"{finding.kind.value}:{_json_value(details)}")
    return tuple(sorted(set(facts)))


def _known_facts(hypotheses: Sequence[Hypothesis], dimension: GapDimension) -> tuple[str, ...]:
    facts: list[str] = []
    for hypothesis in hypotheses:
        dimension_facts = _dimension_facts(hypothesis, dimension)
        facts.extend(f"{hypothesis.hypothesis_id}: {fact}" for fact in dimension_facts)
    return tuple(sorted(set(facts)))[:12]


def _missing_fact(dimension: GapDimension) -> str:
    facts = {
        GapDimension.AUTOSCALING_TARGET_STATE: "which candidate target showed pre-onset scaling or saturation evidence",
        GapDimension.CONFIG_DIFFERENCE: "which candidate configuration differed in the incident window",
        GapDimension.CHANGE_TIMING: "which candidate had the causally relevant change before onset",
        GapDimension.DEPENDENCY_HEALTH: "which dependency was unhealthy before the symptom",
        GapDimension.EVENT_SEQUENCE: "which failure event occurred first in the causal episode",
        GapDimension.FAILURE_ONSET: "which entity entered failure before the symptom propagated",
        GapDimension.LOG_ERROR_PATTERN: "which candidate has matching captured error observations",
        GapDimension.TOPOLOGY_RELATION: "which candidate has the missing causal relation to the symptom",
        GapDimension.ENTITY_STATE: "which candidate had the relevant observed Kubernetes state",
        GapDimension.METRIC_BASELINE: "which candidate changed relative to its pre-onset metric baseline",
        GapDimension.METRIC_CHANGE: "which candidate had an incident-period metric change",
        GapDimension.RESOURCE_PRESSURE: "which candidate showed pre-onset resource pressure",
    }
    return facts[dimension]


def _outcomes(
    hypotheses: Sequence[Hypothesis],
    alternatives: Sequence[StructuralAlternative],
    missing: str,
    resolvability: GapResolvability,
) -> tuple[GapOutcome, ...]:
    ids = tuple(sorted(hypothesis.hypothesis_id for hypothesis in hypotheses))
    alternative_ids = tuple(sorted(item.alternative_id for item in alternatives))
    if resolvability is GapResolvability.ALREADY_OBSERVED:
        return (
            GapOutcome(
                kind=GapOutcomeKind.UNKNOWN,
                hypothesis_ids=ids,
                alternative_ids=alternative_ids,
                condition="the dimension is already observed without a differentiator",
                implication="no additional bounded observation is useful for this gap",
            ),
        )
    outcomes: list[GapOutcome] = []
    for hypothesis in sorted(hypotheses, key=lambda item: item.hypothesis_id):
        outcomes.append(
            GapOutcome(
                kind=GapOutcomeKind.SUPPORTS,
                hypothesis_ids=(hypothesis.hypothesis_id,),
                condition=f"the missing fact is observed specifically for {hypothesis.hypothesis_id}",
                implication=f"supports {hypothesis.hypothesis_id} over the other plausible hypotheses",
            )
        )
    for alternative in sorted(alternatives, key=lambda item: item.alternative_id):
        outcomes.append(
            GapOutcome(
                hypothesis_ids=(),
                alternative_ids=(alternative.alternative_id,),
                kind=GapOutcomeKind.SUPPORTS,
                condition=f"the missing fact promotes {alternative.alternative_id} into a causal explanation",
                implication=f"promotes {alternative.actor.canonical} for deterministic RCA evaluation",
            )
        )
    outcomes.extend(
        (
            GapOutcome(
                kind=GapOutcomeKind.NO_DATA,
                hypothesis_ids=ids,
                alternative_ids=alternative_ids,
                condition="the evidence source returns no observation",
                implication="does not support or contradict any hypothesis; ambiguity remains",
            ),
            GapOutcome(
                kind=GapOutcomeKind.UNKNOWN,
                hypothesis_ids=ids,
                alternative_ids=alternative_ids,
                condition="the observation is inconclusive or contradictory",
                implication="no deterministic resolution follows",
            ),
        )
    )
    return tuple(outcomes)


def _available_capabilities(
    dimension: GapDimension,
    hypotheses: Sequence[Hypothesis],
    source: ObservationSource | None,
) -> tuple[ToolCapability, ...]:
    capabilities = capabilities_for(dimension)
    if source is None:
        return tuple(
            capability
            for capability in capabilities
            if capability.name not in {"resource_pressure", "traffic"}
        )
    available: list[ToolCapability] = []
    bounded = bool(getattr(source, "initial_observation_bounded", False))
    # The seed view already contains latest state and topology.  Re-exposing
    # those capabilities would create queries that can only repeat the seed;
    # investigation may instead query raw history, events, logs, or metrics.
    blocked_in_bounded_view = {"describe", "neighbors"} if bounded else set()
    capability_source = getattr(source, "full_source", source)
    source_methods = {
        "history": "object_history",
        "events": "events",
        "incident_events": "events",
        "incident_changes": "object_history",
        "logs": "error_logs",
        "resource_pressure": "resource_pressure",
        "traffic": "traffic_observations",
        "runtime_traces": "trace_observations",
    }
    for capability in capabilities:
        if capability.name in blocked_in_bounded_view:
            continue
        supports = getattr(capability_source, "supports", None)
        method_name = source_methods.get(capability.name)
        # Availability is a capability contract, not a telemetry existence
        # probe.  Reading metrics/traffic here would leak whether incident
        # evidence exists before an investigation query is authorized.
        if callable(supports):
            aliases = {"incident_events": "events", "incident_changes": "history"}
            alias = aliases.get(capability.name)
            available_capability = bool(
                supports(capability.name) or (alias is not None and supports(alias))
            )
        else:
            available_capability = method_name is not None and callable(
                getattr(capability_source, method_name, None)
            )
        if available_capability:
            available.append(capability)
    return tuple(available)


_WORKLOAD_CONTROLLER_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"})
_TRACE_TARGET_KINDS = frozenset({"Pod", "Deployment", "StatefulSet", "DaemonSet"})
_CHAOS_KINDS = frozenset({"StressChaos", "NetworkChaos", "PodChaos", "Schedule", "Workflow"})


def _available_names(dimension: GapDimension, source: ObservationSource | None) -> frozenset[str]:
    return frozenset(item.name for item in _available_capabilities(dimension, (), source))


def _typed_runtime_provider_available(source: ObservationSource | None, capability: str) -> bool:
    """Check explicit provider configuration, never whether incident data exists."""
    current = source
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        supports_typed_runtime = getattr(current, "supports_typed_runtime", None)
        if callable(supports_typed_runtime):
            return bool(supports_typed_runtime(capability))
        current = getattr(current, "full_source", None) or getattr(current, "base", None)
    return False


def _query(
    capability: str,
    target: EntityRef,
    *,
    alternative_id: str | None = None,
) -> AuthorizedQuery | None:
    if not _capability_allows_target(capability, target):
        return None
    return AuthorizedQuery(
        capability=capability,
        target=target,
        alternative_ids=(alternative_id,) if alternative_id is not None else (),
    )


def incident_namespaces(case: object) -> tuple[str, ...]:
    """Return bounded namespaces visible in the current incident context."""
    symptoms = getattr(case, "symptoms", None)
    context = getattr(case, "context", None)
    values = set(getattr(symptoms, "namespaces", ()))
    values.update(
        entity.namespace
        for entity in getattr(context, "symptom_entities", set())
        if getattr(entity, "namespace", "")
    )
    ordered = tuple(sorted(value for value in values if value))
    return ordered[:4]


def authorized_event_namespaces(case: object, config: object | None = None) -> tuple[str, ...]:
    """Return visible plus explicitly configured event-observation namespaces."""
    visible = incident_namespaces(case)
    configured = getattr(config, "auxiliary_event_namespaces", ()) if config is not None else ()
    auxiliary = tuple(sorted({value.strip() for value in configured if value.strip()}))[:4]
    return tuple(sorted(set((*visible, *auxiliary))))[:8]


def _discovery_needs(
    source: ObservationSource | None,
    event_namespaces: Sequence[str],
    change_namespaces: Sequence[str],
) -> tuple[_InformationNeed, ...]:
    if source is None or not getattr(source, "initial_observation_bounded", False):
        return ()
    needs: list[_InformationNeed] = []
    event_available = "incident_events" in _available_names(GapDimension.EVENT_SEQUENCE, source)
    changes_available = "incident_changes" in _available_names(GapDimension.CHANGE_TIMING, source)
    for namespace_name in event_namespaces:
        namespace = EntityRef(kind="Namespace", name=namespace_name)
        if event_available:
            query = _query("incident_events", namespace)
            if query is not None:
                needs.append(
                    _InformationNeed(
                        dimension=GapDimension.EVENT_SEQUENCE,
                        authorized_queries=(query,),
                        missing_fact=(
                            "whether incident-scoped Kubernetes events reveal an unobserved "
                            "failure, autoscaling, scheduling, quota, or fault actor"
                        ),
                        rationale=(
                            "bounded incident-event discovery may reveal causal actors absent "
                            "from the initial structural frontier"
                        ),
                        origin=InformationGapOrigin.DISCOVERY,
                    )
                )
    for namespace_name in change_namespaces:
        namespace = EntityRef(kind="Namespace", name=namespace_name)
        if changes_available:
            query = _query("incident_changes", namespace)
            if query is not None:
                needs.append(
                    _InformationNeed(
                        dimension=GapDimension.CHANGE_TIMING,
                        authorized_queries=(query,),
                        missing_fact=(
                            "whether incident-scoped source changes reveal an unobserved "
                            "initiating object change"
                        ),
                        rationale=(
                            "bounded incident-change discovery may reveal source-capable "
                            "actors absent from the initial structural frontier"
                        ),
                        origin=InformationGapOrigin.DISCOVERY,
                    )
                )
    return tuple(needs)


def _actor_local_contract(
    actor: EntityRef,
    dimension: GapDimension,
    source: ObservationSource | None,
    *,
    alternative_id: str | None = None,
) -> tuple[AuthorizedQuery, ...]:
    """Return only exact actor-local capabilities for one causal predicate."""
    typed_tempo = _typed_runtime_provider_available(source, "runtime_traces")
    typed_prometheus = _typed_runtime_provider_available(source, "resource_pressure")
    capability_by_kind: dict[str, tuple[str, ...]] = {
        **{
            kind: (
                ("history",)
                if dimension in {GapDimension.CHANGE_TIMING, GapDimension.CONFIG_DIFFERENCE}
                else ("runtime_traces",)
                if kind in _TRACE_TARGET_KINDS and dimension is GapDimension.DEPENDENCY_HEALTH
                else ("runtime_traces",)
                if kind in _TRACE_TARGET_KINDS
                and dimension is GapDimension.FAILURE_ONSET
                and typed_tempo
                else ()
            )
            for kind in _WORKLOAD_CONTROLLER_KINDS
        },
        "ConfigMap": (
            ("history",)
            if dimension in {GapDimension.CHANGE_TIMING, GapDimension.CONFIG_DIFFERENCE}
            else ()
        ),
        "HorizontalPodAutoscaler": (
            "history"
            if dimension in {GapDimension.CHANGE_TIMING, GapDimension.FAILURE_ONSET}
            else "events",
        ),
        # Retain the repository's historical synthetic HPA spelling at this
        # private compatibility boundary; production Kubernetes identity is
        # still the exact HorizontalPodAutoscaler kind.
        "HPA": (
            "history"
            if dimension in {GapDimension.CHANGE_TIMING, GapDimension.FAILURE_ONSET}
            else "events",
        ),
        "Pod": {
            GapDimension.RESOURCE_PRESSURE: ("resource_pressure",),
            GapDimension.METRIC_BASELINE: ("resource_pressure",) if typed_prometheus else (),
            GapDimension.DEPENDENCY_HEALTH: ("runtime_traces",),
            GapDimension.FAILURE_ONSET: (
                ("events", "runtime_traces") if typed_tempo else ("events",)
            ),
        }.get(dimension, ()),
        "Service": {
            GapDimension.DEPENDENCY_HEALTH: ("logs",),
            GapDimension.LOG_ERROR_PATTERN: ("logs",),
            GapDimension.METRIC_CHANGE: ("traffic",),
            GapDimension.METRIC_BASELINE: ("traffic",),
        }.get(dimension, ()),
        "NetworkPolicy": ("history",) if dimension is GapDimension.CHANGE_TIMING else (),
        **{
            kind: (
                ("events",)
                if dimension is GapDimension.EVENT_SEQUENCE
                else ("history", "events")
                if dimension is GapDimension.FAILURE_ONSET
                else ("history",)
                if dimension is GapDimension.CHANGE_TIMING
                else ()
            )
            for kind in _CHAOS_KINDS
        },
    }
    if actor.kind in {"HorizontalPodAutoscaler", "HPA"} and dimension not in {
        GapDimension.CHANGE_TIMING,
        GapDimension.AUTOSCALING_TARGET_STATE,
        GapDimension.EVENT_SEQUENCE,
        GapDimension.FAILURE_ONSET,
    }:
        return ()
    if (
        actor.kind in {"HorizontalPodAutoscaler", "HPA"}
        and dimension is GapDimension.AUTOSCALING_TARGET_STATE
    ):
        capability_by_kind[actor.kind] = ("events",)
    capabilities = capability_by_kind.get(actor.kind, ())
    available = _available_names(dimension, source)
    result = [
        query
        for capability in capabilities
        if capability in available
        for query in (_query(capability, actor, alternative_id=alternative_id),)
        if query is not None
    ]
    return tuple(sorted(result, key=lambda item: (item.capability, item.target.canonical)))


def _dependency_trace_targets(alternative: StructuralAlternative) -> tuple[EntityRef, ...]:
    kind_order = {"Deployment": 0, "StatefulSet": 1, "DaemonSet": 2}
    targets = tuple(
        sorted(
            (
                target
                for target in alternative.observation_targets
                if target.kind in {"Deployment", "StatefulSet", "DaemonSet"}
            ),
            key=lambda item: (kind_order[item.kind], item.canonical),
        )
    )
    if targets:
        return targets
    return tuple(
        sorted(
            (target for target in alternative.observation_targets if target.kind == "Pod"),
            key=lambda item: item.canonical,
        )
    )


def _dependency_log_targets(
    alternative: StructuralAlternative,
    runtime_context: InformationGapContext,
) -> tuple[EntityRef, ...]:
    """Return symptom-linked callers whose logs feed dependency_findings().

    ``dependency_findings`` indexes connection errors by the service names of
    the caller workload, not by the downstream dependency actor.  The caller
    must be visible in the incident context and have a direct ``calls`` edge
    to this structural dependency alternative.
    """
    topology = runtime_context.topology
    if topology is None:
        # Preserve the standalone information-gap helper contract for callers
        # that do not have topology context.  Production diagnosis always
        # supplies the case topology below.
        return (alternative.actor,)
    symptoms = runtime_context.symptom_entities
    callers: set[EntityRef] = set()
    candidates = tuple(
        target for target in alternative.observation_targets if target.kind in WORKLOAD_KINDS
    )
    if not candidates:
        candidates = tuple(topology.latest)
    for entity in candidates:
        if entity.kind not in WORKLOAD_KINDS:
            continue
        if alternative.actor not in topology.outgoing(entity, "calls"):
            continue
        if entity in symptoms or any(topology.workload_of(item) == entity for item in symptoms):
            callers.add(entity)
    return tuple(sorted(callers, key=lambda item: item.canonical))


def _ambiguity_dimensions(actor: EntityRef) -> tuple[GapDimension, ...]:
    """Return dimensions for resolver-supported ambiguity, scoped to the actor."""
    if actor.kind in _WORKLOAD_CONTROLLER_KINDS or actor.kind in {"ConfigMap", "NetworkPolicy"}:
        return (GapDimension.CHANGE_TIMING, GapDimension.CONFIG_DIFFERENCE)
    if actor.kind in {"HorizontalPodAutoscaler", "HPA"}:
        return (
            GapDimension.AUTOSCALING_TARGET_STATE,
            GapDimension.EVENT_SEQUENCE,
            GapDimension.RESOURCE_PRESSURE,
            GapDimension.FAILURE_ONSET,
        )
    if actor.kind == "Pod":
        return (
            GapDimension.DEPENDENCY_HEALTH,
            GapDimension.EVENT_SEQUENCE,
            GapDimension.FAILURE_ONSET,
            GapDimension.RESOURCE_PRESSURE,
        )
    if actor.kind == "Service":
        return (
            GapDimension.DEPENDENCY_HEALTH,
            GapDimension.LOG_ERROR_PATTERN,
            GapDimension.METRIC_CHANGE,
        )
    if actor.kind in _CHAOS_KINDS:
        return (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET)
    return ()


def _initiating_dimensions(actor: EntityRef) -> tuple[GapDimension, ...]:
    """Dimensions whose actor-local evidence can establish initiation."""
    return _ambiguity_dimensions(actor)


def _structural_contract(
    alternative: StructuralAlternative,
    dimension: GapDimension,
    source: ObservationSource | None,
    runtime_context: InformationGapContext,
) -> tuple[AuthorizedQuery, ...]:
    """Map a structural role to its exact observation actor(s)."""
    actor = alternative.actor
    role = alternative.role
    if role in {"configuration_source", "workload_controller"}:
        if dimension in {GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING}:
            return _actor_local_contract(
                actor, dimension, source, alternative_id=alternative.alternative_id
            )
        return ()
    if role == "autoscaler":
        if dimension in {GapDimension.AUTOSCALING_TARGET_STATE, GapDimension.EVENT_SEQUENCE}:
            return _actor_local_contract(
                actor, dimension, source, alternative_id=alternative.alternative_id
            )
        return ()
    if role == "fault_actor":
        if dimension in {GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET}:
            return _actor_local_contract(
                actor, dimension, source, alternative_id=alternative.alternative_id
            )
        return ()
    if role == "dependency":
        if dimension in {GapDimension.DEPENDENCY_HEALTH, GapDimension.LOG_ERROR_PATTERN}:
            queries: list[AuthorizedQuery] = []
            if dimension in {GapDimension.DEPENDENCY_HEALTH, GapDimension.LOG_ERROR_PATTERN}:
                for target in _dependency_log_targets(alternative, runtime_context):
                    query = _query("logs", target, alternative_id=alternative.alternative_id)
                    if query is not None and "logs" in _available_names(dimension, source):
                        queries.append(query)
            if dimension is GapDimension.DEPENDENCY_HEALTH:
                for target in _dependency_trace_targets(alternative):
                    query = _query(
                        "runtime_traces", target, alternative_id=alternative.alternative_id
                    )
                    if query is not None and "runtime_traces" in _available_names(
                        dimension, source
                    ):
                        queries.append(query)
            return tuple(sorted(queries, key=lambda item: (item.capability, item.target.canonical)))
        return ()
    if role == "network_policy" and dimension is GapDimension.CHANGE_TIMING:
        return _actor_local_contract(
            actor, dimension, source, alternative_id=alternative.alternative_id
        )
    return ()


def _hypothesis_needs(
    hypotheses: Sequence[Hypothesis],
    resolution: ResolutionTrace,
    source: ObservationSource | None,
    runtime_context: InformationGapContext,
) -> tuple[_InformationNeed, ...]:
    by_id = {item.hypothesis_id: item for item in hypotheses}
    selected_ids = set(resolution.unresolved_hypotheses)
    if resolution.state is Resolution.AMBIGUOUS:
        selected_ids.update(resolution.leading_hypothesis_ids)
    if not selected_ids:
        selected_ids.update(resolution.plausible_hypotheses)
    needs: list[_InformationNeed] = []
    for audit in sorted(resolution.hypothesis_audits, key=lambda item: item.hypothesis_id):
        hypothesis = by_id.get(audit.hypothesis_id)
        if hypothesis is None or audit.hypothesis_id not in selected_ids:
            continue
        eligibility = runtime_context.root_cause_eligibilities.for_hypothesis(
            hypothesis.hypothesis_id
        )
        role = runtime_context.causal_roles.for_hypothesis(hypothesis.hypothesis_id)
        if (
            audit.epistemic_state.value == "UNRESOLVED"
            and "NO_ONSET_CAPABLE_INITIATING_EVIDENCE" in audit.plausibility_reasons
            and (
                eligibility is None
                or eligibility.state is not RootCauseEligibilityState.INELIGIBLE_PROPAGATED_EFFECT
            )
        ):
            for dimension in sorted(
                _initiating_dimensions(hypothesis.causal_actor),
                key=lambda item: item.value,
            ):
                queries = _actor_local_contract(hypothesis.causal_actor, dimension, source)
                if queries:
                    needs.append(
                        _InformationNeed(
                            dimension=dimension,
                            hypothesis_ids=(hypothesis.hypothesis_id,),
                            authorized_queries=queries,
                            missing_fact=(
                                f"whether {hypothesis.causal_actor.canonical} has actor-local "
                                f"{dimension.value.lower()} evidence"
                            ),
                            rationale="the resolver audit explicitly lacks onset-capable initiating evidence",
                        )
                    )
        if (
            resolution.state is Resolution.AMBIGUOUS
            and audit.hypothesis_id in resolution.leading_hypothesis_ids
            and audit.epistemic_state.value == "SUPPORTED"
        ):
            for dimension in _ambiguity_dimensions(hypothesis.causal_actor):
                queries = _actor_local_contract(hypothesis.causal_actor, dimension, source)
                if queries or (
                    dimension is GapDimension.RESOURCE_PRESSURE
                    and any(
                        finding.kind is FindingKind.RESOURCE_PRESSURE
                        for finding in hypothesis.findings
                    )
                ):
                    needs.append(
                        _InformationNeed(
                            dimension=dimension,
                            hypothesis_ids=(hypothesis.hypothesis_id,),
                            authorized_queries=queries,
                            missing_fact=(
                                f"which actor-local {dimension.value.lower()} evidence "
                                f"distinguishes {hypothesis.causal_actor.canonical}"
                            ),
                            rationale=(
                                "resolver ambiguity leaves the initiating evidence source "
                                "unresolved for this causal actor"
                            ),
                        )
                    )
        if (
            role is not None
            and role.causal_actor == hypothesis.causal_actor
            and role.role is HypothesisCausalRole.UNKNOWN
            and role.unresolved_incoming_edges > 0
            and (
                eligibility is None
                or eligibility.state is not RootCauseEligibilityState.INELIGIBLE_PROPAGATED_EFFECT
            )
            and hypothesis.causal_actor.kind in _TRACE_TARGET_KINDS
        ):
            runtime_queries = _actor_local_contract(
                hypothesis.causal_actor, GapDimension.DEPENDENCY_HEALTH, source
            )
            if runtime_queries:
                needs.append(
                    _InformationNeed(
                        dimension=GapDimension.DEPENDENCY_HEALTH,
                        hypothesis_ids=(hypothesis.hypothesis_id,),
                        authorized_queries=runtime_queries,
                        missing_fact=(
                            f"whether {hypothesis.causal_actor.canonical} was already "
                            "participating in an unresolved runtime boundary"
                        ),
                        rationale="causal-role assessment has unresolved incoming runtime edges",
                    )
                )
    merged: dict[tuple[str, str], _InformationNeed] = {}
    for need in needs:
        merge_key = (
            need.dimension.value,
            "resolver-ambiguity"
            if need.rationale.startswith("resolver ambiguity")
            else need.rationale,
        )
        previous = merged.get(merge_key)
        if previous is None:
            merged[merge_key] = need
            continue
        merged_queries = {
            (item.capability, item.target.canonical): item
            for item in (*previous.authorized_queries, *need.authorized_queries)
        }
        merged[merge_key] = _InformationNeed(
            dimension=need.dimension,
            hypothesis_ids=tuple(sorted({*previous.hypothesis_ids, *need.hypothesis_ids})),
            alternative_ids=tuple(sorted({*previous.alternative_ids, *need.alternative_ids})),
            authorized_queries=tuple(
                sorted(
                    merged_queries.values(),
                    key=lambda item: (item.capability, item.target.canonical),
                )
            ),
            missing_fact=previous.missing_fact,
            rationale=previous.rationale,
        )
    return tuple(merged.values())


def _need_gap(
    need: _InformationNeed,
    hypotheses: Sequence[Hypothesis],
    alternatives: Sequence[StructuralAlternative],
) -> InformationGap:
    ordered_hypotheses = tuple(
        sorted(
            (item for item in hypotheses if item.hypothesis_id in need.hypothesis_ids),
            key=lambda item: item.hypothesis_id,
        )
    )
    ordered_alternatives = tuple(
        sorted(
            (item for item in alternatives if item.alternative_id in need.alternative_ids),
            key=lambda item: item.alternative_id,
        )
    )
    target_scope = tuple(
        sorted({item.target for item in need.authorized_queries}, key=lambda item: item.canonical)
    )
    payload = {
        "dimension": need.dimension.value,
        "hypotheses": list(need.hypothesis_ids),
        "alternatives": list(need.alternative_ids),
        "queries": [
            (item.capability, item.target.canonical, item.alternative_ids)
            for item in need.authorized_queries
        ],
    }
    if need.origin is InformationGapOrigin.DISCOVERY:
        payload["origin"] = need.origin.value
    gap_id = f"gap:{need.dimension.value.lower()}:{sha256(_json_value(payload).encode()).hexdigest()[:20]}"
    resolvability = (
        GapResolvability.RESOLVABLE
        if need.authorized_queries
        else GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS
    )
    return InformationGap(
        gap_id=gap_id,
        origin=need.origin,
        dimension=need.dimension,
        hypothesis_ids=tuple(item.hypothesis_id for item in ordered_hypotheses),
        alternative_ids=tuple(item.alternative_id for item in ordered_alternatives),
        entity_scope=target_scope,
        known_facts=_known_facts(ordered_hypotheses, need.dimension),
        missing_fact=need.missing_fact or _missing_fact(need.dimension),
        required_relation=_RELATIONS.get(need.dimension),
        discriminating_outcomes=_outcomes(
            ordered_hypotheses, ordered_alternatives, need.missing_fact, resolvability
        ),
        authorized_queries=need.authorized_queries,
        candidate_tools=tuple(sorted({item.capability for item in need.authorized_queries})),
        evidence_refs=_evidence_refs(ordered_hypotheses),
        priority=1,
        resolvability=resolvability,
        rationale=need.rationale,
    )


def _derive_runtime_aware_gaps(
    hypotheses: Sequence[Hypothesis],
    resolution: ResolutionTrace,
    source: ObservationSource | None,
    alternatives: Sequence[StructuralAlternative],
    runtime_context: InformationGapContext,
    discovery_event_namespaces: Sequence[str],
    discovery_change_namespaces: Sequence[str],
) -> tuple[InformationGap, ...]:
    open_alternatives = tuple(
        item for item in alternatives if item.status is FrontierStatus.UNEXPLORED
    )
    if resolution.state is Resolution.RESOLVED and not open_alternatives:
        return ()
    needs = list(_hypothesis_needs(hypotheses, resolution, source, runtime_context))
    if resolution.state is not Resolution.RESOLVED:
        needs.extend(
            _discovery_needs(source, discovery_event_namespaces, discovery_change_namespaces)
        )
    for alternative in open_alternatives:
        for dimension in sorted(alternative.queryable_dimensions, key=lambda item: item.value):
            queries = _structural_contract(alternative, dimension, source, runtime_context)
            if not queries:
                continue
            needs.append(
                _InformationNeed(
                    dimension=dimension,
                    alternative_ids=(alternative.alternative_id,),
                    authorized_queries=queries,
                    missing_fact=(
                        f"whether {alternative.actor.canonical} has actor-local evidence "
                        f"for {dimension.value.lower()}"
                    ),
                    rationale=f"open {alternative.role} structural alternative requires actor-local evidence",
                )
            )
    unique: dict[tuple[object, ...], _InformationNeed] = {}
    for need in needs:
        key = (
            need.dimension.value,
            need.hypothesis_ids,
            need.alternative_ids,
            tuple((item.capability, item.target.canonical) for item in need.authorized_queries),
        )
        unique[key] = need
    return tuple(
        _need_gap(need, hypotheses, alternatives)
        for need in sorted(
            unique.values(),
            key=lambda item: (
                item.dimension.value,
                item.hypothesis_ids,
                item.alternative_ids,
                tuple(
                    (query.capability, query.target.canonical) for query in item.authorized_queries
                ),
            ),
        )
    )


def _gap_for(
    dimension: GapDimension,
    hypotheses: Sequence[Hypothesis],
    alternatives: Sequence[StructuralAlternative],
    source: ObservationSource | None,
) -> InformationGap:
    ordered = tuple(sorted(hypotheses, key=lambda item: item.hypothesis_id))
    facts_by_hypothesis = tuple(_dimension_facts(hypothesis, dimension) for hypothesis in ordered)
    complete = all(facts_by_hypothesis)
    same_facts = complete and len(set(facts_by_hypothesis)) == 1
    capabilities = _available_capabilities(dimension, ordered, source)
    bounded_can_expand = bool(getattr(source, "initial_observation_bounded", False)) and (
        dimension in _BOUNDED_EXPANDABLE_DIMENSIONS
    )
    source_unavailable_for_metric = source is None and dimension in {
        GapDimension.RESOURCE_PRESSURE,
        GapDimension.METRIC_BASELINE,
        GapDimension.METRIC_CHANGE,
    }
    authorized: dict[tuple[str, EntityRef], set[str]] = {}
    for capability in capabilities:
        for hypothesis in ordered:
            for target in (hypothesis.causal_actor, *hypothesis.members):
                if _capability_allows_target(capability.name, target):
                    authorized.setdefault((capability.name, target), set())
        for alternative in alternatives:
            if dimension not in alternative.queryable_dimensions:
                continue
            for target in alternative.observation_targets:
                if _capability_allows_target(capability.name, target):
                    authorized.setdefault((capability.name, target), set()).add(
                        alternative.alternative_id
                    )
    authorized_queries = tuple(
        AuthorizedQuery(
            capability=capability,
            target=target,
            alternative_ids=tuple(sorted(alternative_ids)),
        )
        for (capability, target), alternative_ids in sorted(
            authorized.items(), key=lambda item: (item[0][0], item[0][1].canonical)
        )
    )
    entity_scope = tuple(
        sorted({item.target for item in authorized_queries}, key=lambda entity: entity.canonical)
    )
    if source_unavailable_for_metric:
        resolvability = GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS
    elif facts_by_hypothesis and complete and not same_facts:
        resolvability = GapResolvability.ALREADY_OBSERVED
    elif same_facts and not bounded_can_expand:
        resolvability = GapResolvability.ALREADY_OBSERVED
    elif authorized_queries:
        resolvability = GapResolvability.RESOLVABLE
    else:
        resolvability = GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS
    tools = (
        tuple(sorted({item.capability for item in authorized_queries}))
        if resolvability is GapResolvability.RESOLVABLE
        else ()
    )
    missing = _missing_fact(dimension)
    return InformationGap(
        gap_id=_stable_gap_id(
            dimension,
            ordered,
            alternatives,
            [entity.canonical for entity in entity_scope],
        ),
        dimension=dimension,
        hypothesis_ids=tuple(hypothesis.hypothesis_id for hypothesis in ordered),
        alternative_ids=tuple(item.alternative_id for item in alternatives),
        entity_scope=entity_scope,
        known_facts=_known_facts(ordered, dimension),
        missing_fact=missing,
        required_relation=_RELATIONS.get(dimension),
        discriminating_outcomes=_outcomes(ordered, alternatives, missing, resolvability),
        authorized_queries=authorized_queries,
        candidate_tools=tools,
        evidence_refs=_evidence_refs(ordered),
        priority=1,
        resolvability=resolvability,
        rationale=(
            "The hypotheses share the currently observed causal support; this bounded "
            "fact could distinguish them."
            if resolvability is GapResolvability.RESOLVABLE
            else "The relevant dimension is already observed or cannot be acquired by an available capability."
        ),
    )


def derive_information_gaps(
    hypotheses: Sequence[Hypothesis],
    resolution: ResolutionTrace,
    source: ObservationSource | None = None,
    structural_alternatives: Sequence[StructuralAlternative] = (),
    *,
    runtime_context: InformationGapContext | None = None,
    discovery_event_namespaces: Sequence[str] = (),
    discovery_change_namespaces: Sequence[str] = (),
) -> tuple[InformationGap, ...]:
    """Derive gaps for unresolved evidence or open structural alternatives."""
    if runtime_context is not None:
        return _derive_runtime_aware_gaps(
            hypotheses,
            resolution,
            source,
            structural_alternatives,
            runtime_context,
            discovery_event_namespaces,
            discovery_change_namespaces,
        )
    open_alternatives = tuple(
        item for item in structural_alternatives if item.status is FrontierStatus.UNEXPLORED
    )
    if resolution.state is Resolution.RESOLVED and not open_alternatives:
        return ()
    by_id = {hypothesis.hypothesis_id: hypothesis for hypothesis in hypotheses}
    selected_ids = tuple(
        resolution.leading_hypothesis_ids
        if resolution.state is Resolution.AMBIGUOUS
        else (
            resolution.unresolved_hypotheses
            or resolution.plausible_hypotheses
            or tuple(
                hypothesis.hypothesis_id
                for hypothesis in hypotheses
                if hypothesis.causal_explanation != "UNLINKED"
            )[:2]
        )
    )
    selected = tuple(by_id[item] for item in selected_ids if item in by_id)
    if not selected and not open_alternatives:
        return ()
    dimensions = _dimensions(selected, open_alternatives)
    gaps = tuple(
        _gap_for(
            dimension,
            selected,
            tuple(
                alternative
                for alternative in open_alternatives
                if dimension in alternative.queryable_dimensions
            ),
            source,
        )
        for dimension in dimensions
    )
    return tuple(sorted(gaps, key=lambda gap: (gap.dimension.value, gap.gap_id)))


__all__ = [
    "CAPABILITIES",
    "InformationGapContext",
    "authorized_event_namespaces",
    "capabilities_for",
    "derive_information_gaps",
    "incident_namespaces",
]
