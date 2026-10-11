"""The testbed runner (docs/architecture/testbed-scenarios-design.md §3, §5, §7, §10).

``run_once`` executes one scenario repeat against a ``World`` (the lab, behind a small interface so the
protocol can be tested without one): isolate, calibrate on a quiet baseline, inject, observe execution
and the first alert, remove, wait for recovery and the stored diagnosis, then assemble the ground-truth
record and score it. The injector journal and the oracle series are written as the run goes; nothing is
back-filled. The engine is only ever *read* (its stored diagnosis); it never sees any of this.
"""

from __future__ import annotations

import dataclasses
import json
import random
import shutil
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from packages.evals.live.ground_truth import (
    Chain,
    Decoy,
    Link,
    RunRecord,
    ScenarioSpec,
    Source,
    Stamp,
    SuiteManifest,
    SymptomGroup,
    TestbedStore,
    Timeline,
    assemble_run,
    timeline_problems,
)
from packages.evals.live.journal import (
    ROLE_ALERT_OBSERVED,
    ROLE_EXECUTION_OBSERVED,
    InjectorJournal,
    JournalEntry,
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
from packages.rca.service_effect import BASELINE_BEFORE_EXECUTION

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
# A CPU stress on order-service raises the latency and error alerts of that service and of what hangs off it.
DIRECT_POD_ALERTS = DEPENDENCY_ALERTS | {
    "OrderErrorRateHigh",
    "HighErrorRate",
    "OrderDbQueryLatencyHigh",
    "OrderWorkerConsumerErrorsHigh",
}
LAG_ALERTS = frozenset({"KafkaConsumerLag", "OrderWorkerLagHigh"})
LATENCY_ALERTS = DEPENDENCY_ALERTS - LAG_ALERTS
ALERTS_BY_FAMILY: dict[str, frozenset[str]] = {
    "direct-pod-fault": frozenset(DIRECT_POD_ALERTS),
    "config-or-rollout": DEPENDENCY_ALERTS | {"PaymentServiceLatencyCritical"},
    # the designed symptom is latency; the lag alerts fire without any fault in this lab (design, slice 4)
    "scheduled-recurring": LATENCY_ALERTS,
    "competing-causes": DEPENDENCY_ALERTS,
    "negative-control": DEPENDENCY_ALERTS | {"PaymentServiceLatencyCritical"},
}
# The alerts that stamp the timeline when they differ from the collected ones: a competing run's timeline is the
# payment delay's (contract §15.4), so only its latency alerts may be its ``alert_fired_at``.
TIMELINE_ALERTS_BY_FAMILY: dict[str, frozenset[str]] = {"competing-causes": LATENCY_ALERTS}
ALERT_LATENCY_SECONDS = 0.5  # the latency of the lab's alert rules: the symptom threshold
RECOVERY_TIMEOUT_SECONDS = 180.0
REDIAGNOSE_SETTLE_SECONDS = (
    20.0  # the closing events must reach the control plane before it is asked again
)
EXECUTION_TIMEOUT_SECONDS = 30.0
DIAGNOSIS_TIMEOUT_SECONDS = 300.0
DIAGNOSIS_QUIET_SECONDS = 20.0
# design §18: lag builds about a minute after a pod-kill, then 30 s of admission, a poll and a diagnosis
SECOND_CAUSE_COLLECT_SECONDS = 180.0
ALERT_POLL_SECONDS = 5.0
# design §33: a fault acts seconds before its Applied is recorded; the engine's baseline window ends at Applied
APPLY_LAG = timedelta(seconds=60)


class BaselineNotQuiet(RuntimeError):
    """The target already failed before the fault: nothing measured afterwards could be told apart from it.

    Raised before the injection, so no run is recorded; the work directory keeps its journal.
    """


class RunAborted(RuntimeError):
    """The run failed before anything was injected (design §31.2 A).

    Its work directory and database are set aside under new names, so the repeat can run again
    from the same manifest; nothing is deleted.
    """


class Interrupted(Exception):
    """The driver was asked to stop (``SIGINT``, ``SIGTERM``); handled like any other failure."""


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
    fault: str = "network-delay"  # "cpu-stress" (direct-pod-fault), "env-delay" (config-or-rollout)
    cpu_workers: int = 16
    # scheduled-recurring: how often the Schedule spawns an experiment and how long each lasts
    spawn_every_seconds: int = 60
    spawn_seconds: int = 20
    # competing-causes: when the second cause follows the first
    second_offset_seconds: float = 0.0
    # negative-control: when the decoy starts relative to the cause (negative: before it)
    decoy_offset_seconds: float = 0.0
    # packet-loss variants: the share of packets dropped
    loss_percent: int = 0
    # design §35 DEV check 1: one value of the workloads' ConfigMap is written after the baseline, before the injection
    edit_config: bool = False
    # design §35 DEV check 2: how many replicas the faulted workload runs with
    replicas: int = 1


# The HOLDOUT variants B (design §12.2.2): the injection differs from the family's variant A.
FAULT_BY_SCENARIO: dict[str, str] = {
    "dependency-loss-payment": "network-loss",
    "direct-stress-payment": "cpu-stress",
    "scheduled-stress-order": "scheduled-stress",
    "config-image-payment": "image-break",
    "negative-image-decoy": "image-break",
    "competing-loss-podkill": "competing-loss",
    "config-error-payment": "env-error",
    "config-crash-payment": "env-crash",
    "negative-dbdelay-decoy": "env-db-delay",
}
# Variants whose faulted service is also the one showing the symptom: one probe, two views (contract §4.2).
DIRECT_SCENARIOS = frozenset({"direct-stress-payment", "scheduled-stress-order"})
ERROR_ALERTS = frozenset({"OrderErrorRateHigh", "HighErrorRate", "PaymentErrorRateHigh"})
# Alert sets of the variants B, fixed before their phase 0 (design §12.2.2).
ALERTS_BY_SCENARIO: dict[str, frozenset[str]] = {
    "dependency-loss-payment": DEPENDENCY_ALERTS | ERROR_ALERTS,
    "config-image-payment": DEPENDENCY_ALERTS | {"PaymentServiceLatencyCritical"} | ERROR_ALERTS,
    "negative-image-decoy": DEPENDENCY_ALERTS | {"PaymentServiceLatencyCritical"} | ERROR_ALERTS,
    "competing-loss-podkill": DEPENDENCY_ALERTS | ERROR_ALERTS,
    "config-error-payment": DEPENDENCY_ALERTS | ERROR_ALERTS,
    "config-crash-payment": DEPENDENCY_ALERTS | {"PaymentServiceLatencyCritical"} | ERROR_ALERTS,
    "negative-dbdelay-decoy": DEPENDENCY_ALERTS | {"PaymentServiceLatencyCritical"},
}
TIMELINE_ALERTS_BY_SCENARIO: dict[str, frozenset[str]] = {
    "competing-loss-podkill": LATENCY_ALERTS | ERROR_ALERTS,
}


def derive_parameters(spec: ScenarioSpec, seed: int) -> RunParameters:
    """Seeded, reproducible draws from the manifest's ranges; a missing range uses a fixed default."""
    rng = random.Random(seed)

    def draw(name: str, default: float) -> float:
        given = spec.parameters.get(name)
        return default if given is None else round(rng.uniform(given.low, given.high), 3)

    params = RunParameters(
        seed=seed,
        baseline_seconds=draw("baseline_seconds", 45.0),
        offset_seconds=draw("offset_seconds", 10.0),
        duration_seconds=draw("duration_seconds", 100.0),
        latency_ms=int(draw("latency_ms", 400.0)),
        load_rps=draw("load_rps", 10.0),
        fault=FAULT_BY_SCENARIO.get(spec.scenario_id)
        or {
            "direct-pod-fault": "cpu-stress",
            "config-or-rollout": "env-delay",
            "negative-control": "env-delay",
            "scheduled-recurring": "scheduled-delay",
            "competing-causes": "competing",
        }.get(spec.family, "network-delay"),
        cpu_workers=int(draw("cpu_workers", 16.0)),
        spawn_every_seconds=int(draw("spawn_every_seconds", 60.0)),
        spawn_seconds=int(draw("spawn_seconds", 20.0)),
        second_offset_seconds=draw("second_offset_seconds", 0.0),
        decoy_offset_seconds=draw("decoy_offset_seconds", 0.0),
        loss_percent=int(draw("loss_percent", 0.0)),
    )
    if "second_before_end_seconds" in spec.parameters:
        # design §18: the second cause lands this long before the first one is removed (drawn last, so the
        # other draws of a seed stay as they were)
        before = draw("second_before_end_seconds", 0.0)
        params = dataclasses.replace(
            params, second_offset_seconds=max(0.0, params.duration_seconds - before)
        )
    if "edit_config" in spec.parameters:
        # design §35: drawn after the others, so the variant's other draws of a seed stay as they were
        params = dataclasses.replace(params, edit_config=draw("edit_config", 0.0) >= 0.5)
    if "replicas" in spec.parameters:
        params = dataclasses.replace(params, replicas=int(draw("replicas", 1.0)))
    return params


def calibrate(latencies: Sequence[float], *, floor: float = 0.1) -> float:
    """A latency threshold from a quiet baseline: clearly above what the run itself showed, never below ``floor``.

    Three times the 90th percentile of the baseline: ordinary jitter is never read as an effect, and one
    stray spike in a short baseline (a slow first request) does not push the threshold out of reach of the
    fault (testbed contract §4.5).
    """
    if not latencies:
        raise ValueError("calibration needs at least one baseline sample")
    ordered = sorted(latencies)
    return max(floor, 3.0 * ordered[int(0.9 * (len(ordered) - 1))])


@dataclass(frozen=True)
class Injection:
    name: str
    uid: str
    target_pod: str
    target_pod_uid: str
    injected_at: datetime
    kind: str = "NetworkChaos"
    # a rollout's execution is the new ReplicaSet, known only once the controller has created it
    execution_name: str = ""
    execution_uid: str = ""
    # a Schedule's executions: the experiments it spawned, (name, uid), captured before it is removed
    spawned: tuple[tuple[str, str], ...] = ()
    # competing causes: the second cause, injected after a seeded offset (contract §15)
    companion: Injection | None = None
    # negative control: the decoy injected around the cause (contract §16)
    decoy: Injection | None = None
    namespace: str = "sre-demo"
    spawn_kind: str = "NetworkChaos"  # what a Schedule spawns


@dataclass(frozen=True)
class StoredDiagnosis:
    """One incident of the run and the latest diagnosis stored for it."""

    incident_id: str
    alert: str
    incident_created_at: datetime
    diagnosed_at: datetime
    document: dict[str, object]
    first_diagnosed_at: datetime | None = (
        None  # the incident's first diagnosis (``diagnosed_at`` is the latest)
    )


class World(Protocol):
    """Everything the protocol needs from the lab."""

    def shape(self, params: RunParameters) -> None:
        """The lab's shape the next isolation sets up for the run (design §35: the target's replicas)."""
        ...

    def isolate(self, run_id: str) -> None: ...

    def quiet_since(self) -> datetime | None:
        """When the isolation step finished quieting the lab (design §33); None before it."""
        ...

    def start_load(self, rps: float) -> None: ...

    def stop_load(self) -> None: ...

    def target_measure(self) -> Measurement: ...

    def client_measure(self) -> Measurement: ...

    def target_warnings(self) -> list[str]:
        """Warning events of the fault's target pod as the cluster holds them now."""
        ...

    def foreign_faults(self) -> list[str]:
        """Chaos objects or chaos events in the watched namespaces that this run did not create.

        A manual diagnostic experiment, or one left by another run, would enter the run's evidence
        and could be blamed for, or credited with, the incident.
        """
        ...

    def inject(self, params: RunParameters, journal: InjectorJournal, name: str) -> Injection: ...

    def before_injection(self, params: RunParameters, journal: InjectorJournal) -> None:
        """What the run does to the lab after the baseline gate and before the injection (design §35)."""
        ...

    def construction_problems(self) -> list[str]:
        """Why the decoy could reach the symptom (contract §16.3); empty when the construction holds."""
        ...

    def applied_at(self, injection: Injection) -> datetime | None: ...

    def settle(self, injection: Injection) -> Injection:
        """The injection with what the controller decided after it (a rollout's new ReplicaSet and pod)."""
        ...

    def remove(self, injection: Injection, journal: InjectorJournal) -> None: ...

    def alert_started_at(self, alerts: Collection[str], since: datetime) -> datetime | None: ...

    def diagnoses(self, alerts: Collection[str], since: datetime) -> list[StoredDiagnosis]: ...

    def rediagnose(self, alerts: Collection[str], since: datetime) -> int:
        """Ask the control plane for a fresh diagnosis of each incident of the run; the count asked."""
        ...

    def cleanup(self) -> None: ...

    def set_aside(self, run_id: str) -> str | None:
        """Rename the run's database out of the way of a later attempt; the new name, or None if none existed."""
        ...


def _set_aside(world: World, run_id: str, work: Path, kind: str, clock: Clock) -> Path:
    """Move a run that ended before its injection out of the way, keeping everything (design §31.2 A)."""
    target = work.with_name(f"{work.name}.{kind}-{clock.now():%Y%m%dT%H%M%S}")
    work.rename(target)
    database = world.set_aside(run_id)
    if database is not None:
        (target / "set-aside.json").write_text(
            json.dumps({"run_id": run_id, "database": database}, sort_keys=True) + "\n"
        )
    return target


@dataclass(frozen=True)
class RunOutcome:
    record: RunRecord
    score: RunScore | None
    directory: Path


def _workload(pod: str) -> str:
    """``payment-service-84ff5d9795-zhw6l`` -> ``payment-service`` (a Deployment's pod name)."""
    parts = pod.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else ""


def direct_pod_chain(injection: Injection) -> Chain:
    """The world's chain for a CPU stress on ``order-service``: the pod is the symptom, nothing propagates."""
    experiment = f"sre-demo/{injection.kind}/{injection.name}"
    return Chain(
        links=(
            Link(
                role="cause",
                actor=experiment,
                instance_uid=injection.uid,
                knowable=True,
                mechanism="StressChaos cpu",
            ),
            Link(
                role="execution",
                actor=experiment,
                instance_uid=injection.uid,
                knowable=True,
                mechanism="StressChaos cpu",
                evidence_class="execution",
            ),
            Link(
                role="target_effect",
                actor=f"sre-demo/Pod/{injection.target_pod}",
                instance_uid=injection.target_pod_uid,
                knowable=True,
                mechanism="request latency at the stressed pod",
                evidence_class="effect",
            ),
            Link(
                role="symptom",
                actor=f"sre-demo/Service/{_workload(injection.target_pod) or 'order-service'}",
                knowable=False,
                mechanism="latency of the stressed service itself",
            ),
        )
    )


def config_chain(injection: Injection) -> Chain:
    """The world's chain for an environment change on ``payment-service`` (design §4, `config-or-rollout`)."""
    change = f"sre-demo/Deployment/{injection.name}"
    return Chain(
        links=(
            Link(
                role="cause",
                actor=change,
                instance_uid=injection.uid,
                knowable=True,
                mechanism="environment change that slows payments",
            ),
            Link(
                role="execution",
                actor=f"sre-demo/ReplicaSet/{injection.execution_name}",
                instance_uid=injection.execution_uid,
                knowable=True,
                mechanism="rollout of the changed template",
                evidence_class="execution",
            ),
            Link(
                role="target_effect",
                actor=f"sre-demo/Pod/{injection.target_pod}",
                instance_uid=injection.target_pod_uid,
                knowable=True,
                mechanism="payment latency at the new pod",
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


def scheduled_chain(injection: Injection) -> Chain:
    """The world's chain for a ``Schedule`` spawning delays on ``payment-service`` (design §4, variant A).

    The cause is the Schedule instance; each experiment it spawned is an execution with its own UID.
    """
    schedule = f"sre-demo/Schedule/{injection.name}"
    mechanism = "Schedule spawning NetworkChaos delay"
    return Chain(
        links=(
            Link(
                role="cause",
                actor=schedule,
                instance_uid=injection.uid,
                knowable=True,
                mechanism=mechanism,
            ),
            *(
                Link(
                    role="execution",
                    actor=f"sre-demo/NetworkChaos/{name}",
                    instance_uid=uid,
                    knowable=True,
                    mechanism=mechanism,
                    evidence_class="execution",
                )
                for name, uid in injection.spawned
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


def competing_chain(injection: Injection) -> Chain:
    """The world's chain for a delay on ``payment-service`` plus a pod-kill of ``order-worker`` (contract §15)."""
    delay = dependency_chain(injection)
    kill = injection.companion
    if kill is None:
        return delay
    experiment = f"sre-demo/{injection.kind}/{injection.name}"
    killer = f"sre-demo/{kill.kind}/{kill.name}"
    return Chain(
        links=(
            *delay.links,
            Link(
                role="cause",
                actor=killer,
                instance_uid=kill.uid,
                knowable=True,
                mechanism="PodChaos pod-kill",
            ),
            Link(
                role="execution",
                actor=f"sre-demo/Pod/{kill.target_pod}",
                instance_uid=kill.target_pod_uid,
                knowable=True,
                mechanism="PodChaos pod-kill",
                evidence_class="execution",
            ),
            Link(
                role="symptom",
                actor="sre-demo/Deployment/order-worker",
                knowable=False,
                mechanism="consumer lag while the worker is gone",
            ),
        ),
        symptom_groups=(
            SymptomGroup(
                alerts=tuple(sorted(LATENCY_ALERTS)), causes=(experiment,), required=(experiment,)
            ),
            # the delay slows order-worker's calls to payment-service, so it adds to the lag too (§15.1)
            SymptomGroup(
                alerts=tuple(sorted(LAG_ALERTS)), causes=(killer, experiment), required=(killer,)
            ),
        ),
    )


NEGATIVE_CONSTRUCTION = (
    "the decoy runs on lab-control/isolated-echo: no sre-demo workload references lab-control in its configuration "
    "and NetworkPolicy default-deny isolates the namespace, both checked before the injection (contract §16.3)"
)


def negative_chain(injection: Injection) -> Chain:
    """The real configuration change of slice 3 plus a decoy experiment on the isolated workload (contract §16)."""
    real = config_chain(injection)
    decoy = injection.decoy
    decoys = (
        (
            Decoy(
                actor=f"{decoy.namespace}/{decoy.kind}/{decoy.name}",
                instance_uid=decoy.uid,
                knowable=True,
                mechanism="NetworkChaos delay on an isolated workload",
            ),
        )
        if decoy is not None
        else ()
    )
    return Chain(links=real.links, construction=NEGATIVE_CONSTRUCTION, decoys=decoys)


def _remechanised(chain: Chain, old: str, new: str) -> Chain:
    links = tuple(
        link.model_copy(update={"mechanism": link.mechanism.replace(old, new)})
        for link in chain.links
    )
    return chain.model_copy(update={"links": links})


def loss_chain(injection: Injection) -> Chain:
    """Variant B of `dependency-fault`: packet loss instead of delay on ``payment-service``."""
    return _remechanised(dependency_chain(injection), "NetworkChaos delay", "NetworkChaos loss")


def competing_loss_chain(injection: Injection) -> Chain:
    """Variant B of `competing-causes`: packet loss on ``payment-service`` plus the pod-kill (§15). Loss can
    raise error alerts as well as latency ones, so the loss's group holds both."""
    chain = _remechanised(competing_chain(injection), "NetworkChaos delay", "NetworkChaos loss")
    groups = tuple(
        group.model_copy(update={"alerts": tuple(sorted(set(group.alerts) | ERROR_ALERTS))})
        if set(group.alerts) & LATENCY_ALERTS
        else group
        for group in chain.symptom_groups
    )
    return chain.model_copy(update={"symptom_groups": groups})


def scheduled_stress_chain(injection: Injection) -> Chain:
    """Variant B of `scheduled-recurring`: a Schedule spawning CPU stress on ``order-service``, which is also
    the service showing the symptom, so the chain has no propagation link (contract §14.3)."""
    schedule = f"sre-demo/Schedule/{injection.name}"
    mechanism = f"Schedule spawning {injection.spawn_kind} cpu"
    return Chain(
        links=(
            Link(
                role="cause",
                actor=schedule,
                instance_uid=injection.uid,
                knowable=True,
                mechanism=mechanism,
            ),
            *(
                Link(
                    role="execution",
                    actor=f"sre-demo/{injection.spawn_kind}/{name}",
                    instance_uid=uid,
                    knowable=True,
                    mechanism=mechanism,
                    evidence_class="execution",
                )
                for name, uid in injection.spawned
            ),
            Link(
                role="target_effect",
                actor=f"sre-demo/Pod/{injection.target_pod}",
                instance_uid=injection.target_pod_uid,
                knowable=True,
                mechanism="request latency at the stressed pod",
                evidence_class="effect",
            ),
            Link(
                role="symptom",
                actor="sre-demo/Service/order-service",
                knowable=False,
                mechanism="latency of the stressed service itself",
            ),
        )
    )


# variants whose chain differs from their family's (design §12.2.2)
def error_config_chain(injection: Injection) -> Chain:
    """m21 §13.4, `config-or-rollout` variant C: the change makes the new revision fail payments."""
    return _remechanised(
        _remechanised(config_chain(injection), "slows payments", "fails payments"),
        "payment latency",
        "payment failures",
    )


def dbdelay_negative_chain(injection: Injection) -> Chain:
    """m21 §13.4, `negative-control` variant C: the real change slows payments through the database."""
    return _remechanised(negative_chain(injection), "slows payments", "slows payment queries")


def crash_config_chain(injection: Injection) -> Chain:
    """m21 §14: the change leaves the new revision unable to start, and the old one is already gone."""
    return _remechanised(
        _remechanised(config_chain(injection), "slows payments", "stops payments starting"),
        "payment latency at the new pod",
        "the new pod never ready, the old one removed",
    )


CHAINS_BY_SCENARIO = {
    "config-crash-payment": crash_config_chain,
    "config-error-payment": error_config_chain,
    "negative-dbdelay-decoy": dbdelay_negative_chain,
    "dependency-loss-payment": loss_chain,
    "scheduled-stress-order": scheduled_stress_chain,
    "competing-loss-podkill": competing_loss_chain,
}


CHAINS = {
    "negative-control": negative_chain,
    "competing-causes": competing_chain,
    "direct-pod-fault": direct_pod_chain,
    "config-or-rollout": config_chain,
    "scheduled-recurring": scheduled_chain,
}


def dependency_chain(injection: Injection) -> Chain:
    """The world's chain for a delay on ``payment-service`` (design §10)."""
    experiment = f"sre-demo/{injection.kind}/{injection.name}"
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

    def __init__(
        self,
        world: World,
        clock: Clock,
        work: Path,
        params: RunParameters,
        *,
        direct: bool = False,
        direct_on_target: bool = False,
    ) -> None:
        self.world, self.clock, self.params = world, clock, params
        self.journal = InjectorJournal(work / "journal.jsonl", clock=clock.now)
        self.writer = SeriesWriter(work / "series.jsonl")
        # Thresholds are read at every sample, so calibration edits them in place.
        self.views: dict[str, float | None] = {"target": None, "propagation": None, "symptom": None}
        if direct:
            # The faulted service is the one showing the symptom: one probe, two views of it (contract §4.2).
            # the faulted service's own probe: the client's for order-service, the target's otherwise
            measure = world.target_measure if direct_on_target else world.client_measure
            probes = [LatencyProbe("target", measure, _Views(self.views, ("target", "symptom")))]
        else:
            probes = [
                LatencyProbe("target", world.target_measure, _Views(self.views, ("target",))),
                LatencyProbe(
                    "client", world.client_measure, _Views(self.views, ("propagation", "symptom"))
                ),
            ]
        self.oracle = Oracle(probes, self.writer)

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
    alerts: Collection[str] | None = None,
    code_identity: Mapping[str, str] | None = None,
) -> RunOutcome:
    spec = manifest.spec(scenario_id)
    alerts = (
        alerts
        or ALERTS_BY_SCENARIO.get(spec.scenario_id)
        or ALERTS_BY_FAMILY.get(spec.family, DEPENDENCY_ALERTS)
    )
    params = derive_parameters(spec, spec.seeds[repeat])
    timeline_alerts = TIMELINE_ALERTS_BY_SCENARIO.get(
        spec.scenario_id, TIMELINE_ALERTS_BY_FAMILY.get(spec.family, alerts)
    )
    run_id = f"{manifest.suite_id}-{scenario_id}-{repeat}"
    work = work_root / run_id
    if work.exists():
        raise FileExistsError(f"{work} exists; a re-run is a new repeat")
    work.mkdir(parents=True)
    direct = spec.family == "direct-pod-fault" or spec.scenario_id in DIRECT_SCENARIOS
    watched_views = ("target", "symptom") if direct else ("target", "propagation", "symptom")
    run = _Run(
        world,
        clock,
        work,
        params,
        direct=direct,
        direct_on_target=spec.scenario_id == "direct-stress-payment",
    )
    problems: list[str] = []
    injection: Injection | None = None
    stored: list[StoredDiagnosis] = []
    injecting = False  # from the first inject call on, a failure belongs to the run
    if code_identity is not None:
        run.journal.record(
            verb="check", object="code", role="code_identity", payload=dict(code_identity)
        )
    try:
        world.shape(params)
        world.isolate(run_id)
        world.start_load(params.load_rps)  # the client probe and the alerts need traffic
        # 1. quiet baseline, then thresholds fixed from it and written to the journal
        run.wait(params.baseline_seconds)
        base = run.writer.read()
        target_limit = calibrate(_latencies(base, "target"))
        client_limit = None if direct else calibrate(_latencies(base, "propagation"))
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
        # design §33: the engine's baseline window lies wholly after the lab was made quiet
        quiet = world.quiet_since()
        if quiet is None:
            raise RuntimeError("the world recorded no moment it was made quiet")
        earliest = quiet + BASELINE_BEFORE_EXECUTION + APPLY_LAG
        waited = max(0.0, (earliest - clock.now()).total_seconds())
        run.wait(waited)
        run.journal.record(
            verb="check",
            object="baseline/window",
            role="baseline_window",
            payload={
                "quiet_since": quiet.isoformat(),
                "earliest_injection": earliest.isoformat(),
                "waited_seconds": round(waited, 3),
            },
        )
        # the gate: a target that already failed makes every later effect ambiguous (design, "Baseline health")
        warnings = world.target_warnings()
        foreign = world.foreign_faults()
        run.journal.record(
            verb="check",
            object="target/events",
            role="baseline_gate",
            ok=not warnings and not foreign,
            payload={"warnings": warnings, "foreign_faults": foreign},
        )
        if warnings:
            raise BaselineNotQuiet(
                f"the target raised warnings before the injection: {warnings[:5]}"
            )
        if foreign:
            raise BaselineNotQuiet(f"faults this run did not create are present: {foreign[:5]}")
        world.before_injection(params, run.journal)
        prefix = {
            "cpu-stress": "pod-stress",
            "env-delay": "env-delay",
            "env-error": "env-error",
            "env-crash": "env-crash",
            "env-db-delay": "env-db-delay",
            "scheduled-delay": "sched-delay",
            "scheduled-stress": "sched-stress",
            "network-loss": "dep-loss",
            "competing-loss": "dep-loss",
        }.get(params.fault, "dep-delay")
        decoy: Injection | None = None
        decoy_params = dataclasses.replace(
            params,
            fault="decoy-stress" if spec.scenario_id == "negative-image-decoy" else "decoy-delay",
        )
        if spec.family == "negative-control":
            construction = world.construction_problems()
            run.journal.record(
                verb="check",
                object="decoy/construction",
                role="construction_check",
                ok=not construction,
                payload={"problems": construction},
            )
            problems.extend(f"construction: {p}" for p in construction)
            if params.decoy_offset_seconds < 0:
                injecting = True
                decoy = world.inject(decoy_params, run.journal, f"decoy-{params.seed}")
                run.wait(-params.decoy_offset_seconds)
        injecting = True
        injection = world.inject(params, run.journal, f"{prefix}-{params.seed}")
        alert_at: datetime | None = None
        applied: datetime | None = None
        deadline = clock.now().timestamp() + params.duration_seconds
        next_alert_poll = 0.0
        companion: Injection | None = None
        while clock.now().timestamp() < deadline:
            run.wait(1.0)
            if (
                params.fault in ("competing", "competing-loss")
                and companion is None
                and (clock.now() - injection.injected_at).total_seconds()
                >= params.second_offset_seconds
            ):
                companion = world.inject(
                    dataclasses.replace(params, fault="pod-kill"),
                    run.journal,
                    f"pod-kill-{params.seed}",
                )
            if (
                spec.family == "negative-control"
                and decoy is None
                and (clock.now() - injection.injected_at).total_seconds()
                >= params.decoy_offset_seconds
            ):
                decoy = world.inject(decoy_params, run.journal, f"decoy-{params.seed}")
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
                alert_at = world.alert_started_at(timeline_alerts, injection.injected_at)
                if alert_at is not None:
                    run.journal.record(
                        verb="observe",
                        object="alertmanager/alerts",
                        role=ROLE_ALERT_OBSERVED,
                        payload={"starts_at": alert_at.isoformat()},
                    )
        if applied is None:
            problems.append("the controller's Applied event was never observed")
        injection = dataclasses.replace(world.settle(injection), companion=companion, decoy=decoy)
        if spec.family == "negative-control" and decoy is None:
            problems.append("the decoy was never injected")
        if params.fault in ("competing", "competing-loss") and companion is None:
            problems.append("the second cause was never injected")
        if injection.kind == "Schedule" and not injection.spawned:
            problems.append("the schedule spawned no experiment before it was removed")
        # 3. removal, then recovery of every probe view
        world.remove(injection, run.journal)
        if injection.companion is not None:
            world.remove(injection.companion, run.journal)
        if injection.decoy is not None:
            world.remove(injection.decoy, run.journal)
        removed_at = clock.now()
        recover_by = removed_at.timestamp() + RECOVERY_TIMEOUT_SECONDS
        while clock.now().timestamp() < recover_by:
            run.wait(3.0)
            if all_recovered_at(run.writer.read(), watched_views, after=removed_at):
                break
        # 4. the closing events reach the control plane, which is asked once more for each incident: a
        #    diagnosis stored at the alert cannot know how the experiment ended (an unobserved end proves nothing)
        run.wait(REDIAGNOSE_SETTLE_SECONDS)
        if alert_at is None:
            # the control plane admits an alert only once it is old enough (connector contract §16), which can be
            # after the cause is gone: the alert's start is still its start
            alert_at = world.alert_started_at(timeline_alerts, injection.injected_at)
            if alert_at is not None:
                run.journal.record(
                    verb="observe",
                    object="alertmanager/alerts",
                    role=ROLE_ALERT_OBSERVED,
                    payload={"starts_at": alert_at.isoformat()},
                )
        asked = world.rediagnose(alerts, injection.injected_at)
        run.journal.record(
            verb="rediagnose",
            object="incidents",
            role="rediagnose_requested",
            payload={"incidents": asked},
        )
        # 5. the stored diagnoses, once they stop changing
        # design §18: a second cause's symptom can open its incident well after the first one is removed
        not_before = (
            injection.companion.injected_at + timedelta(seconds=SECOND_CAUSE_COLLECT_SECONDS)
            if injection.companion is not None
            else None
        )
        stored = _await_diagnoses(
            world, clock, run, alerts, injection.injected_at, not_before=not_before
        )
        foreign = world.foreign_faults()
        run.journal.record(
            verb="check",
            object="chaos",
            role="isolation_check",
            ok=not foreign,
            payload={"foreign_faults": foreign},
        )
        if foreign:
            problems.append(f"faults this run did not create were present: {foreign[:5]}")
    except BaselineNotQuiet:
        # kept for inspection, out of the way of the repeat's real run
        world.stop_load()
        world.cleanup()
        _set_aside(world, run_id, work, "refused", clock)
        raise
    except Exception as failure:
        if injecting:
            raise
        world.stop_load()
        world.cleanup()
        aside = _set_aside(world, run_id, work, "aborted", clock)
        raise RunAborted(
            f"{type(failure).__name__}: {failure} (set aside as {aside.name})"
        ) from failure
    finally:
        world.stop_load()
        world.cleanup()
    return _finish(manifest, spec, repeat, run, store, injection, stored, problems, work)


def _await_diagnoses(
    world: World,
    clock: Clock,
    run: _Run,
    alerts: Collection[str],
    since: datetime,
    *,
    not_before: datetime | None = None,
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
        quiet = clock.now().timestamp() - last_change >= DIAGNOSIS_QUIET_SECONDS
        if found and quiet and (not_before is None or clock.now() >= not_before):
            break
        run.wait(3.0)
    return found


def _stamp(at: datetime, source: Source) -> Stamp:
    return Stamp(at=at, source=source)


def probe_roles(family: str, scenario_id: str = "") -> ProbeRoles:
    """Which probe view stands for which field: a direct fault has no downstream (contract §4.2)."""
    direct = family == "direct-pod-fault" or scenario_id in DIRECT_SCENARIOS
    return ProbeRoles(
        target="target",
        downstream=None if direct else "propagation",
        symptom="symptom",
        everything=("target", "symptom") if direct else ("target", "propagation", "symptom"),
    )


def rederive(
    record: RunRecord, series: Sequence[ProbeResult], entries: Sequence[JournalEntry]
) -> RunRecord:
    """The run's record with its oracle fields derived again by the current rules (contract §14.4).

    The injector fields, the chain and every reason that is not about the timeline are kept as recorded.
    """
    recorded = set(timeline_problems(record.timeline, record.family, record.clock_offset_seconds))
    kept = [reason for reason in record.invalid_reasons if reason not in recorded]
    execution = record.timeline.execution_started_at or record.timeline.cause_created_at
    if execution is None or not series:
        return record
    removed = next((e.at for e in entries if e.role == "cause_removed" and e.ok), None)
    oracle = oracle_stamps(
        series,
        probe_roles(record.family, record.scenario_id),
        execution_started_at=execution.at,
        cause_removed_at=removed or series[-1].observed_at,
    )
    fields = {
        name: stamp
        for name in ("cause_created_at", "execution_started_at", "alert_fired_at")
        if (stamp := record.timeline.stamp(name)) is not None
    }
    timeline = Timeline(**fields, **oracle)  # type: ignore[arg-type]
    reasons = timeline_problems(timeline, record.family, record.clock_offset_seconds, record.chain)
    return record.model_copy(update={"timeline": timeline, "invalid_reasons": (*reasons, *kept)})


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
        probe_roles(spec.family, spec.scenario_id),
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
        chain=(
            CHAINS_BY_SCENARIO.get(spec.scenario_id) or CHAINS.get(spec.family, dependency_chain)
        )(injection),
        clock_offset_seconds=0.0,  # the injector and the oracle share one host clock
        diagnosis_completed_at=(primary.first_diagnosed_at or primary.diagnosed_at)
        if primary
        else None,
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
        score = score_run(
            record,
            Diagnosis.model_validate(primary.document),
            tier=spec.tier,
            also=[Diagnosis.model_validate(d.document) for d in stored if d is not primary],
            incidents=[(d.alert, Diagnosis.model_validate(d.document)) for d in stored],
        )
        store.write_artifact(record, "score.json", score.model_dump_json().encode())
    shutil.rmtree(work, ignore_errors=True)
    return RunOutcome(record=record, score=score, directory=directory)
