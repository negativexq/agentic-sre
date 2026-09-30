"""The testbed runner (docs/architecture/testbed-scenarios-design.md §3, §5, §7, §10).

``run_once`` executes one scenario repeat against a ``World`` (the lab, behind a small interface so the
protocol can be tested without one): isolate, calibrate on a quiet baseline, inject, observe execution
and the first alert, remove, wait for recovery and the stored diagnosis, then assemble the ground-truth
record and score it. The injector journal and the oracle series are written as the run goes; nothing is
back-filled. The engine is only ever *read* (its stored diagnosis); it never sees any of this.
"""

from __future__ import annotations

import json
import random
import shutil
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from packages.evals.live.ground_truth import (
    Chain,
    Link,
    RunRecord,
    ScenarioSpec,
    Source,
    Stamp,
    SuiteManifest,
    TestbedStore,
    Timeline,
    assemble_run,
)
from packages.evals.live.journal import (
    ROLE_ALERT_OBSERVED,
    ROLE_EXECUTION_OBSERVED,
    InjectorJournal,
    injector_stamps,
)
from packages.evals.live.oracle import (
    LatencyProbe,
    Measurement,
    Oracle,
    ProbeResult,
    ProbeRoles,
    SeriesWriter,
    all_recovered_at,
    oracle_stamps,
)
from packages.evals.live.testbed_grader import RunScore, score_run
from packages.rca.model import Diagnosis

# The alerts one payment-service delay is expected to raise (seen in the lab); the primary incident of a
# run is the earliest one created after the injection whose alert is in this set.
DEPENDENCY_ALERTS = frozenset(
    {
        "OrderDependencyLatencyHigh",
        "PaymentRequestLatencyHigh",
        "PaymentDbQueryLatencyHigh",
        "OrderRequestLatencyHigh",
        "HighRequestLatency",
        "OrderWorkerLagHigh",
        "KafkaConsumerLag",
    }
)
ALERT_LATENCY_SECONDS = 0.5  # the latency of the lab's alert rules: the symptom threshold
RECOVERY_TIMEOUT_SECONDS = 180.0
EXECUTION_TIMEOUT_SECONDS = 30.0
DIAGNOSIS_TIMEOUT_SECONDS = 300.0
DIAGNOSIS_QUIET_SECONDS = 20.0
ALERT_POLL_SECONDS = 5.0


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class RunParameters:
    """What the seed decided for one repeat."""

    seed: int
    baseline_seconds: float
    offset_seconds: float
    duration_seconds: float
    latency_ms: int
    load_rps: float


def derive_parameters(spec: ScenarioSpec, seed: int) -> RunParameters:
    """Seeded, reproducible draws from the manifest's ranges; a missing range uses a fixed default."""
    rng = random.Random(seed)

    def draw(name: str, default: float) -> float:
        given = spec.parameters.get(name)
        return default if given is None else round(rng.uniform(given.low, given.high), 3)

    return RunParameters(
        seed=seed,
        baseline_seconds=draw("baseline_seconds", 45.0),
        offset_seconds=draw("offset_seconds", 10.0),
        duration_seconds=draw("duration_seconds", 100.0),
        latency_ms=int(draw("latency_ms", 400.0)),
        load_rps=draw("load_rps", 10.0),
    )


def calibrate(latencies: Sequence[float], *, floor: float = 0.1) -> float:
    """A latency threshold from a quiet baseline: clearly above what the run itself showed, never below ``floor``.

    Three times the largest baseline latency, so ordinary jitter is never read as an effect.
    """
    if not latencies:
        raise ValueError("calibration needs at least one baseline sample")
    return max(floor, 3.0 * max(latencies))


@dataclass(frozen=True)
class Injection:
    name: str
    uid: str
    target_pod: str
    target_pod_uid: str
    injected_at: datetime


@dataclass(frozen=True)
class StoredDiagnosis:
    """One incident of the run and the latest diagnosis stored for it."""

    incident_id: str
    alert: str
    incident_created_at: datetime
    diagnosed_at: datetime
    document: dict[str, object]


class World(Protocol):
    """Everything the protocol needs from the lab."""

    def isolate(self, run_id: str) -> None: ...

    def start_load(self, rps: float) -> None: ...

    def stop_load(self) -> None: ...

    def target_measure(self) -> Measurement: ...

    def client_measure(self) -> Measurement: ...

    def inject(self, params: RunParameters, journal: InjectorJournal, name: str) -> Injection: ...

    def applied_at(self, injection: Injection) -> datetime | None: ...

    def remove(self, injection: Injection, journal: InjectorJournal) -> None: ...

    def alert_started_at(self, alerts: Collection[str], since: datetime) -> datetime | None: ...

    def diagnoses(self, alerts: Collection[str], since: datetime) -> list[StoredDiagnosis]: ...

    def cleanup(self) -> None: ...


@dataclass(frozen=True)
class RunOutcome:
    record: RunRecord
    score: RunScore | None
    directory: Path


