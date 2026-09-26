"""M19-6.7: fresh-cluster orchestration, dry-run trace, and the live adapters (offline)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy
import yaml
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from test_product_actions import FakeEvidence, ledger, version

from packages.evals.product.actions import (
    ActionReceipt,
    DeletePodOf,
    IncidentRecord,
    JournalRecord,
    LifecycleRecord,
    PatchService,
    PodIdentity,
    ProductAction,
    SetReadiness,
    SetResources,
)
from packages.evals.product.live import (
    CLUSTER,
    PRODUCT_CONTROL_PLANE_ENV,
    LiveBackend,
    LiveClusterControl,
    LiveEvidenceReader,
    StageNotImplemented,
    baseline_faults,
    product_control_plane_manifest,
    product_environment,
)
from packages.evals.product.runner import (
    DRY_RUN_EPOCH,
    ProductRunner,
    RecordingBackend,
    RecordingControl,
    RunnerConfig,
    RunStatus,
    Stage,
    await_evidence,
)
from packages.evals.product.spec import Expectation, Phase, ProductScenario
from packages.storage.database import create_session_factory
from packages.storage.models import Base, IncidentRow, LifecycleObservationRow, ObjectVersionRow

ROOT = Path(__file__).resolve().parents[3]
E = DRY_RUN_EPOCH
LIFECYCLE = [
    Stage.CLUSTER_UP,
    Stage.DEPLOY_OBSERVABILITY_WORKLOAD,
    Stage.WAIT_WORKLOAD_READY,
    Stage.START_FRESH_DB_CONTROL_PLANE,
    Stage.WARMUP,
    Stage.CLEAN_BASELINE,
    Stage.TIMELINE,
    Stage.AWAIT_R1,
    Stage.R_EARLY,
    Stage.AWAIT_R2,
    Stage.ARTIFACT,
    Stage.CLUSTER_DOWN,
]
READINESS = SetReadiness("order-service", True, timedelta(seconds=40))
UID = "dry-run-order-service-0"


def _scenario(scenario_id: str = "s", *phases: Phase) -> ProductScenario:
    return ProductScenario(scenario_id=scenario_id, phases=tuple(phases), expectation=Expectation())


def _ready_evidence(uid: str = UID) -> FakeEvidence:
    """The ledger a correct SetReadiness at the dry-run epoch would leave."""
    return FakeEvidence(
        lifecycle=[
            _at(ledger(uid, "STATUS_SNAPSHOT", 0), E - timedelta(minutes=1)),
            _at(ledger(uid, "READY_FALSE", 0), E + timedelta(seconds=10)),
            _at(ledger(uid, "READY_TRUE", 0), E + timedelta(seconds=45)),
        ]
    )


def _at(record: LifecycleRecord, when: datetime) -> LifecycleRecord:
    return LifecycleRecord(record.instance_uid, record.type, when, None, record.payload)


def _stages(backend: RecordingBackend) -> list[Stage]:
    return list(backend.stages())


# --- lifecycle order -------------------------------------------------------------


def test_one_scenario_runs_the_exact_fresh_cluster_lifecycle() -> None:
    backend = RecordingBackend(_ready_evidence())
    result = ProductRunner(backend).run(_scenario("s", Phase(timedelta(0), (READINESS,))))
    assert result.status is RunStatus.RUN_OK
    assert _stages(backend) == LIFECYCLE  # every stage once, in order


def test_each_scenario_gets_its_own_cluster() -> None:
    backend = RecordingBackend()
    results = ProductRunner(backend).run_all([_scenario("a"), _scenario("b")])
    assert [item.scenario_id for item in results] == ["a", "b"]
    assert _stages(backend) == LIFECYCLE + LIFECYCLE
    assert _stages(backend).count(Stage.CLUSTER_UP) == 2


def test_warmup_is_ten_minutes_between_collector_start_and_baseline() -> None:
    backend = RecordingBackend()
    ProductRunner(backend).run(_scenario())
    (warmup,) = [event for event in backend.events if event[0] is Stage.WARMUP]
    assert warmup[1] == timedelta(minutes=10)
    assert RunnerConfig().warmup == timedelta(seconds=600)


# --- dry-run has no effects ------------------------------------------------------


def test_dry_run_performs_no_external_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    def bomb(*_: Any, **__: Any) -> Any:
        raise AssertionError("dry-run reached a real effect")

    for target, name in (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (time, "sleep"),
        (urllib.request, "urlopen"),
        (sqlalchemy, "create_engine"),
    ):
        monkeypatch.setattr(target, name, bomb)
    monkeypatch.setattr(socket.socket, "connect", bomb)
    backend = RecordingBackend(_ready_evidence())
    scenario = _scenario(
        "s",
        Phase(timedelta(minutes=-10), (READINESS,)),
        Phase(timedelta(0), (PatchService("order-service", {"app": "x"}),)),
    )
    result = ProductRunner(backend).run(scenario)
    assert _stages(backend)[0] is Stage.CLUSTER_UP and _stages(backend)[-1] is Stage.CLUSTER_DOWN
    assert (
        result.status is RunStatus.ERROR
    )  # PatchService has no journal: the world was not verified


def test_dry_run_trace_carries_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SRE_LLM_API_KEY", "sk-very-secret")
    backend = RecordingBackend(_ready_evidence())
    ProductRunner(backend).run(_scenario("s", Phase(timedelta(0), (READINESS,))))
    assert "sk-very-secret" not in repr(backend.events)


# --- environment -------------------------------------------------------------------


def test_product_environment_pins_the_scheduler_and_drops_llm_settings() -> None:
    env = product_environment(
        {
            "PATH": "/bin",
            "SRE_LLM_ENABLED": "true",
            "SRE_LLM_API_KEY": "secret",
            "SRE_LLM_MODEL": "gpt",
            "SRE_REEVALUATE": "false",
        }
    )
    assert env["PATH"] == "/bin"
    assert env["SRE_REEVALUATE"] == "true"
    assert env["SRE_AUTO_DIAGNOSE"] == "true"
    assert env["SRE_WATCH_INTERVAL_SECONDS"] == "15"
    assert env["SRE_LLM_ENABLED"] == "false"
    assert "SRE_LLM_API_KEY" not in env and "SRE_LLM_MODEL" not in env


def _control_plane_env(documents: list[Any]) -> list[dict[str, str]]:
    (deployment,) = [doc for doc in documents if doc and doc.get("kind") == "Deployment"]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    return list(container["env"])


def test_control_plane_manifest_gets_exactly_the_product_environment() -> None:
    documents = list(yaml.safe_load_all((ROOT / "infra/kubernetes/control-plane.yaml").read_text()))
    deployment = next(doc for doc in documents if doc and doc.get("kind") == "Deployment")
    deployment["spec"]["template"]["spec"]["containers"][0]["env"] += [
        {"name": "SRE_LLM_ENABLED", "value": "true"},
        {"name": "SRE_LLM_MODEL", "value": "gpt"},
    ]
    env = _control_plane_env(product_control_plane_manifest(documents))
    by_name = {item["name"]: item["value"] for item in env}
    for name, value in PRODUCT_CONTROL_PLANE_ENV.items():
        assert [item["name"] for item in env].count(name) == 1
        assert by_name[name] == value
    assert "SRE_LLM_MODEL" not in by_name
    assert by_name["SRE_CLUSTER_ACCESS"] == "true"  # unrelated settings kept
    assert _control_plane_env(documents) != env  # the input is not mutated


# --- cleanup ----------------------------------------------------------------------


class Failing(RecordingBackend):
    def __init__(self, stage: str, evidence: Any = None, cleanup_fails: bool = False) -> None:
        super().__init__(evidence)
        self._fail, self._cleanup_fails = stage, cleanup_fails

    def _stage(self, stage: Stage, *detail: Any) -> None:
        super()._stage(stage, *detail)
        if stage.value == self._fail:
            raise RuntimeError(f"{stage.value} failed")
        if stage is Stage.CLUSTER_DOWN and self._cleanup_fails:
            raise RuntimeError("teardown failed")


@pytest.mark.parametrize("stage", [s.value for s in LIFECYCLE[1:-1]])
def test_teardown_follows_any_later_stage_failure(stage: str) -> None:
    backend = Failing(stage)
    with pytest.raises(RuntimeError, match=f"{stage} failed"):
        ProductRunner(backend).run(_scenario())
    assert _stages(backend)[-1] is Stage.CLUSTER_DOWN


def test_no_teardown_is_claimed_when_cluster_up_fails() -> None:
    backend = Failing(Stage.CLUSTER_UP.value)
    with pytest.raises(RuntimeError, match="CLUSTER_UP failed"):
        ProductRunner(backend).run(_scenario())
    assert _stages(backend) == [Stage.CLUSTER_UP]


def test_a_failing_teardown_never_hides_the_original_failure() -> None:
    backend = Failing(Stage.WARMUP.value, cleanup_fails=True)
    with pytest.raises(RuntimeError, match="WARMUP failed") as raised:
        ProductRunner(backend).run(_scenario())
    assert any("teardown failed" in note for note in raised.value.__notes__)
    with pytest.raises(RuntimeError, match="teardown failed"):
        ProductRunner(Failing("none", cleanup_fails=True)).run(_scenario())


# --- actions in the timeline ------------------------------------------------------------


def test_actions_apply_then_settle_then_verify() -> None:
    order: list[str] = []

    class Tracing(FakeEvidence):
        def lifecycle(self, instance_uid: str) -> Sequence[LifecycleRecord]:
            order.append("read")
            return super().lifecycle(instance_uid)

    class Traced(SetReadiness):
        def apply(self, control: Any) -> Any:
            order.append("apply")
            return SetReadiness.apply(self, control)

        def verify(self, evidence: Any, receipt: Any) -> None:
            order.append("verify")
            SetReadiness.verify(self, evidence, receipt)

    evidence = Tracing(lifecycle=_ready_evidence()._lifecycle)
    backend = RecordingBackend(evidence)
    action = Traced("order-service", True, timedelta(seconds=40))
    result = ProductRunner(backend).run(_scenario("s", Phase(timedelta(0), (action,))))
    assert result.status is RunStatus.RUN_OK
    assert order[0] == "apply" and order[1] == "read" and "verify" in order
    assert order.index("verify") > order.index("read")


def test_a_failed_verification_is_an_error_that_stops_the_run() -> None:
    backend = RecordingBackend(FakeEvidence())  # nothing observed: SetReadiness cannot verify
    second = DeletePodOf("payment-service")
    result = ProductRunner(backend).run(
        _scenario("s", Phase(timedelta(0), (READINESS,)), Phase(timedelta(minutes=1), (second,)))
    )
    assert result.status is RunStatus.ERROR
    assert result.error is not None and "no READY_FALSE" in result.error
    assert not any(event[0] == "delete_pod" for event in backend.events)  # timeline stopped
    stages = _stages(backend)
    assert stages[-2:] == [Stage.TIMELINE, Stage.CLUSTER_DOWN]
    for later in (Stage.AWAIT_R1, Stage.R_EARLY, Stage.AWAIT_R2, Stage.ARTIFACT):
        assert later not in stages


def test_pending_evidence_is_waited_for_within_the_bound() -> None:
    control = RecordingControl([])
    receipt = READINESS.apply(control)
    arrives = receipt.finished_at + timedelta(seconds=17)

    class Late(FakeEvidence):
        def lifecycle(self, instance_uid: str) -> Sequence[LifecycleRecord]:
            ready = (
                [_at(ledger(instance_uid, "READY_TRUE", 0), arrives)]
                if control.now() >= arrives
                else []
            )
            return ready

    config = RunnerConfig(evidence_timeout=timedelta(minutes=1), evidence_poll=timedelta(seconds=5))
    assert await_evidence(READINESS, receipt, Late(), control, config) is True
    assert control.now() == receipt.finished_at + timedelta(seconds=20)  # four 5 s polls


def test_missing_evidence_stops_waiting_at_the_bound() -> None:
    control = RecordingControl([])
    receipt = READINESS.apply(control)
    config = RunnerConfig(
        evidence_timeout=timedelta(seconds=30), evidence_poll=timedelta(seconds=10)
    )
    assert await_evidence(READINESS, receipt, FakeEvidence(), control, config) is False
    assert control.now() - receipt.finished_at == timedelta(seconds=30)


def test_an_action_without_a_known_milestone_fails_closed() -> None:
    class Unknown(ProductAction):
        pass

    control = RecordingControl([])
    with pytest.raises(TypeError, match="no evidence milestone"):
        await_evidence(Unknown(), ActionReceipt(E, E), FakeEvidence(), control, RunnerConfig())


@pytest.mark.parametrize(
    ("action", "evidence"),
    [
        (
            DeletePodOf("payment-service"),
            FakeEvidence(
                lifecycle=[_at(ledger("dry-run-payment-service-0", "DELETED", 0), E)],
                journal=[
                    JournalRecord(
                        "Pod", "payment-service-0", "dry-run-payment-service-0", E, "DELETED", {}
                    )
                ],
            ),
        ),
        (
            SetResources("order-service", limits={"cpu": "200m"}),
            FakeEvidence(
                journal=[
                    JournalRecord(
                        "ReplicaSet",
                        "rs",
                        None,
                        E,
                        "CREATED",
                        {
                            "metadata": {
                                "ownerReferences": [
                                    {
                                        "kind": "Deployment",
                                        "name": "order-service",
                                        "controller": True,
                                    }
                                ]
                            }
                        },
                    )
                ]
            ),
        ),
        (
            PatchService("order-service", {"app": "x"}),
            FakeEvidence(journal=[version("Service", "order-service", 0, {})]),
        ),
    ],
    ids=["deletion", "resources", "selector"],
)
def test_each_action_has_a_persisted_milestone(
    action: ProductAction, evidence: FakeEvidence
) -> None:
    control = RecordingControl([], start=datetime(2026, 9, 26, 11, 59, tzinfo=UTC))
    receipt = action.apply(control)
    receipt = type(receipt)(**{**_fields(receipt), "started_at": E - timedelta(hours=1)})
    config = RunnerConfig(evidence_timeout=timedelta(0))
    assert await_evidence(action, receipt, evidence, control, config) is True
    assert await_evidence(action, receipt, FakeEvidence(), control, config) is False


def _fields(receipt: Any) -> dict[str, Any]:
    return {name: getattr(receipt, name) for name in receipt.__dataclass_fields__}


# --- timeline -----------------------------------------------------------------------


def test_negative_offsets_are_kept_and_phases_wait_their_gaps() -> None:
    first = SetReadiness("order-service", True, timedelta(0))
    second = SetReadiness("order-service", True, timedelta(0))
    evidence = FakeEvidence(
        lifecycle=[
            _at(ledger(UID, "STATUS_SNAPSHOT", 0), E - timedelta(minutes=1)),
            _at(ledger(UID, "READY_FALSE", 0), E + timedelta(seconds=1)),
            _at(ledger(UID, "READY_TRUE", 0), E + timedelta(seconds=2)),
            _at(ledger(UID, "READY_FALSE", 0), E + timedelta(minutes=15, seconds=1)),
            _at(ledger(UID, "READY_TRUE", 0), E + timedelta(minutes=15, seconds=2)),
        ]
    )
    backend = RecordingBackend(evidence)
    config = RunnerConfig(evidence_poll=timedelta(seconds=1))
    result = ProductRunner(backend, config).run(
        _scenario(
            "s",
            Phase(timedelta(minutes=-25), (first,)),
            Phase(timedelta(minutes=-10), (second,)),
        )
    )
    assert result.status is RunStatus.RUN_OK
    (timeline,) = [event for event in backend.events if event[0] is Stage.TIMELINE]
    assert timeline[1] == (timedelta(minutes=-25), timedelta(minutes=-10))
    toggles = [i for i, event in enumerate(backend.events) if event[0] == "set_not_ready"]
    assert len(toggles) == 4
    gap = sum(
        (event[1] for event in backend.events[toggles[1] + 1 : toggles[2]] if event[0] == "wait"),
        timedelta(0),
    )
    assert gap == timedelta(minutes=15)  # -25 min → -10 min, measured on the virtual clock


def test_actions_within_a_phase_keep_their_order() -> None:
    backend = RecordingBackend(FakeEvidence())
    phase = Phase(
        timedelta(0),
        (
            PatchService("order-service", {"app": "a"}),
            PatchService("payment-service", {"app": "b"}),
        ),
    )
    ProductRunner(backend, RunnerConfig(evidence_timeout=timedelta(0))).run(_scenario("s", phase))
    patched = [event[1] for event in backend.events if event[0] == "patch_service_selector"]
    assert patched == ["order-service"]  # the first failed verification stops the second


# --- CLI ------------------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/product_benchmark.py", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_dry_run_rejects_unknown_or_missing_scenarios() -> None:
    unknown = _cli("--dry-run", "--scenario", "nope")
    assert unknown.returncode == 2 and "unknown product scenario: nope" in unknown.stderr
    missing = _cli("--dry-run")
    assert missing.returncode == 2 and "--scenario" in missing.stderr
    live = _cli("--scenario", "nope")
    assert live.returncode == 2 and "not implemented yet" in live.stderr
    listing = _cli("--list")
    assert listing.returncode == 0 and listing.stdout.endswith("product scenarios registered\n")


# --- live control port --------------------------------------------------------------


class Commands:
    def __init__(self, outputs: Mapping[str, str] | None = None) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.envs: list[Mapping[str, str]] = []
        self.outputs = dict(outputs or {})

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], stdin: str | None) -> str:
        self.calls.append((list(argv), stdin))
        self.envs.append(env)
        for key, output in self.outputs.items():
            if key in " ".join(argv):
                return output
        return ""


def _control(commands: Commands, posts: list[tuple[str, Any]]) -> LiveClusterControl:
    return LiveClusterControl(
        service_urls={"order-service": "http://o", "payment-service": "http://p"},
        run=commands,
        env={"PATH": "/bin"},
        post=lambda url, payload: posts.append((url, dict(payload))),
        sleep=lambda _: None,
    )


@pytest.mark.parametrize(
    ("service", "url"), [("order-service", "http://o"), ("payment-service", "http://p")]
)
def test_readiness_toggles_post_the_full_baseline(service: str, url: str) -> None:
    posts: list[tuple[str, Any]] = []
    control = _control(Commands(), posts)
    control.set_not_ready(service, True)
    control.set_not_ready(service, False)
    baseline = baseline_faults(service)
    assert posts == [
        (f"{url}/__faults", {**baseline, "not_ready": True}),
        (f"{url}/__faults", {**baseline, "not_ready": False}),
    ]
    for _, payload in posts:
        assert set(payload) == set(baseline)  # never a partial update
        assert payload["memory_ballast_mb"] == 0
        assert payload["delay_ms"] == 0 and payload["error"] is False
        assert payload["db_query_delay_ms"] == 0
    if service == "payment-service":
        assert all(payload["db_hold_ms"] == 0 for _, payload in posts)


def test_live_control_commands_are_argv_on_the_product_context() -> None:
    pods = {"items": [{"metadata": {"name": "order-a", "uid": "u1"}}]}
    commands = Commands({"get pods": json.dumps(pods), "jsonpath": "u1"})
    control = _control(commands, [])
    assert control.pod_of("order-service") == PodIdentity("order-a", "u1")
    control.delete_pod(PodIdentity("order-a", "u1"))
    control.patch_resources("order-service", "order-service", {"cpu": "200m"}, {})
    control.wait_for_rollout("order-service")
    control.patch_service_selector("order-service", {"app": "payment-service"})
    for argv, _ in commands.calls:
        assert argv[:5] == ["kubectl", "--context", f"kind-{CLUSTER}", "-n", "sre-demo"]
    tails = [argv[5:] for argv, _ in commands.calls]
    assert ["delete", "pod", "order-a", "--wait=false"] in tails
    assert [
        "set",
        "resources",
        "deployment/order-service",
        "-c",
        "order-service",
        "--limits=cpu=200m",
    ] in tails
    patch = next(tail for tail in tails if tail[0] == "patch")
    assert json.loads(patch[-1]) == [
        {"op": "replace", "path": "/spec/selector", "value": {"app": "payment-service"}}
    ]


def test_deleting_refuses_a_pod_whose_uid_changed() -> None:
    control = _control(Commands({"jsonpath": "u2"}), [])
    with pytest.raises(RuntimeError, match="no longer uid u1"):
        control.delete_pod(PodIdentity("order-a", "u1"))


# --- live backend command order -------------------------------------------------------------


def test_live_backend_starts_the_control_plane_only_after_the_workload(tmp_path: Path) -> None:
    commands = Commands()
    evidence_engine = create_engine("sqlite://")
    Base.metadata.create_all(evidence_engine)
    spawned: list[list[str]] = []
    slept: list[float] = []

    def spawn(argv: Sequence[str], env: Mapping[str, str]) -> _Process:
        spawned.append(list(argv))
        return _Process()

    backend = LiveBackend(
        root=ROOT,
        control_port=_control(Commands(), []),
        evidence_port=LiveEvidenceReader(create_session_factory(evidence_engine)),
        run=commands,
        spawn=spawn,
        sleep=slept.append,
        port_forwards={"order-service": 18100},
        control_plane_url="http://cp",
        request_json=lambda url, payload, headers: {
            "initiating_finding_count": 0,
            "initiating_finding_ids": [],
            "root_eligible_manifestation_only_count": 0,
            "root_eligible_manifestation_only_hypothesis_ids": [],
        },
    )
    with pytest.raises(StageNotImplemented, match="M19-6.12"):
        ProductRunner(backend).run(_scenario())
    argvs = [" ".join(argv) for argv, _ in commands.calls]
    joined = "\n".join(argvs)
    assert argvs[0].startswith(f"kind create cluster --name {CLUSTER}")
    assert argvs[-1] == f"kind delete cluster --name {CLUSTER}"
    workload_ready = max(
        i for i, a in enumerate(argvs) if "rollout status deployment/order-worker" in a
    )
    control_plane = next(i for i, (argv, stdin) in enumerate(commands.calls) if stdin is not None)
    assert (
        argvs.index(next(a for a in argvs if "workload.yaml" in a)) < workload_ready < control_plane
    )
    assert "control-plane.yaml" not in joined  # applied only from the rendered product manifest
    rendered = list(yaml.safe_load_all(commands.calls[control_plane][1] or ""))
    by_name = {item["name"]: item["value"] for item in _control_plane_env(rendered)}
    assert all(by_name[key] == value for key, value in PRODUCT_CONTROL_PLANE_ENV.items())
    assert slept == [600.0]  # the live warmup
    assert spawned and all(f"kind-{CLUSTER}" in argv for argv in spawned)
    assert all("kind-agentic-sre " not in a + " " for a in argvs if "--context" in a)
    for forbidden in (
        "make deploy",
        "cluster-up",
        "TRUNCATE",
        "DELETE FROM",
        "reset_change_journal",
    ):
        assert forbidden not in joined
    assert all(
        not key.startswith("SRE_LLM_") or env[key] == "false"
        for env in commands.envs
        for key in env
    )


class _Process:
    def terminate(self) -> None:
        return None


# --- live evidence port -----------------------------------------------------------------


def test_live_evidence_reads_are_scoped_and_read_only() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    at = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    with factory() as session:
        for uid, namespace in (("u1", "sre-demo"), ("u2", "sre-demo"), ("u1", "other")):
            session.add(
                LifecycleObservationRow(
                    evidence_id=f"lifecycle:{namespace}:Pod:{uid}:1",
                    instance_uid=uid,
                    namespace=namespace,
                    kind="Pod",
                    name="p",
                    type="READY_TRUE",
                    source_at=None,
                    observed_at=at,
                    ingested_at=at,
                    source=f"collector-{namespace}",
                    payload={},
                )
            )
        for name in ("order-service", "payment-service"):
            session.add(
                ObjectVersionRow(
                    object_key=f"sre-demo/Service/{name}",
                    namespace="sre-demo",
                    kind="Service",
                    name=name,
                    uid=None,
                    observed_at=at,
                    content_hash="h",
                    body={"spec": {}},
                    lifecycle="OBSERVED",
                )
            )
        session.add(
            IncidentRow(
                incident_id=uuid4(),
                status="OPEN",
                severity="CRITICAL",
                source="ALERTMANAGER",
                title="t",
                created_at=at,
                updated_at=at,
                correlation_id=uuid4(),
            )
        )
        session.commit()
    reader = LiveEvidenceReader(factory)
    assert [item.instance_uid for item in reader.lifecycle("u1")] == ["u1"]
    assert [item.name for item in reader.journal("Service", "order-service")] == ["order-service"]
    assert len(reader.journal("Service")) == 2
    (incident,) = reader.incidents()
    assert isinstance(incident, IncidentRecord) and incident.created_at == at

    def counts() -> tuple[int, ...]:
        with factory() as session:
            return tuple(
                session.scalar(select(func.count()).select_from(table)) or 0
                for table in (LifecycleObservationRow, ObjectVersionRow, IncidentRow)
            )

    assert counts() == (3, 2, 1)
    public = {name for name in dir(reader) if not name.startswith("_")}
    assert public == {"lifecycle", "journal", "incidents", "namespace"}


def test_evidence_session_never_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    def bomb(*_: Any, **__: Any) -> None:
        raise AssertionError("the evidence port wrote")

    for method in ("add", "add_all", "delete", "merge", "commit"):
        monkeypatch.setattr(Session, method, bomb)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    reader = LiveEvidenceReader(create_session_factory(engine))
    assert reader.lifecycle("u1") == [] and reader.journal("Pod") == [] and reader.incidents() == []
