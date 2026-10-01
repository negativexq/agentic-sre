"""Reusable onset-independent case inputs and the assessed onset (M21 timing contract)."""

from __future__ import annotations

from datetime import timedelta

from rca_builders import at
from test_fault_execution import TARGET, _chaos_case, ev

from packages.rca.engine import (
    Case,
    EngineConfig,
    build_case,
    diagnose_case,
    prepare_case_inputs,
)

EVENTS = [
    ev("chaos/Schedule/checkout-delay", "Spawned", 1, uid="s1"),
    ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 1, uid="c1", message=TARGET),
    ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 40, uid="c2", message=TARGET),
]


def dump(case: Case) -> str:
    return diagnose_case(case).model_dump_json()


def test_reusing_prepared_inputs_changes_nothing() -> None:
    source = _chaos_case(EVENTS)
    assert dump(build_case(source)) == dump(build_case(source, inputs=prepare_case_inputs(source)))


def test_assessed_onset_moves_only_the_onset_and_reference_time() -> None:
    source = _chaos_case(EVENTS)
    base = build_case(source)
    moved = build_case(source, assessed_onset=at(50))
    assert base.symptoms.onset == at(6) and moved.symptoms.onset == at(50)
    assert moved.symptoms.reference_time == at(50)
    for field in ("services", "namespaces", "alert_names", "alert_observation_start"):
        assert getattr(base.symptoms, field) == getattr(moved.symptoms, field)
    assert moved.context.symptom_entities == base.context.symptom_entities


def test_assessing_the_onset_of_record_is_the_identity() -> None:
    # The decision is identical; only the timing record differs (an assessed case never
    # assesses itself), so compare with timing assessment off on both sides.
    off = EngineConfig(timing_stability=False)
    source = _chaos_case(EVENTS)
    base = build_case(source)
    assert base.symptoms.onset is not None
    assessed = build_case(source, assessed_onset=base.symptoms.onset)
    assert (
        diagnose_case(assessed, config=off).model_dump_json()
        == diagnose_case(base, config=off).model_dump_json()
    )


def test_shared_inputs_give_the_same_result_as_fresh_reads_for_every_onset() -> None:
    source = _chaos_case(EVENTS)
    inputs = prepare_case_inputs(source)
    for minutes in (2, 20, 50):
        onset = at(0) + timedelta(minutes=minutes)
        assert dump(build_case(source, assessed_onset=onset, inputs=inputs)) == dump(
            build_case(source, assessed_onset=onset)
        )


def test_the_case_remembers_what_re_assessment_needs() -> None:
    source = _chaos_case(EVENTS)
    case = build_case(source)
    assert case.extra_findings == () and case.inputs is not None
    assert case.inputs.alerts == tuple(source.alerts())