def dependency_chain(injection: Injection) -> Chain:
    """The world's chain for a delay on ``payment-service`` (design §10)."""
    experiment = f"sre-demo/NetworkChaos/{injection.name}"
    return Chain(
        links=(
            Link(
                role="cause",
                actor=experiment,
                instance_uid=injection.uid,
                knowable=True,
                mechanism="NetworkChaos delay",
            ),
            Link(
                role="execution",
                actor=experiment,
                instance_uid=injection.uid,
                knowable=True,
                mechanism="NetworkChaos delay",
                evidence_class="execution",
            ),
            Link(
                role="target_effect",
                actor=f"sre-demo/Pod/{injection.target_pod}",
                instance_uid=injection.target_pod_uid,
                knowable=True,
                mechanism="request latency at the faulted pod",
                evidence_class="effect",
            ),
            Link(
                role="propagation",
                actor="sre-demo/Deployment/order-service",
                knowable=False,
                mechanism="dependency latency seen by the caller",
                evidence_class="propagation",
            ),
            Link(
                role="symptom",
                actor="sre-demo/Service/order-service",
                knowable=False,
                mechanism="client-facing request latency",
            ),
        )
    )


class _Run:
    """One repeat in progress: the shared state of the protocol steps."""

    def __init__(self, world: World, clock: Clock, work: Path, params: RunParameters) -> None:
        self.world, self.clock, self.params = world, clock, params
        self.journal = InjectorJournal(work / "journal.jsonl", clock=clock.now)
        self.writer = SeriesWriter(work / "series.jsonl")
        # Thresholds are read at every sample, so calibration edits them in place.
        self.views: dict[str, float | None] = {"target": None, "propagation": None, "symptom": None}
        self.oracle = Oracle(
            [
                LatencyProbe("target", world.target_measure, _Views(self.views, ("target",))),
                LatencyProbe(
                    "client", world.client_measure, _Views(self.views, ("propagation", "symptom"))
                ),
            ],
            self.writer,
        )

    def sample(self) -> list[ProbeResult]:
        return self.oracle.sample_once()

    def wait(self, seconds: float) -> None:
        """Sample once a second for ``seconds``; a sample that runs long shortens the next sleep."""
        end = self.clock.now().timestamp() + seconds
        while True:
            started = self.clock.now().timestamp()
            self.sample()
            remaining = end - self.clock.now().timestamp()
            if remaining <= 0:
                return
            self.clock.sleep(
                min(remaining, max(0.0, 1.0 - (self.clock.now().timestamp() - started)))
            )


