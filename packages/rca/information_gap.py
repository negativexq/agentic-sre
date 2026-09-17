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
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    Hypothesis,
    InformationGap,
    Resolution,
    ResolutionTrace,
    ToolCapability,
)
from packages.rca.resolution import hypothesis_signature

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
)


_FINDING_DIMENSIONS: dict[FindingKind, tuple[GapDimension, ...]] = {
    FindingKind.AUTOSCALING_FAILURE: (GapDimension.AUTOSCALING_TARGET_STATE,),
    FindingKind.CONFIG_CHANGE: (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    FindingKind.SPEC_CHANGE: (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    FindingKind.IMAGE_CHANGE: (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
    FindingKind.DEPENDENCY_ERRORS: (GapDimension.DEPENDENCY_HEALTH,),
    FindingKind.FAILURE_EVENT: (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET),
    FindingKind.CONTAINER_FAILURE: (GapDimension.FAILURE_ONSET, GapDimension.EVENT_SEQUENCE),
    FindingKind.RESOURCE_PRESSURE: (GapDimension.RESOURCE_PRESSURE, GapDimension.METRIC_BASELINE),
    FindingKind.TRAFFIC_INCREASE: (GapDimension.METRIC_CHANGE, GapDimension.METRIC_BASELINE),
    FindingKind.NETWORK_RESTRICTION: (GapDimension.TOPOLOGY_RELATION, GapDimension.ENTITY_STATE),
}

_RELATIONS: dict[GapDimension, str] = {
    GapDimension.AUTOSCALING_TARGET_STATE: "scales",
    GapDimension.CONFIG_DIFFERENCE: "configures",
    GapDimension.DEPENDENCY_HEALTH: "dependency_of",
    GapDimension.TOPOLOGY_RELATION: "causal_path",
}


def capabilities_for(dimension: GapDimension) -> tuple[ToolCapability, ...]:
    """Return actual bounded read-only capabilities that can inspect a dimension."""
    return tuple(capability for capability in CAPABILITIES if dimension in capability.dimensions)


def _stable_gap_id(
    dimension: GapDimension, hypotheses: Sequence[Hypothesis], entity_scope: Sequence[str]
) -> str:
    payload = {
        "dimension": dimension.value,
        "hypotheses": sorted(hypothesis.hypothesis_id for hypothesis in hypotheses),
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


def _dimensions(hypotheses: Sequence[Hypothesis]) -> tuple[GapDimension, ...]:
    dimensions = {
        dimension
        for hypothesis in hypotheses
        for finding in hypothesis.findings
        for dimension in _FINDING_DIMENSIONS.get(finding.kind, ())
    }
    if not dimensions:
        dimensions.add(GapDimension.TOPOLOGY_RELATION)
    return tuple(sorted(dimensions, key=lambda item: item.value))


def _known_facts(hypotheses: Sequence[Hypothesis], dimension: GapDimension) -> tuple[str, ...]:
    facts: list[str] = []
    for hypothesis in hypotheses:
        signature = hypothesis_signature(hypothesis)
        if signature.initiating_kinds:
            facts.append(
                f"{hypothesis.hypothesis_id}: initiating={','.join(signature.initiating_kinds)}"
            )
        if signature.temporal_profile:
            facts.append(
                f"{hypothesis.hypothesis_id}: temporal={','.join(signature.temporal_profile)}"
            )
        facts.append(f"{hypothesis.hypothesis_id}: relation={signature.symptom_relation}")
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


def _outcomes(hypotheses: Sequence[Hypothesis], missing: str) -> tuple[GapOutcome, ...]:
    ids = tuple(sorted(hypothesis.hypothesis_id for hypothesis in hypotheses))
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
    outcomes.extend(
        (
            GapOutcome(
                kind=GapOutcomeKind.NO_DATA,
                hypothesis_ids=ids,
                condition="the evidence source returns no observation",
                implication="does not support or contradict any hypothesis; ambiguity remains",
            ),
            GapOutcome(
                kind=GapOutcomeKind.UNKNOWN,
                hypothesis_ids=ids,
                condition="the observation is inconclusive or contradictory",
                implication="no deterministic resolution follows",
            ),
        )
    )
    return tuple(outcomes)


def _gap_for(dimension: GapDimension, hypotheses: Sequence[Hypothesis]) -> InformationGap:
    ordered = tuple(sorted(hypotheses, key=lambda item: item.hypothesis_id))
    entity_scope = tuple(
        sorted(
            {
                entity
                for hypothesis in ordered
                for entity in (hypothesis.causal_actor, *hypothesis.manifestations)
            },
            key=lambda entity: entity.canonical,
        )
    )
    capabilities = capabilities_for(dimension)
    resolvability = (
        GapResolvability.RESOLVABLE
        if capabilities
        else GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS
    )
    tools = tuple(sorted({capability.name for capability in capabilities}))
    missing = _missing_fact(dimension)
    return InformationGap(
        gap_id=_stable_gap_id(dimension, ordered, [entity.canonical for entity in entity_scope]),
        dimension=dimension,
        hypothesis_ids=tuple(hypothesis.hypothesis_id for hypothesis in ordered),
        entity_scope=entity_scope,
        known_facts=_known_facts(ordered, dimension),
        missing_fact=missing,
        required_relation=_RELATIONS.get(dimension),
        discriminating_outcomes=_outcomes(ordered, missing),
        candidate_tools=tools,
        evidence_refs=_evidence_refs(ordered),
        priority=1,
        resolvability=resolvability,
        rationale=(
            "The hypotheses share the currently observed causal support; this bounded "
            "fact could distinguish them."
        ),
    )


def derive_information_gaps(
    hypotheses: Sequence[Hypothesis], resolution: ResolutionTrace
) -> tuple[InformationGap, ...]:
    """Derive stable gaps for ambiguous or evidence-poor diagnoses only."""
    if resolution.state not in {Resolution.AMBIGUOUS, Resolution.INSUFFICIENT_EVIDENCE}:
        return ()
    by_id = {hypothesis.hypothesis_id: hypothesis for hypothesis in hypotheses}
    selected_ids = tuple(
        resolution.leading_hypothesis_ids
        if resolution.state is Resolution.AMBIGUOUS
        else resolution.plausible_hypotheses
    )
    selected = tuple(by_id[item] for item in selected_ids if item in by_id)
    if not selected:
        return ()
    dimensions = _dimensions(selected)
    gaps = tuple(_gap_for(dimension, selected) for dimension in dimensions)
    return tuple(sorted(gaps, key=lambda gap: (gap.dimension.value, gap.gap_id)))


__all__ = [
    "CAPABILITIES",
    "capabilities_for",
    "derive_information_gaps",
]
