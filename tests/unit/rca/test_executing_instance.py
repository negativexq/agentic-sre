"""The executing instance beside the root cause (m21 contract §17): only fired execution witnesses name it."""

from __future__ import annotations

from test_fault_execution_support import EXP, OFF, full, source

from packages.rca.engine import build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest


def test_a_schedules_executing_instance_is_the_experiment_its_witness_names_on_the_exact_pod() -> (
    None
):
    diagnosis = diagnose_case(build_case(source(full())), config=OFF)
    assert diagnosis.root_cause is not None and diagnosis.root_cause.kind == "Schedule"
    (instance,) = diagnosis.executing_instances
    assert instance.actor == diagnosis.root_cause
    assert instance.instance.canonical == EXP
    assert instance.target is not None and instance.target.name == "checkout-rs-abcde"
    assert instance.started_at is not None and instance.ended_at is not None
    assert instance.started_at < instance.ended_at


def test_the_field_changes_no_digest() -> None:
    diagnosis = diagnose_case(build_case(source(full())), config=OFF)
    assert diagnosis.executing_instances
    without = diagnosis.model_copy(update={"executing_instances": ()})
    assert diagnosis_epistemic_digest(diagnosis) == diagnosis_epistemic_digest(without)