class _Views(Mapping[str, float | None]):
    """A read-through mapping onto shared thresholds, restricted to some names."""

    def __init__(self, source: dict[str, float | None], names: Sequence[str]) -> None:
        self._source, self._names = source, tuple(names)

    def __getitem__(self, key: str) -> float | None:
        return self._source[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


def _latencies(series: Sequence[ProbeResult], probe: str) -> list[float]:
    return [
        r.latency_seconds
        for r in series
        if r.probe == probe and r.ok and r.latency_seconds is not None
    ]


def run_once(
    manifest: SuiteManifest,
    scenario_id: str,
    repeat: int,
    world: World,
    store: TestbedStore,
    *,
    work_root: Path,
    clock: Clock,
    alerts: Collection[str] = DEPENDENCY_ALERTS,
) -> RunOutcome:
    spec = manifest.spec(scenario_id)
    params = derive_parameters(spec, spec.seeds[repeat])
    run_id = f"{manifest.suite_id}-{scenario_id}-{repeat}"
    work = work_root / run_id
    if work.exists():
        raise FileExistsError(f"{work} exists; a re-run is a new repeat")
    work.mkdir(parents=True)
    run = _Run(world, clock, work, params)
    problems: list[str] = []
    injection: Injection | None = None
    stored: list[StoredDiagnosis] = []
    try:
        world.isolate(run_id)
        world.start_load(params.load_rps)  # the client probe and the alerts need traffic
        # 1. quiet baseline, then thresholds fixed from it and written to the journal
        run.wait(params.baseline_seconds)
        base = run.writer.read()
        target_limit = calibrate(_latencies(base, "target"))
        client_limit = calibrate(_latencies(base, "propagation"))
        run.views["target"], run.views["propagation"] = target_limit, client_limit
        run.views["symptom"] = ALERT_LATENCY_SECONDS
        run.journal.record(
            verb="calibrate",
            object="probes",
            role="probe_calibration",
            payload={
                "target_limit_seconds": target_limit,
                "propagation_limit_seconds": client_limit,
                "symptom_limit_seconds": ALERT_LATENCY_SECONDS,
                "baseline_samples": len(_latencies(base, "target")),
            },
        )
        # 2. injection after the seeded offset
        run.wait(params.offset_seconds)
        injection = world.inject(params, run.journal, f"dep-delay-{params.seed}")
        alert_at: datetime | None = None
        applied: datetime | None = None
        deadline = clock.now().timestamp() + params.duration_seconds
        next_alert_poll = 0.0
        while clock.now().timestamp() < deadline:
            run.wait(1.0)
            if applied is None:
                applied = world.applied_at(injection)
                if applied is not None:
                    run.journal.record(
                        verb="observe",
                        object=f"events/{injection.name}",
                        role=ROLE_EXECUTION_OBSERVED,
                        payload={"applied_at": applied.isoformat()},
                    )
            if alert_at is None and clock.now().timestamp() >= next_alert_poll:
                next_alert_poll = clock.now().timestamp() + ALERT_POLL_SECONDS
                alert_at = world.alert_started_at(alerts, injection.injected_at)
                if alert_at is not None:
                    run.journal.record(
                        verb="observe",
                        object="alertmanager/alerts",
                        role=ROLE_ALERT_OBSERVED,
                        payload={"starts_at": alert_at.isoformat()},
                    )
        if applied is None:
            problems.append("the controller's Applied event was never observed")
        # 3. removal, then recovery of every probe view
        world.remove(injection, run.journal)
        removed_at = clock.now()
        recover_by = removed_at.timestamp() + RECOVERY_TIMEOUT_SECONDS
        while clock.now().timestamp() < recover_by:
            run.wait(3.0)
            if all_recovered_at(
                run.writer.read(), ("target", "propagation", "symptom"), after=removed_at
            ):
                break
        # 4. the stored diagnoses, once they stop changing
        stored = _await_diagnoses(world, clock, run, alerts, injection.injected_at)
    finally:
        world.stop_load()
        world.cleanup()
    return _finish(manifest, spec, repeat, run, store, injection, stored, problems, work)


def _await_diagnoses(
    world: World, clock: Clock, run: _Run, alerts: Collection[str], since: datetime
) -> list[StoredDiagnosis]:
    give_up = clock.now().timestamp() + DIAGNOSIS_TIMEOUT_SECONDS
    last_change = clock.now().timestamp()
    seen: tuple[tuple[str, datetime], ...] = ()
    found: list[StoredDiagnosis] = []
    while clock.now().timestamp() < give_up:
        found = world.diagnoses(alerts, since)
        state = tuple(sorted((d.incident_id, d.diagnosed_at) for d in found))
        if state != seen:
            seen, last_change = state, clock.now().timestamp()
        if found and clock.now().timestamp() - last_change >= DIAGNOSIS_QUIET_SECONDS:
            break
        run.wait(3.0)
    return found


def _stamp(at: datetime, source: Source) -> Stamp:
    return Stamp(at=at, source=source)


def _finish(
    manifest: SuiteManifest,
    spec: ScenarioSpec,
    repeat: int,
    run: _Run,
    store: TestbedStore,
    injection: Injection | None,
    stored: list[StoredDiagnosis],
    problems: list[str],
    work: Path,
) -> RunOutcome:
    if injection is None:
        raise RuntimeError("the run ended before an injection was made")
    entries = run.journal.entries()
    injector = injector_stamps(entries)
    removed = next((e.at for e in entries if e.role == "cause_removed" and e.ok), None)
    execution_at = injector.get("execution_started_at", injection.injected_at)
    series = run.writer.read()
    oracle = oracle_stamps(
        series,
        ProbeRoles(
            target="target",
            downstream="propagation",
            symptom="symptom",
            everything=("target", "propagation", "symptom"),
        ),
        execution_started_at=execution_at,
        cause_removed_at=removed or run.clock.now(),
    )
    fields: dict[str, Stamp] = {name: _stamp(at, Source.INJECTOR) for name, at in injector.items()}
    fields.update(oracle)
    timeline = Timeline(**fields)  # type: ignore[arg-type]
    primary = min(stored, key=lambda d: d.incident_created_at, default=None)
    if primary is None:
        problems.append("no diagnosis was stored for an expected alert")
    record = assemble_run(
        manifest,
        spec.scenario_id,
        repeat,
        timeline=timeline,
        chain=dependency_chain(injection),
        clock_offset_seconds=0.0,  # the injector and the oracle share one host clock
        diagnosis_completed_at=primary.diagnosed_at if primary else None,
    )
    record = record.model_copy(update={"invalid_reasons": (*record.invalid_reasons, *problems)})
    directory = store.write_run(record)
    for name in ("journal.jsonl", "series.jsonl"):
        store.write_artifact(record, name, (work / name).read_bytes())
    store.write_artifact(
        record,
        "diagnoses.json",
        json.dumps(
            [
                {
                    "incident_id": d.incident_id,
                    "alert": d.alert,
                    "incident_created_at": d.incident_created_at.isoformat(),
                    "diagnosed_at": d.diagnosed_at.isoformat(),
                    "document": d.document,
                }
                for d in sorted(stored, key=lambda d: d.incident_created_at)
            ],
            sort_keys=True,
        ).encode(),
    )
    score = None
    if record.valid and primary is not None:
        score = score_run(record, Diagnosis.model_validate(primary.document), tier=spec.tier)
        store.write_artifact(record, "score.json", score.model_dump_json().encode())
    shutil.rmtree(work, ignore_errors=True)
    return RunOutcome(record=record, score=score, directory=directory)
