"""M19-6.9: traffic stays off through pre-history and no incident may open before T0."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from test_product_actions import FakeEvidence
from test_product_runner import ROOT, Commands, _control, _Process

from packages.evals.product.actions import IncidentRecord, PatchService
from packages.evals.product.live import LiveBackend, LiveEvidenceReader
from packages.evals.product.runner import (
    DRY_RUN_EPOCH,
    PreHistoryIncident,
    ProductRunner,
    RecordingBackend,
    RunnerConfig,
    RunStatus,
    Stage,
)
from packages.evals.product.spec import Expectation, Phase, ProductScenario

E = DRY_RUN_EPOCH
MICROSECOND = timedelta(microseconds=1)


def _scenario(*phases: Phase) -> ProductScenario:
    return ProductScenario(scenario_id="s", phases=tuple(phases), expectation=Expectation())


PRE_HISTORY = Phase(timedelta(minutes=-10), ())  # T0 = E + 10 min on the virtual clock
T0 = E + timedelta(minutes=10)


def _kinds(backend: RecordingBackend) -> list[Any]:
    return [event[0] if isinstance(event[0], Stage) else event[:2] for event in backend.events]


def test_traffic_is_off_through_pre_history_and_on_from_t0() -> None:
    evidence = FakeEvidence(journal=[])
    backend = RecordingBackend(evidence)
    selector = PatchService("order-service", {"app": "x"})
    ProductRunner(backend, RunnerConfig(evidence_timeout=timedelta(0))).run(
        _scenario(
            Phase(timedelta(minutes=-25), (selector,)),
            Phase(timedelta(0), (PatchService("payment-service", {"app": "y"}),)),
        )
    )
    events = backend.events
    off = events.index(("set_traffic", False))
    first_pre = next(i for i, event in enumerate(events) if event[0] == "patch_service_selector")
    assert off < first_pre  # off before pre-history starts
    # The pre-history action fails verification (no journal), so T0 is never reached:
    assert ("set_traffic", True) not in events


def test_the_t0_guard_runs_before_traffic_and_before_t0_actions() -> None:
    order: list[str] = []

    class Tracing(FakeEvidence):
        def incidents(self) -> Sequence[IncidentRecord]:
            order.append("incidents")
            return super().incidents()

    class Recording(RecordingBackend):
        def set_traffic(self, enabled: bool) -> None:
            order.append(f"traffic {enabled}")
            super().set_traffic(enabled)

    backend = Recording(Tracing())
    result = ProductRunner(backend).run(_scenario(PRE_HISTORY, Phase(timedelta(0), ())))
    assert result.status is RunStatus.RUN_OK
    assert order == ["traffic False", "incidents", "traffic True", "traffic False"]
    waits = [event[1] for event in backend.events if event[0] == "wait"]
    assert sum(waits, timedelta(0)) == timedelta(minutes=10)  # the guard runs at T0


def test_without_pre_history_t0_is_the_timeline_start() -> None:
    backend = RecordingBackend()
    assert ProductRunner(backend).run(_scenario()).status is RunStatus.RUN_OK
    toggles = [event for event in backend.events if event[0] == "set_traffic"]
    assert toggles == [("set_traffic", False), ("set_traffic", True), ("set_traffic", False)]
    assert not any(event[0] == "wait" for event in backend.events)


@pytest.mark.parametrize(
    ("created_at", "allowed"),
    [
        pytest.param(T0 - MICROSECOND, False, id="one-microsecond-before-T0"),
        pytest.param(E - timedelta(hours=1), False, id="long-before-T0"),
        pytest.param(T0, True, id="at-T0"),
        pytest.param(T0 + timedelta(seconds=30), True, id="after-T0"),
    ],
)
def test_an_incident_before_t0_is_an_error(created_at: Any, allowed: bool) -> None:
    backend = RecordingBackend(FakeEvidence(incidents=[IncidentRecord("i1", created_at)]))
    result = ProductRunner(backend).run(_scenario(PRE_HISTORY, Phase(timedelta(0), ())))
    if allowed:
        assert result.status is RunStatus.RUN_OK
        return
    assert result.status is RunStatus.ERROR
    assert result.error is not None and "i1" in result.error and "before T0" in result.error
    assert ("set_traffic", True) not in backend.events  # traffic never enabled
    stages = list(backend.stages())
    assert stages[-2:] == [Stage.TIMELINE, Stage.CLUSTER_DOWN]
    for later in (Stage.AWAIT_R1, Stage.R_EARLY, Stage.AWAIT_R2, Stage.ARTIFACT):
        assert later not in stages


def test_no_t0_action_runs_after_a_pre_history_incident() -> None:
    evidence = FakeEvidence(incidents=[IncidentRecord("early", T0 - timedelta(minutes=1))])
    backend = RecordingBackend(evidence)
    ProductRunner(backend).run(
        _scenario(PRE_HISTORY, Phase(timedelta(0), (PatchService("order-service", {"app": "x"}),)))
    )
    assert not any(event[0] == "patch_service_selector" for event in backend.events)


def test_traffic_turns_off_before_the_artifact_on_success() -> None:
    backend = RecordingBackend()
    ProductRunner(backend).run(_scenario())
    events = _kinds(backend)
    on = events.index(("set_traffic", True))
    off = len(events) - 1 - events[::-1].index(("set_traffic", False))
    assert events.index(Stage.AWAIT_R2) < off < events.index(Stage.ARTIFACT)
    assert on < off


class Failing(RecordingBackend):
    def __init__(self, fail_traffic_off: bool = False) -> None:
        super().__init__()
        self._fail_off = fail_traffic_off

    def await_r1(self, scenario: ProductScenario) -> None:
        super().await_r1(scenario)
        raise RuntimeError("R1 failed")

    def set_traffic(self, enabled: bool) -> None:
        super().set_traffic(enabled)
        if not enabled and self._fail_off and ("set_traffic", True) in self.events:
            raise RuntimeError("traffic off failed")


def test_an_error_after_t0_turns_traffic_off_before_teardown() -> None:
    backend = Failing()
    with pytest.raises(RuntimeError, match="R1 failed"):
        ProductRunner(backend).run(_scenario())
    events = _kinds(backend)
    assert events[-2:] == [("set_traffic", False), Stage.CLUSTER_DOWN]


def test_a_failing_traffic_off_never_hides_the_error_or_skips_teardown() -> None:
    backend = Failing(fail_traffic_off=True)
    with pytest.raises(RuntimeError, match="R1 failed") as raised:
        ProductRunner(backend).run(_scenario())
    assert any("traffic off also failed" in note for note in raised.value.__notes__)
    assert list(backend.stages())[-1] is Stage.CLUSTER_DOWN


def test_pre_history_incident_is_a_typed_error() -> None:
    error = PreHistoryIncident(["b", "a"], T0)
    assert error.incident_ids == ("b", "a")
    assert T0.isoformat() in str(error)


def test_live_traffic_enable_is_a_later_task_and_off_is_a_no_op() -> None:
    commands = Commands()
    backend = LiveBackend(
        root=ROOT,
        control_port=_control(Commands(), []),
        evidence_port=LiveEvidenceReader.__new__(LiveEvidenceReader),
        run=commands,
        spawn=lambda argv, env: _Process(),
        sleep=lambda _: None,
    )
    backend.set_traffic(False)
    assert commands.calls == []
    backend.set_traffic(True)
    assert commands.calls and commands.calls[0][1] is not None  # the traffic Pod manifest


def test_the_guard_sees_incidents_opened_just_before_t0() -> None:
    """The guard runs at T0, so an incident opened during the last pre-history wait counts."""
    backend = RecordingBackend()
    control = backend.control()

    class Clocked(FakeEvidence):
        def incidents(self) -> Sequence[IncidentRecord]:
            opened = T0 - timedelta(minutes=1)
            return [IncidentRecord("late", opened)] if control.now() >= opened else []

    backend._evidence = Clocked()
    result = ProductRunner(backend).run(_scenario(PRE_HISTORY, Phase(timedelta(0), ())))
    assert result.status is RunStatus.ERROR
    assert result.error is not None and "late" in result.error
