"""What ran, which instance and how firm the timing is, beside the leader (docs/ui/product-contract.md, D3)."""

from __future__ import annotations

from datetime import UTC, datetime

from test_fault_execution_support import EXP, OFF, full, source

from apps.control_plane.console.mappers import diagnosis_view
from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import (
    Diagnosis,
    InstanceResolution,
    OnsetOutcome,
    OnsetUncertainty,
    StatusDriver,
    TimingAssessment,
    TimingStability,
    WithheldAuthority,
)
from packages.report import ReportSnapshot, build_report, to_markdown, to_pdf


def diagnosis() -> Diagnosis:
    return diagnose_case(build_case(source(full())), config=OFF)


def snapshot(d: Diagnosis) -> ReportSnapshot:
    return build_report(
        incident_id="i1",
        title="RequestLatency",
        severity="WARNING",
        status="OPEN",
        diagnosis=d,
        run_id="r1",
        alert_fired=None,
        incident_opened=None,
        diagnosed_at=None,
        phases=(),
        evidence_count=0,
        generated_at=datetime(2026, 10, 8, tzinfo=UTC),
    )


def report(d: Diagnosis) -> str:
    return to_markdown(snapshot(d))


def with_families(d: Diagnosis, resolution: InstanceResolution) -> Diagnosis:
    assert d.resolution_trace is not None
    families = tuple(
        f.model_copy(update={"instance_resolution": resolution})
        for f in d.resolution_trace.causal_families
    )
    trace = d.resolution_trace.model_copy(update={"causal_families": families})
    return d.model_copy(update={"resolution_trace": trace})


def with_timing(d: Diagnosis, timing: TimingAssessment) -> Diagnosis:
    assert d.resolution_trace is not None
    return d.model_copy(
        update={"resolution_trace": d.resolution_trace.model_copy(update={"timing": timing})}
    )


def test_the_shown_leader_carries_what_ran_on_which_pod_and_when() -> None:
    view = diagnosis_view(diagnosis())
    (item,) = view.executing_instances
    assert item.instance == EXP and item.target == "shop/Pod/checkout-rs-abcde"
    assert item.started_at is not None and item.ended_at is not None
    text = report(diagnosis())
    assert f"Executed by {EXP} on shop/Pod/checkout-rs-abcde" in text
    assert to_pdf(snapshot(diagnosis()))  # the PDF renders the same lines


def test_nothing_is_shown_for_a_leader_that_is_not_shown() -> None:
    hidden = diagnosis().model_copy(
        update={
            "leading_actor_display": "NOT_ESTABLISHED",
            "leading_actor_established": False,
            "leading_actor_withheld_reason": "NO_EVIDENCE_NEAR_ONSET",
        }
    )
    view = diagnosis_view(hidden)
    assert view.executing_instances == () and view.leader_instance_resolution is None
    text = report(hidden)
    assert "Executed by" not in text
    assert not any(
        s in text for s in ("Exact instance", "Several instances", "instance not determined")
    )


def test_without_an_execution_witness_the_line_is_absent_not_missing() -> None:
    bare = diagnosis().model_copy(update={"executing_instances": ()})
    assert diagnosis_view(bare).executing_instances == ()
    assert "Executed by" not in report(bare)


def test_an_unknown_time_is_said_and_never_guessed() -> None:
    d = diagnosis()
    unknown = d.model_copy(
        update={
            "executing_instances": tuple(
                i.model_copy(update={"started_at": None, "ended_at": None})
                for i in d.executing_instances
            )
        }
    )
    assert "time unknown" in report(unknown)
    started = d.model_copy(
        update={
            "executing_instances": tuple(
                i.model_copy(update={"ended_at": None}) for i in d.executing_instances
            )
        }
    )
    assert "end unknown" in report(started)


def test_each_instance_resolution_reads_as_stated() -> None:
    d = diagnosis()
    exact = diagnosis_view(with_families(d, InstanceResolution.EXACT))
    assert exact.leader_instance_resolution == "EXACT"
    several = with_families(d, InstanceResolution.MULTIPLE_VIABLE)
    assert diagnosis_view(several).leader_instance is None
    assert "Several instances remain possible" in report(several)
    unknown = with_families(d, InstanceResolution.UNKNOWN)
    assert "Exact instance not determined" in report(unknown)


def test_timing_is_carried_as_assessed_and_unassessed_stays_a_non_judgement() -> None:
    d = diagnosis()
    view = diagnosis_view(d)
    assert view.timing is None or view.timing.status == "UNASSESSED"
    onset = datetime(2026, 10, 8, 12, tzinfo=UTC)
    sensitive = with_timing(
        d,
        TimingAssessment(
            uncertainty=OnsetUncertainty(),
            status=TimingStability.SENSITIVE,
            outcomes=(
                OnsetOutcome(
                    onset=onset,
                    diagnosis_status="SUPPORTED_CAUSE",
                    drivers=(
                        StatusDriver(
                            hypothesis_key="k",
                            actor="shop/Pod/a",
                            change="ENTERS",
                            reason="FORMS_ONLY_UNDER_THIS_ONSET",
                        ),
                    ),
                ),
            ),
            withheld=(
                WithheldAuthority(
                    hypothesis_key="k",
                    actor="shop/Pod/a",
                    authority="STRONG_MECHANISM",
                    relations=("execution",),
                ),
            ),
        ),
    )
    timing = diagnosis_view(sensitive).timing
    assert timing is not None and timing.status == "SENSITIVE"
    assert timing.withheld[0].authority == "STRONG_MECHANISM"
    assert (timing.drivers[0].actor, timing.drivers[0].onset) == ("shop/Pod/a", onset)


def test_a_standalone_experiment_is_said_to_have_executed_on_its_pod_not_by_itself() -> None:
    d = diagnosis()
    own = d.model_copy(
        update={
            "executing_instances": tuple(
                i.model_copy(update={"instance": i.actor}) for i in d.executing_instances
            )
        }
    )
    text = report(own)
    assert "Executed on shop/Pod/checkout-rs-abcde" in text and "Executed by" not in text
