"""Deterministic descriptions of evidence missing between hypotheses.

This module describes what an eventual investigator could inspect.  It does
not execute a capability, select a tool dynamically, or call a model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from hashlib import sha256

from packages.rca.model import (
    FindingKind,
    FrontierStatus,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    Hypothesis,
    InformationGap,
    Resolution,
    ResolutionTrace,
    StructuralAlternative,
    ToolCapability,
)
from packages.rca.resolution import hypothesis_signature
from packages.rca.source import ObservationSource

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
        "logs": "error_logs",
        "resource_pressure": "resource_pressure",
        "traffic": "traffic_observations",
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
            available_capability = bool(supports(capability.name))
        else:
            available_capability = method_name is not None and callable(
                getattr(capability_source, method_name, None)
            )
        if available_capability:
            available.append(capability)
    return tuple(available)


def _gap_for(
    dimension: GapDimension,
    hypotheses: Sequence[Hypothesis],
    alternatives: Sequence[StructuralAlternative],
    source: ObservationSource | None,
) -> InformationGap:
    ordered = tuple(sorted(hypotheses, key=lambda item: item.hypothesis_id))
    entity_scope = tuple(
        sorted(
            {
                entity
                for hypothesis in ordered
                for entity in (hypothesis.causal_actor, *hypothesis.members)
            }
            | {
                entity for alternative in alternatives for entity in alternative.observation_targets
            },
            key=lambda entity: entity.canonical,
        )
    )
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
    if source_unavailable_for_metric:
        resolvability = GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS
    elif alternatives and capabilities:
        # A structural alternative has no evidence facts by definition.  Its
        # query is therefore meaningful even when the evidence-backed
        # hypothesis already has a complete fact set for this dimension.
        resolvability = GapResolvability.RESOLVABLE
    elif complete and not same_facts:
        resolvability = GapResolvability.ALREADY_OBSERVED
    elif same_facts and not bounded_can_expand:
        resolvability = GapResolvability.ALREADY_OBSERVED
    else:
        resolvability = (
            GapResolvability.RESOLVABLE
            if capabilities
            else GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS
        )
    tools = (
        tuple(sorted({capability.name for capability in capabilities}))
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
) -> tuple[InformationGap, ...]:
    """Derive gaps for unresolved evidence or open structural alternatives."""
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
            resolution.plausible_hypotheses
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
        _gap_for(dimension, selected, open_alternatives, source) for dimension in dimensions
    )
    return tuple(sorted(gaps, key=lambda gap: (gap.dimension.value, gap.gap_id)))


__all__ = [
    "CAPABILITIES",
    "capabilities_for",
    "derive_information_gaps",
]
