"""Constructed atomic-input routes; no benchmark labels or outcomes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from packages.rca.engine import build_case, diagnose_case
from packages.rca.frontier import UNCHANGED_CONFIGURATION_RULE
from packages.rca.investigation.candidates import CandidateDiscriminator, ObservationCandidate
from packages.rca.investigation.evidence_targets import recorded_evidence_deficits
from packages.rca.investigation.selection import rank_observation_candidates
from packages.rca.model import (
    Alert,
    AuthorizedQuery,
    Diagnosis,
    EntityRef,
    FrontierAnswer,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InvestigationQuery,
    Resolution,
    ResolutionTrace,
    StructuralAlternative,
)
from packages.rca.source import InMemorySource


def fixture() -> tuple[Diagnosis, ObservationCandidate]:
    onset = datetime(2026, 1, 1, tzinfo=UTC)
    actor = EntityRef(namespace="ns", kind="ConfigMap", name="settings")
    gap = InformationGap(
        gap_id="g",
        dimension=GapDimension.CONFIG_DIFFERENCE,
        alternative_ids=("alt",),
        missing_fact="configuration listing missing",
        authorized_queries=(AuthorizedQuery(capability="history", target=actor),),
        resolvability=GapResolvability.RESOLVABLE,
    )
    answer = FrontierAnswer(
        alternative_id="alt",
        question="DID_THE_CONFIGURATION_CHANGE_INSIDE_THE_WINDOW",
        state="OPEN",
        blocked_reason="NO_LISTING",
        rule_id=UNCHANGED_CONFIGURATION_RULE,
    )
    diagnosis = diagnose_case(
        build_case(
            InMemorySource(
                name="preconditions",
                alert_coverage_start=onset - timedelta(hours=1),
                alert_items=[Alert(name="failure", service="app", starts_at=onset)],
            )
        )
    ).model_copy(
        update={
            "information_gaps": (gap,),
            "structural_alternatives": (
                StructuralAlternative(
                    alternative_id="alt",
                    actor=actor,
                    role="configuration_source",
                ),
            ),
            "resolution_trace": ResolutionTrace(
                state=Resolution.AMBIGUOUS, frontier_answers=(answer,)
            ),
        }
    )
    candidate = ObservationCandidate(
        candidate_id="bound",
        capability="history",
        target=actor,
        query=InvestigationQuery(start=onset - timedelta(minutes=5), end=onset),
        gap_ids=("g",),
        dimensions=(GapDimension.CONFIG_DIFFERENCE,),
        hypothesis_ids=(),
        alternative_ids=("alt",),
        discriminators=(
            CandidateDiscriminator(
                gap_id="g",
                dimension=GapDimension.CONFIG_DIFFERENCE,
                missing_fact=gap.missing_fact,
                support_outcomes=(
                    GapOutcome(kind=GapOutcomeKind.SUPPORTS, alternative_ids=("alt",)),
                ),
                comparison_hypothesis_ids=(),
                comparison_alternative_ids=("other",),
                no_data_outcomes=(),
            ),
        ),
    )
    return diagnosis, candidate


def test_exact_missing_listing_is_prioritized_without_authority() -> None:
    diagnosis, bound = fixture()
    generic = replace(bound, candidate_id="a-generic", alternative_ids=("other",))
    selected, _ = rank_observation_candidates(candidates=(generic, bound), diagnosis=diagnosis)
    assert selected.candidate == bound
    assert selected.utility.discrimination_value == 3
    target = next(item for item in selected.evidence_targets if item.acquisition_class == "DIRECT")
    assert target.consumer_ids == (UNCHANGED_CONFIGURATION_RULE,)
    assert not target.resolution_authority and not target.no_data_is_exclusion
    (deficit,) = recorded_evidence_deficits(diagnosis, (generic, bound))
    assert deficit.candidate_ids == (bound.candidate_id,)
    assert deficit.routing == "DIRECT_INPUT_ROUTE"


@pytest.mark.parametrize(
    "mutation", ("foreign_actor", "wrong_window", "wrong_gap", "wrong_rule", "absent_onset")
)
def test_unbound_queries_do_not_get_a_precondition_bonus(mutation: str) -> None:
    diagnosis, candidate = fixture()
    assert diagnosis.resolution_trace is not None
    if mutation == "foreign_actor":
        candidate = replace(
            candidate, target=candidate.target.model_copy(update={"namespace": "other"})
        )
    elif mutation == "wrong_window":
        assert candidate.query.end is not None
        candidate = replace(
            candidate,
            query=candidate.query.model_copy(
                update={"end": candidate.query.end - timedelta(minutes=1)}
            ),
        )
    elif mutation == "wrong_gap":
        candidate = replace(candidate, gap_ids=("foreign",))
    elif mutation == "wrong_rule":
        answer = diagnosis.resolution_trace.frontier_answers[0].model_copy(
            update={"rule_id": "unknown.rule"}
        )
        diagnosis = diagnosis.model_copy(
            update={
                "resolution_trace": diagnosis.resolution_trace.model_copy(
                    update={"frontier_answers": (answer,)}
                )
            }
        )
    else:
        diagnosis = diagnosis.model_copy(
            update={"symptoms": diagnosis.symptoms.model_copy(update={"onset": None})}
        )
    (scored,) = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)
    assert scored.utility.discrimination_value == 2
    assert not any(item.acquisition_class == "DIRECT" for item in scored.evidence_targets)


def test_duplicate_conditions_cannot_increase_value_and_coverage_is_not_routable() -> None:
    diagnosis, candidate = fixture()
    assert diagnosis.resolution_trace is not None
    answer = diagnosis.resolution_trace.frontier_answers[0]
    duplicate = diagnosis.model_copy(
        update={
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={"frontier_answers": (answer,) * 100}
            )
        }
    )
    (scored,) = rank_observation_candidates(candidates=(candidate,), diagnosis=duplicate)
    assert scored.utility.discrimination_value == 3
    assert (
        len([item for item in scored.evidence_targets if item.acquisition_class == "DIRECT"]) == 1
    )
    incomplete = diagnosis.model_copy(
        update={
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={
                    "frontier_answers": (answer.model_copy(update={"blocked_reason": "WATCH_GAP"}),)
                }
            )
        }
    )
    (deficit,) = recorded_evidence_deficits(incomplete, (candidate,))
    assert deficit.acquisition_class == "UNMAPPED"
    assert not deficit.required_observations


def test_write_metadata_is_partial_and_not_a_direct_bonus() -> None:
    diagnosis, candidate = fixture()
    assert diagnosis.resolution_trace is not None
    answer = diagnosis.resolution_trace.frontier_answers[0].model_copy(
        update={"blocked_reason": "NO_WRITE_RECORD"}
    )
    diagnosis = diagnosis.model_copy(
        update={
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={"frontier_answers": (answer,)}
            )
        }
    )
    (scored,) = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)
    assert scored.utility.discrimination_value == 2
    (deficit,) = recorded_evidence_deficits(diagnosis, (candidate,))
    assert deficit.acquisition_class == "PARTIAL"
    assert deficit.routing == "OBSERVATION_ROUTE_ONLY"


@pytest.mark.parametrize("has_listing", (False, True))
def test_selected_route_supplies_only_the_missing_input(has_listing: bool) -> None:
    from packages.rca.evidence_coverage import EvidenceCoverage
    from packages.rca.frontier import unchanged_configuration
    from packages.rca.investigation.environment import SourceInvestigationBackend
    from packages.rca.investigation.selection import candidate_to_action
    from packages.rca.investigation.tools import HistoryTool
    from packages.rca.model import ObjectVersion

    diagnosis, candidate = fixture()
    (selected,) = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)
    action = candidate_to_action(selected, diagnosis)
    assert action is not None and action.capability == "history"
    assert action.target == candidate.target and action.query == candidate.query
    assert candidate.query.end is not None
    listing = ObjectVersion(
        entity=candidate.target,
        body={},
        observed_at=candidate.query.end,
        evidence_id="listing:observed",
    )
    source = InMemorySource(name="listing-history", versions=[listing] if has_listing else [])
    case = build_case(source)
    gap = diagnosis.information_gaps[0]
    observation = HistoryTool(backend=SourceInvestigationBackend(source)).execute_query(
        case,
        gap,
        candidate.target,
        candidate.query,
    )
    assert observation.evidence_refs == ((listing.evidence_id,) if has_listing else ())
    assert diagnosis.symptoms.onset is not None
    assert candidate.query.start is not None and candidate.query.end is not None
    answer = unchanged_configuration(
        diagnosis.structural_alternatives[0],
        {candidate.target: [listing]} if has_listing else {},
        EvidenceCoverage(starts_at=candidate.query.start, window_end=candidate.query.end),
        diagnosis.symptoms.onset,
    )
    assert answer is not None
    assert answer.state == "OPEN"
    assert answer.blocked_reason == ("NO_WRITE_RECORD" if has_listing else "NO_LISTING")


@pytest.mark.parametrize(
    "update", ({"state": "ANSWERED_NO_CHANGE_IN_WINDOW"}, {"rule_version": "unknown"})
)
def test_answered_or_unregistered_rule_revision_has_no_direct_bonus(update: dict[str, str]) -> None:
    diagnosis, candidate = fixture()
    assert diagnosis.resolution_trace is not None
    answer = diagnosis.resolution_trace.frontier_answers[0].model_copy(update=update)
    diagnosis = diagnosis.model_copy(
        update={
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={"frontier_answers": (answer,)}
            )
        }
    )
    (scored,) = rank_observation_candidates(candidates=(candidate,), diagnosis=diagnosis)
    assert scored.utility.discrimination_value == 2
    assert not any(item.acquisition_class == "DIRECT" for item in scored.evidence_targets)


def test_same_rule_reason_on_another_actor_does_not_borrow_the_route() -> None:
    diagnosis, candidate = fixture()
    assert diagnosis.resolution_trace is not None
    alternative = diagnosis.structural_alternatives[0]
    other = alternative.model_copy(
        update={
            "alternative_id": "another",
            "actor": alternative.actor.model_copy(update={"name": "other-settings"}),
        }
    )
    answer = diagnosis.resolution_trace.frontier_answers[0]
    diagnosis = diagnosis.model_copy(
        update={
            "structural_alternatives": (alternative, other),
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={
                    "frontier_answers": (
                        answer,
                        answer.model_copy(update={"alternative_id": "another"}),
                    ),
                }
            ),
        }
    )
    by_id = {item.source_id: item for item in recorded_evidence_deficits(diagnosis, (candidate,))}
    assert by_id["alt"].candidate_ids == (candidate.candidate_id,)
    assert by_id["another"].candidate_ids == ()
    assert by_id["another"].routing == "NO_REGISTERED_TOOL_ROUTE"
