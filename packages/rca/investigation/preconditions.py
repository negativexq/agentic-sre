"""Exact recorded rule-input routes; planning only, independent of fault labels."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from packages.rca.frontier import DEPENDENCY_DECLARED_UNUSED_RULE, UNCHANGED_CONFIGURATION_RULE
from packages.rca.model import Diagnosis, EntityRef, InvestigationEvidenceTarget

if TYPE_CHECKING:
    from packages.rca.investigation.candidates import ObservationCandidate


def frontier_input_contract(
    rule_id: str, reason: str, rule_version: str = "v1"
) -> tuple[Literal["DIRECT", "PARTIAL"], str, tuple[str, ...]] | None:
    if rule_version != "v1":
        return None
    if rule_id == UNCHANGED_CONFIGURATION_RULE:
        if reason == "NO_LISTING":
            return "DIRECT", "history", ("object listing at or before incident onset",)
        if reason == "NO_WRITE_RECORD":
            return (
                "PARTIAL",
                "history",
                ("creationTimestamp and every managedFields time on the configuration listing",),
            )
    if rule_id == DEPENDENCY_DECLARED_UNUSED_RULE and reason in {
        "CALLER_NOT_TRACED",
        "CALLS_NOT_TRACED",
        "TRACE_READ_FAILED",
    }:
        return (
            "PARTIAL",
            "runtime_traces",
            (
                "caller-scoped inbound and outbound spans at or before incident onset",
                "read completeness remains separately assessed; a sample never proves absence",
            ),
        )
    return None


def candidate_precondition_targets(
    candidate: ObservationCandidate, diagnosis: Diagnosis
) -> tuple[InvestigationEvidenceTarget, ...]:
    trace = diagnosis.resolution_trace
    onset = diagnosis.symptoms.onset
    if trace is None or onset is None:
        return ()
    query = candidate.query
    if (
        query.start is None
        or query.end is None
        or any(at.utcoffset() is None for at in (query.start, query.end, onset))
        or not query.start <= onset <= query.end
    ):
        return ()
    alternatives = {item.alternative_id: item for item in diagnosis.structural_alternatives}
    result = []
    for answer in trace.frontier_answers:
        if answer.state != "OPEN" or not answer.blocked_reason:
            continue
        contract = frontier_input_contract(
            answer.rule_id, answer.blocked_reason, answer.rule_version
        )
        alternative = alternatives.get(answer.alternative_id)
        if (
            contract is None
            or alternative is None
            or answer.alternative_id not in candidate.alternative_ids
            or candidate.capability != contract[1]
            or alternative.role
            != (
                "configuration_source"
                if answer.rule_id == UNCHANGED_CONFIGURATION_RULE
                else "dependency"
            )
        ):
            continue
        targets: Sequence[EntityRef] = (
            (alternative.actor,)
            if answer.rule_id == UNCHANGED_CONFIGURATION_RULE
            else tuple(
                entity
                for entity in alternative.affected_entities
                if entity.kind in {"Deployment", "StatefulSet", "DaemonSet", "Pod"}
            )
        )
        if candidate.target not in targets:
            continue
        # Bind only to discriminator gaps actually carried by the candidate.
        gap_ids = tuple(
            sorted(
                {item.gap_id for item in candidate.discriminators}
                & set(candidate.gap_ids)
                & {
                    gap.gap_id
                    for gap in diagnosis.information_gaps
                    if answer.alternative_id in gap.alternative_ids
                    and gap.resolvability.value == "RESOLVABLE"
                    and any(
                        query.capability == candidate.capability
                        and query.target == candidate.target
                        for query in gap.authorized_queries
                    )
                }
            )
        )
        if not gap_ids:
            continue
        result.append(
            InvestigationEvidenceTarget(
                contract_id=f"investigation.precondition.{answer.rule_id}.{answer.blocked_reason}.v1",
                source_gap_ids=gap_ids,
                source_alternative_ids=(answer.alternative_id,),
                kind="OBSERVATION" if contract[1] == "history" else "RELATION",
                fact_family="object_listing" if contract[1] == "history" else "caller_trace",
                required_observations=contract[2],
                consumer_ids=(answer.rule_id,),
                acquisition_class=contract[0],
            )
        )
    grouped: dict[str, InvestigationEvidenceTarget] = {}
    for item in result:
        existing = grouped.get(item.contract_id)
        if existing is not None:
            item = item.model_copy(
                update={
                    "source_gap_ids": tuple(
                        sorted(set(item.source_gap_ids + existing.source_gap_ids))
                    ),
                    "source_alternative_ids": tuple(
                        sorted(set(item.source_alternative_ids + existing.source_alternative_ids))
                    ),
                }
            )
        grouped[item.contract_id] = item
    return tuple(grouped[key] for key in sorted(grouped))
