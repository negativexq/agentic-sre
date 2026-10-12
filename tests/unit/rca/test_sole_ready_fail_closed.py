"""§28: an unknown selected replica must not become proof of a sole server."""

from itertools import permutations

import pytest
from rca_builders import at
from scripts.sole_ready_policy import baseline_sole_ready, corrected_sole_ready
from test_declared_symptom_coverage import OTHER, OWNED_BY, POD, SERVICE, ready, witness

from packages.rca.model import PodStatusObservation


def status(minutes: float, value: bool | None, since: float | None = None) -> PodStatusObservation:
    return ready(OTHER, minutes, value=False).model_copy(
        update={
            "ready": value,
            "ready_since": at(since) if since is not None else None,
        }
    )


@pytest.mark.parametrize(
    "other",
    [
        [],
        [status(9, None)],
        [status(11, False)],
        [status(13, False)],
        [status(5, False), status(9, None)],
        [status(9, False), status(11, None)],
        [status(9, False), status(13, None, since=11)],
        [status(9, False), status(9, None)],
    ],
    ids=[
        "absent",
        "unknown-at-start",
        "false-only-inside",
        "false-only-after",
        "latest-is-unknown",
        "unknown-inside",
        "unknown-backdated",
        "conflicting-at-start",
    ],
)
def test_unknown_other_replica_denies_coverage(other: list[PodStatusObservation]) -> None:
    observations = [ready(POD, 9), *other]
    assert not corrected_sole_ready(POD, {POD, OTHER}, observations, at(10), at(12))
    # These characterize the unsafe authority in the old predicate.
    assert baseline_sole_ready(POD, {POD, OTHER}, observations, at(10), at(12))


@pytest.mark.parametrize(
    "other",
    [
        [status(9, False)],
        [status(5, True), status(9, False)],
        [status(9, False), status(13, True, since=13)],
        [status(9, False), status(13, None)],
    ],
)
def test_known_false_at_start_and_no_overlapping_positive_or_unknown_still_holds(
    other: list[PodStatusObservation],
) -> None:
    assert corrected_sole_ready(POD, {POD, OTHER}, [ready(POD, 9), *other], at(10), at(12))


def test_later_positive_record_reaching_into_interval_still_blocks() -> None:
    observations = [ready(POD, 9), status(9, False), status(13, True, since=11)]
    assert not corrected_sole_ready(POD, {POD, OTHER}, observations, at(10), at(12))


def test_order_and_conflicting_observation_ties_never_grant_authority() -> None:
    observations = [ready(POD, 9), status(9, False), status(9, None)]
    for ordered in permutations(observations):
        assert not corrected_sole_ready(POD, {POD, OTHER}, ordered, at(10), at(12))


def test_single_pod_and_direct_witness_scope_are_preserved() -> None:
    assert corrected_sole_ready(POD, {POD}, [ready(POD, 9)], at(10), at(12))
    assert not corrected_sole_ready(POD, {POD}, [], at(10), at(12))


def test_service_bridge_disappears_when_selected_other_replica_has_no_record() -> None:
    from unittest.mock import patch

    from packages.rca import resolution

    with patch.object(resolution, "_sole_ready", corrected_sole_ready):
        covered = resolution._covered_symptoms(
            [witness()], OWNED_BY, {SERVICE: {POD, OTHER}}, [ready(POD, 9)]
        )
    assert SERVICE not in covered
    assert POD in covered
    assert OWNED_BY[OWNED_BY[POD]] in covered


def test_direct_service_witness_does_not_invent_coverage_of_an_unknown_single_pod() -> None:
    from unittest.mock import patch

    from packages.rca import resolution

    with patch.object(resolution, "_sole_ready", corrected_sole_ready):
        covered = resolution._covered_symptoms(
            [witness(symptom=SERVICE)], OWNED_BY, {SERVICE: {POD, OTHER}}, [ready(POD, 9)]
        )
    assert SERVICE in covered  # directly witnessed scope is retained
    assert POD not in covered
    assert OWNED_BY[OWNED_BY[POD]] not in covered
