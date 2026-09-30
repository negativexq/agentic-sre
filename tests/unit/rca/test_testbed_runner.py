"""The runner's protocol against a scripted world and a fake clock (no lab, no waiting)."""

from __future__ import annotations

import stat
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_fault_execution_support import OFF, full, source

from packages.evals.live.ground_truth import (
    ParameterRange,
    ScenarioSpec,
    SuiteManifest,
    TestbedStore,
)
from packages.evals.live.journal import ROLE_CAUSE_CREATED, ROLE_CAUSE_REMOVED, InjectorJournal
from packages.evals.live.oracle import Measurement
from packages.evals.live.testbed_runner import (
    Injection,
    RunParameters,
    StoredDiagnosis,
    calibrate,
    derive_parameters,
    run_once,
)
from packages.rca.engine import build_case, diagnose_case

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.current = T0

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeWorld:
    """A lab that degrades between an injection and its removal, and raises alerts and diagnoses."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        apply_event: bool = True,
        diagnose: bool = True,
        fail_on_inject: bool = False,
    ) -> None:
        self.clock = clock
        self.calls: list[str] = []
        self.apply_event, self.diagnose, self.fail_on_inject = apply_event, diagnose, fail_on_inject
        self.injected: datetime | None = None
        self.removed: datetime | None = None
        self.document = diagnose_case(build_case(source(full())), config=OFF).model_dump(
            mode="json"
        )

    def _degraded(self) -> bool:
        return self.injected is not None and self.removed is None

    def isolate(self, run_id: str) -> None:
        self.calls.append(f"isolate:{run_id}")

    def start_load(self, rps: float) -> None:
        self.calls.append("start_load")

    def stop_load(self) -> None:
        self.calls.append("stop_load")

    def target_measure(self) -> Measurement:
        return Measurement(self.clock.now(), True, 0.6 if self._degraded() else 0.005)

    def client_measure(self) -> Measurement:
        return Measurement(self.clock.now(), True, 0.9 if self._degraded() else 0.05)

    def inject(self, params: RunParameters, journal: InjectorJournal, name: str) -> Injection:
        if self.fail_on_inject:
            raise RuntimeError("the API server refused the experiment")
        self.calls.append("inject")
        self.injected = self.clock.now()
        journal.record(
            verb="apply", object=f"networkchaos {name}", role=ROLE_CAUSE_CREATED, uid="e1"
        )
        return Injection(name, "e1", "payment-1", "p1", self.injected)

    def applied_at(self, injection: Injection) -> datetime | None:
        ready = self.clock.now() >= injection.injected_at + timedelta(seconds=1)
        return injection.injected_at + timedelta(seconds=1) if self.apply_event and ready else None

    def remove(self, injection: Injection, journal: InjectorJournal) -> None:
        self.calls.append("remove")
        self.removed = self.clock.now()
        journal.record(
            verb="delete", object=f"networkchaos {injection.name}", role=ROLE_CAUSE_REMOVED
        )

    def alert_started_at(self, alerts: Collection[str], since: datetime) -> datetime | None:
        alert = since + timedelta(seconds=25)
        return alert if self.clock.now() >= alert else None

    def diagnoses(self, alerts: Collection[str], since: datetime) -> list[StoredDiagnosis]:
        if (
            not self.diagnose
            or self.injected is None
            or self.clock.now() < self.injected + timedelta(seconds=60)
        ):
            return []
        return [
            StoredDiagnosis(
                "i1",
                "OrderDependencyLatencyHigh",
                self.injected + timedelta(seconds=30),
                self.injected + timedelta(seconds=55),
                self.document,
            )
        ]

    def cleanup(self) -> None:
        self.calls.append("cleanup")


def manifest() -> SuiteManifest:
    spec = ScenarioSpec(
        scenario_id="dependency-delay",
        family="dependency-fault",
        tier="DEV",
        repeats=2,
        seeds=(7, 8),
        parameters={
            "baseline_seconds": ParameterRange(low=30, high=30),
            "offset_seconds": ParameterRange(low=5, high=15),
            "duration_seconds": ParameterRange(low=90, high=110),
            "latency_ms": ParameterRange(low=300, high=600),
            "load_rps": ParameterRange(low=8, high=12),
        },
    )
    return SuiteManifest(
        suite_id="s1",
        engine_version="2.1.0",
        created_at=T0,
        salt="x",
        scenarios=(spec,),
        acceptance={"false_resolved": 0},
    ).frozen()


def run(tmp_path: Path, world: FakeWorld | None = None, repeat: int = 0):  # type: ignore[no-untyped-def]
    clock = world.clock if world is not None else FakeClock()
    world = world or FakeWorld(clock)
    store = TestbedStore(tmp_path / "store")
    frozen = manifest()
    if not (store.root / "s1" / "manifest.json").exists():
        store.write_manifest(frozen)
    return (
        world,
        store,
        run_once(
            frozen,
            "dependency-delay",
            repeat,
            world,
            store,
            work_root=tmp_path / "work",
            clock=clock,
        ),
    )


def test_a_complete_run_is_valid_ordered_and_scored(tmp_path: Path) -> None:
    world, _, outcome = run(tmp_path)
    record = outcome.record
    assert record.valid, record.invalid_reasons
    timeline = record.timeline
    for name in (
        "cause_created_at",
        "execution_started_at",
        "target_effect_at",
        "propagation_started_at",
        "symptom_started_at",
        "alert_fired_at",
        "recovery_at",
    ):
        assert timeline.stamp(name) is not None, name
    assert outcome.score is not None and outcome.score.valid
    assert world.calls[0].startswith("isolate:s1-dependency-delay-0")
    assert world.calls[-2:] == ["stop_load", "cleanup"]


def test_the_effect_is_seen_at_the_target_before_the_symptom(tmp_path: Path) -> None:
    _, _, outcome = run(tmp_path)
    t = outcome.record.timeline
    assert t.execution_started_at and t.target_effect_at and t.symptom_started_at and t.recovery_at
    assert t.execution_started_at.at <= t.target_effect_at.at <= t.symptom_started_at.at
    assert t.recovery_at.at > t.symptom_started_at.at


def test_the_journal_records_the_calibration_and_every_call_in_order(tmp_path: Path) -> None:
    _, store, outcome = run(tmp_path)
    journal = InjectorJournal(store.run_dir("s1", "dependency-delay", 0) / "journal.jsonl")
    roles = [e.role for e in journal.entries()]
    assert (
        roles.index("probe_calibration")
        < roles.index(ROLE_CAUSE_CREATED)
        < roles.index(ROLE_CAUSE_REMOVED)
    )
    calibration = next(e for e in journal.entries() if e.role == "probe_calibration")
    assert calibration.payload["symptom_limit_seconds"] == 0.5
    assert calibration.payload["target_limit_seconds"] >= 0.1
    assert outcome.record.seed == 7


def test_everything_a_run_stores_is_write_once(tmp_path: Path) -> None:
    _, store, outcome = run(tmp_path)
    names = sorted(p.name for p in outcome.directory.iterdir())
    assert names == [
        "chain.json",
        "diagnoses.json",
        "journal.jsonl",
        "run.json",
        "score.json",
        "series.jsonl",
        "timeline.json",
    ]
    assert all(not (p.stat().st_mode & stat.S_IWUSR) for p in outcome.directory.iterdir())
    with pytest.raises(FileExistsError):
        run(tmp_path, FakeWorld(FakeClock()))  # the same repeat again is refused, not overwritten


def test_a_run_whose_execution_was_never_seen_is_invalid_and_not_scored(tmp_path: Path) -> None:
    _, _, outcome = run(tmp_path, FakeWorld(FakeClock(), apply_event=False))
    assert not outcome.record.valid
    assert any("Applied" in reason for reason in outcome.record.invalid_reasons)
    assert outcome.score is None
    assert outcome.directory.exists()  # an invalid run is still recorded


def test_a_run_without_a_stored_diagnosis_is_invalid(tmp_path: Path) -> None:
    _, _, outcome = run(tmp_path, FakeWorld(FakeClock(), diagnose=False))
    assert any("no diagnosis" in reason for reason in outcome.record.invalid_reasons)
    assert outcome.score is None


def test_the_lab_is_cleaned_up_even_when_the_injection_fails(tmp_path: Path) -> None:
    world = FakeWorld(FakeClock(), fail_on_inject=True)
    with pytest.raises(RuntimeError):
        run(tmp_path, world)
    assert world.calls[-2:] == ["stop_load", "cleanup"]


def test_parameters_are_reproducible_from_the_seed_and_stay_in_their_ranges() -> None:
    spec = manifest().spec("dependency-delay")
    first, again = derive_parameters(spec, 7), derive_parameters(spec, 7)
    assert first == again and first != derive_parameters(spec, 8)
    assert 5 <= first.offset_seconds <= 15 and 90 <= first.duration_seconds <= 110
    assert 300 <= first.latency_ms <= 600 and 8 <= first.load_rps <= 12
    assert derive_parameters(spec.model_copy(update={"parameters": {}}), 7).baseline_seconds == 45.0


def test_calibration_sits_well_above_the_baseline_and_never_below_the_floor() -> None:
    # three times the 90th percentile (contract §4.5, amended 2026-09-30; it was the maximum)
    assert calibrate([0.01, 0.02]) == 0.1
    assert calibrate([0.05 + 0.01 * i for i in range(20)]) == pytest.approx(
        3 * 0.22
    )  # index 17 of 20
    with pytest.raises(ValueError):
        calibrate([])


def test_a_direct_pod_fault_is_a_cpu_stress_whose_chain_has_no_propagation() -> None:
    from packages.evals.live.testbed_runner import Injection, direct_pod_chain

    spec = ScenarioSpec(
        scenario_id="direct-stress",
        family="direct-pod-fault",
        tier="DEV",
        repeats=1,
        seeds=(3,),
        parameters={"cpu_workers": ParameterRange(low=40, high=48)},
    )
    params = derive_parameters(spec, 3)
    assert params.fault == "cpu-stress" and 40 <= params.cpu_workers <= 48
    chain = direct_pod_chain(Injection("pod-stress-3", "u", "pay-1", "pu", T0, "StressChaos"))
    assert [link.role for link in chain.links] == ["cause", "execution", "target_effect", "symptom"]
    assert chain.of_role("cause")[0].actor == "sre-demo/StressChaos/pod-stress-3"
