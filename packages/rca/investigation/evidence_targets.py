"""Registered decoder inputs for evidence acquisition, independent of labels.

Specificity is ordinal: unmapped (0), scoped observation (1), structured
mechanism/relation/discovery inputs (2), exact atomic rule inputs (3). It is not predicted confidence,
resolution probability or the number of hypotheses a query might eliminate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from packages.rca.model import (
    Diagnosis,
    GapDimension,
    InvestigationDiscriminatorKind,
    InvestigationEvidenceDeficit,
    InvestigationEvidenceTarget,
    InvestigationTransitionCertificate,
    RootSupportStatus,
)

if TYPE_CHECKING:
    from packages.rca.investigation.candidates import ObservationCandidate


def candidate_evidence_targets(
    candidate: ObservationCandidate,
    *,
    certificates: Sequence[InvestigationTransitionCertificate] = (),
    diagnosis: Diagnosis | None = None,
) -> tuple[InvestigationEvidenceTarget, ...]:
    """Describe possible observations from an already-authorized exact candidate."""
    grouped: dict[str, InvestigationEvidenceTarget] = {}
    for discriminator in candidate.discriminators:
        dimension = discriminator.dimension
        capability = candidate.capability
        kind: Literal["DISCOVERY", "MECHANISM", "RELATION", "OBSERVATION", "UNMAPPED"] = "UNMAPPED"
        family = "unmapped"
        required: tuple[str, ...] = ()
        consumers: tuple[str, ...] = ()
        reason: Literal["REQUEST_RELATION_RECONSTRUCTION"] | None = None
        if discriminator.kind is InvestigationDiscriminatorKind.DISCOVERY:
            if (
                capability in {"history", "events", "incident_events", "incident_changes"}
                and dimension in {GapDimension.EVENT_SEQUENCE, GapDimension.CHANGE_TIMING}
                and discriminator.unknown_slots
                and discriminator.expected_fact_families
                and discriminator.possible_outcomes
            ):
                kind = "DISCOVERY"
                family = "+".join(sorted(set(discriminator.expected_fact_families)))
                required = discriminator.unknown_slots
                consumers = ("investigation.normalize_observation", "engine.rebuild_hypotheses")
        elif capability in {"history", "incident_changes"} and dimension in {
            GapDimension.CHANGE_TIMING,
            GapDimension.CONFIG_DIFFERENCE,
        }:
            kind = "MECHANISM"
            family = "object_change"
            required = (
                "ordered object versions for the exact target and query window",
                "changed desired-state fields with object identity and provenance",
                "change timing; execution and incident effect remain separately assessed",
            )
            consumers = ("normalizers._history_findings", "signals.change_findings")
        elif capability in {"events", "incident_events"} and dimension in {
            GapDimension.EVENT_SEQUENCE,
            GapDimension.FAILURE_ONSET,
            GapDimension.AUTOSCALING_TARGET_STATE,
            GapDimension.ENTITY_STATE,
        }:
            kind = "MECHANISM"
            family = "actor_event"
            required = (
                "event reason and involved object identity",
                "recorded event interval and provenance",
                "event's temporal and ownership relationship to the claim",
            )
            consumers = ("normalizers._event_findings", "signals.autoscaling_findings")
        elif capability == "runtime_traces" and dimension in {
            GapDimension.DEPENDENCY_HEALTH,
            GapDimension.TOPOLOGY_RELATION,
        }:
            kind = "RELATION"
            family = "runtime_dependency_non_success"
            required = (
                "direct CLIENT/SERVER parent relationship in one trace",
                "callee outcome and operation timestamps",
                "explicit workload bindings; identity and incident scope validated separately",
            )
            consumers = (
                "normalizers._trace_findings",
                "runtime_evidence.derive_runtime_trace_call_facts",
            )
            if candidate.query.source_log_ids:
                family = "log_bound_request_pair"
                required += (
                    "selected visible log references with request/trace/span identities",
                    "exact request, peer, error and method agreement with the returned pair",
                )
                consumers += ("trace_request_pairs.pair_deficits",)
                reason = "REQUEST_RELATION_RECONSTRUCTION"
        elif capability == "logs" and dimension in {
            GapDimension.DEPENDENCY_HEALTH,
            GapDimension.LOG_ERROR_PATTERN,
            GapDimension.TOPOLOGY_RELATION,
        }:
            kind = "OBSERVATION"
            family = "dependency_error"
            required = (
                "caller and peer error pattern at a recorded time",
                "log provenance; request identity needed separately for transaction binding",
            )
            consumers = ("normalizers._log_findings", "signals.dependency_findings")
        elif capability in {"resource_pressure", "traffic"} and dimension in {
            GapDimension.RESOURCE_PRESSURE,
            GapDimension.METRIC_BASELINE,
            GapDimension.METRIC_CHANGE,
        }:
            kind = "MECHANISM"
            family = "resource_pressure" if capability == "resource_pressure" else "traffic_change"
            required = (
                "target-scoped measured values and timestamps",
                "baseline/window and sample provenance",
                "complete coverage assessed by existing rules, never inferred from a normal sample",
            )
            consumers = ("normalizers._metric_findings",)
        elif capability == "history":
            kind = "OBSERVATION"
            family = "object_history"
            required = ("target-scoped object versions with lifecycle and provenance",)
            consumers = ("normalizers._history_findings",)
        key = f"investigation.evidence-target.{family}.v1"
        existing = grouped.get(key)
        gap_ids = tuple(
            sorted({discriminator.gap_id, *(existing.source_gap_ids if existing else ())})
        )
        grouped[key] = InvestigationEvidenceTarget(
            contract_id=key,
            source_gap_ids=gap_ids,
            kind=kind,
            fact_family=family,
            required_observations=tuple(
                dict.fromkeys((*(existing.required_observations if existing else ()), *required))
            ),
            consumer_ids=tuple(
                dict.fromkeys((*(existing.consumer_ids if existing else ()), *consumers))
            ),
            reacquisition_reason=reason,
        )
    for certificate in certificates:
        if (
            certificate.capability != candidate.capability
            or certificate.target != candidate.target
            or certificate.source_gap_id not in candidate.gap_ids
        ):
            continue
        key = f"investigation.rule-input.{certificate.normalizer_rule_id}.{certificate.transition_rule_id}"
        existing = grouped.get(key)
        grouped[key] = InvestigationEvidenceTarget(
            contract_id=key,
            source_gap_ids=tuple(
                sorted({certificate.source_gap_id, *(existing.source_gap_ids if existing else ())})
            ),
            kind="MECHANISM",
            fact_family=certificate.observation_fact_family,
            required_observations=certificate.preconditions,
            consumer_ids=(certificate.normalizer_rule_id, certificate.transition_rule_id),
        )
    if diagnosis is not None:
        from packages.rca.investigation.preconditions import candidate_precondition_targets

        for target in candidate_precondition_targets(candidate, diagnosis):
            grouped[target.contract_id] = target
    return tuple(grouped[key] for key in sorted(grouped))


def evidence_target_specificity(
    candidate: ObservationCandidate, diagnosis: Diagnosis | None = None
) -> int:
    """State multiplicity and IDs never contribute to acquisition utility."""
    return max(
        (
            3
            if target.acquisition_class == "DIRECT"
            else 0
            if target.kind == "UNMAPPED"
            else 1
            if target.kind == "OBSERVATION"
            else 2
            for target in candidate_evidence_targets(candidate, diagnosis=diagnosis)
        ),
        default=0,
    )


def recorded_evidence_deficits(
    diagnosis: Diagnosis, candidates: Sequence[ObservationCandidate]
) -> tuple[InvestigationEvidenceDeficit, ...]:
    """Retain actual rule deficits; candidate availability is only a partial route.

    Generic telemetry never claims to discharge an entire execution/coverage
    precondition. A rule reason is not parsed into an invented observation.
    """
    trace = diagnosis.resolution_trace
    if trace is None:
        return ()
    from packages.rca.investigation.preconditions import (
        candidate_precondition_targets,
        frontier_input_contract,
    )

    routed_targets = {
        candidate.candidate_id: candidate_precondition_targets(candidate, diagnosis)
        for candidate in candidates
    }
    result: dict[tuple[str, str, str, str], InvestigationEvidenceDeficit] = {}
    for audit in trace.hypothesis_audits:
        for support in audit.root_support:
            if support.status is RootSupportStatus.NOT_FIRED:
                # A summary rule reason is preserved literally. Without an
                # atomic witness precondition contract, do not invent a route.
                for reason in support.reasons:
                    key = ("WITNESS", audit.hypothesis_id, support.rule_id, reason)
                    result[key] = InvestigationEvidenceDeficit(
                        source_kind="WITNESS",
                        source_id=audit.hypothesis_id,
                        rule_id=support.rule_id,
                        missing_precondition=reason,
                        routing="NO_REGISTERED_TOOL_ROUTE",
                        evidence_ids=support.decisive_evidence_ids,
                    )
            for witness in support.witnesses:
                for missing in witness.missing:
                    key = ("WITNESS", audit.hypothesis_id, witness.rule_id, missing)
                    result[key] = InvestigationEvidenceDeficit(
                        source_kind="WITNESS",
                        source_id=audit.hypothesis_id,
                        rule_id=witness.rule_id,
                        missing_precondition=missing,
                        routing="NO_REGISTERED_TOOL_ROUTE",
                        evidence_ids=witness.evidence_ids,
                    )
    for answer in trace.frontier_answers:
        if answer.state != "OPEN" or not answer.blocked_reason:
            continue
        contract = frontier_input_contract(
            answer.rule_id, answer.blocked_reason, answer.rule_version
        )
        bound_ids = tuple(
            sorted(
                candidate.candidate_id
                for candidate in candidates
                if any(
                    target.contract_id
                    == (f"investigation.precondition.{answer.rule_id}.{answer.blocked_reason}.v1")
                    and answer.alternative_id in target.source_alternative_ids
                    for target in routed_targets[candidate.candidate_id]
                )
            )
        )
        key = ("FRONTIER", answer.alternative_id, answer.rule_id, answer.blocked_reason)
        result[key] = InvestigationEvidenceDeficit(
            source_kind="FRONTIER",
            source_id=answer.alternative_id,
            rule_id=answer.rule_id,
            missing_precondition=answer.blocked_reason,
            candidate_ids=bound_ids,
            routing=(
                "DIRECT_INPUT_ROUTE"
                if contract and contract[0] == "DIRECT" and bound_ids
                else "OBSERVATION_ROUTE_ONLY"
                if bound_ids
                else "NO_REGISTERED_TOOL_ROUTE"
            ),
            acquisition_class=contract[0] if contract else "UNMAPPED",
            required_observations=contract[2] if contract else (),
            evidence_ids=answer.evidence_ids,
        )
    return tuple(result[key] for key in sorted(result))
