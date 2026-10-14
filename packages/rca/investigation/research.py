"""Truth-blind research questions over existing gaps and authorized candidates.

No telemetry reads, resolver calls, new gaps, or inferred exclusion rules. In
particular, a missing candidate is not proof that the underlying relation is false.
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.rca.investigation.candidates import ObservationCandidate
from packages.rca.investigation.evidence_targets import candidate_evidence_targets
from packages.rca.investigation.selection import (
    is_observation_candidate_admissible,
    score_observation_candidate,
)
from packages.rca.model import (
    Diagnosis,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InvestigationLedgerEntry,
    InvestigationResearchQuestion,
    InvestigationResearchRead,
    ResearchAvailability,
)


def research_question(
    gap: InformationGap,
    candidates: Sequence[ObservationCandidate],
    *,
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
    max_tool_calls_per_gap: int | None = None,
    available_capabilities: Sequence[str] | None = None,
    diagnosis: Diagnosis | None = None,
) -> InvestigationResearchQuestion:
    """Retain both supporting and contradicting outcomes without predicting either.

    The caller supplies current candidates; only exact gap/capability/target
    matches with a recorded discriminator may become research reads.
    """
    matching = tuple(
        candidate
        for candidate in sorted(candidates, key=lambda item: item.candidate_id)
        if gap.gap_id in candidate.gap_ids
        and (available_capabilities is None or candidate.capability in available_capabilities)
        and any(item.gap_id == gap.gap_id for item in candidate.discriminators)
        and any(
            query.capability == candidate.capability and query.target == candidate.target
            for query in gap.authorized_queries
        )
    )
    remaining = tuple(
        candidate
        for candidate in matching
        if is_observation_candidate_admissible(
            candidate,
            attempted_observations=attempted_observations,
            previous_investigations=previous_investigations,
        )
    )
    availability: ResearchAvailability = "AVAILABLE"
    if gap.resolvability is GapResolvability.UNRESOLVABLE_WITH_CURRENT_TOOLS:
        availability = "UNAVAILABLE_WITH_CURRENT_TOOLS"
    elif gap.resolvability is GapResolvability.ALREADY_OBSERVED:
        availability = "ALREADY_OBSERVED"
    elif (
        available_capabilities is not None
        and gap.authorized_queries
        and not any(item.capability in available_capabilities for item in gap.authorized_queries)
    ):
        availability = "UNAVAILABLE_WITH_CURRENT_TOOLS"
    elif (
        max_tool_calls_per_gap is not None
        and sum(entry.gap_id == gap.gap_id for entry in previous_investigations)
        >= max_tool_calls_per_gap
    ):
        availability = "GAP_BUDGET_EXHAUSTED"
    elif not matching:
        availability = "NO_ELIGIBLE_CANDIDATE"
    elif not remaining:
        availability = "READS_ALREADY_ATTEMPTED"

    outcomes = list(gap.discriminating_outcomes)
    # Retain recorded outcomes, including CONTRADICTS; never invent a negative
    # interpretation for absent observations or promote NO_DATA to exclusion.
    for candidate in matching:
        for discriminator in candidate.discriminators:
            if discriminator.gap_id == gap.gap_id:
                for outcome in (*discriminator.support_outcomes, *discriminator.no_data_outcomes):
                    if outcome not in outcomes:
                        outcomes.append(outcome)
    for kind in (GapOutcomeKind.NO_DATA, GapOutcomeKind.UNKNOWN):
        if not any(outcome.kind is kind for outcome in outcomes):
            outcomes.append(
                GapOutcome(kind=kind, implication="No causal conclusion from this outcome.")
            )

    reads = []
    if availability == "AVAILABLE":
        for candidate in remaining:
            discriminators = tuple(
                item for item in candidate.discriminators if item.gap_id == gap.gap_id
            )
            reads.append(
                InvestigationResearchRead(
                    candidate_id=candidate.candidate_id,
                    capability=candidate.capability,
                    target=candidate.target,
                    query=candidate.query,
                    evidence_targets=(
                        score_observation_candidate(
                            candidate=candidate, diagnosis=diagnosis
                        ).evidence_targets
                        if diagnosis
                        else candidate_evidence_targets(candidate)
                    ),
                    expected_fact_families=tuple(
                        sorted(
                            {
                                value
                                for item in discriminators
                                for value in item.expected_fact_families
                            }
                        )
                    ),
                    comparison_hypothesis_ids=tuple(
                        sorted(
                            {
                                value
                                for item in discriminators
                                for value in item.comparison_hypothesis_ids
                            }
                        )
                    ),
                    comparison_alternative_ids=tuple(
                        sorted(
                            {
                                value
                                for item in discriminators
                                for value in item.comparison_alternative_ids
                            }
                        )
                    ),
                )
            )
    return InvestigationResearchQuestion(
        gap_id=gap.gap_id,
        dimension=gap.dimension,
        missing_fact=gap.missing_fact,
        required_relation=gap.required_relation,
        hypothesis_ids=gap.hypothesis_ids,
        alternative_ids=gap.alternative_ids,
        entity_scope=gap.entity_scope,
        evidence_refs=gap.evidence_refs,
        recorded_rationale=gap.rationale,
        possible_outcomes=tuple(outcomes),
        reads=tuple(reads),
        availability=availability,
    )


def research_questions(
    diagnosis: Diagnosis,
    candidates: Sequence[ObservationCandidate],
    *,
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
    max_tool_calls_per_gap: int | None = None,
    available_capabilities: Sequence[str] | None = None,
) -> tuple[InvestigationResearchQuestion, ...]:
    """Report every recorded gap, including unavailable ones, without ranking causes."""
    return tuple(
        research_question(
            gap,
            candidates,
            attempted_observations=attempted_observations,
            previous_investigations=previous_investigations,
            max_tool_calls_per_gap=max_tool_calls_per_gap,
            available_capabilities=available_capabilities,
            diagnosis=diagnosis,
        )
        for gap in sorted(diagnosis.information_gaps, key=lambda item: item.gap_id)
    )
