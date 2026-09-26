"""M19-6.12a: live wiring — deploy order, in-cluster traffic, port-forwards, smokes."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import create_engine, func, select
from test_product_runner import ROOT, Commands, _control, _Process

from packages.evals.product.live import (
    CLUSTER,
    OBSERVABILITY_DEPLOYMENTS,
    TRAFFIC_NAMESPACE,
    WORKLOAD_DEPLOYMENTS,
    LiveBackend,
    LiveEvidenceReader,
    traffic_manifest,
)
from packages.evals.product.revisions import NoR1, ScheduleConfig, discover_r1
from packages.evals.product.runner import (
    DRY_RUN_EPOCH,
    ProductRunner,
    RecordingBackend,
    RecordingControl,
    RunResult,
    RunStatus,
)
from packages.evals.product.smoke import (
    CHAIN_ROOT,
    READINESS,
    SMOKES,
    accept,
    capture_evidence,
)
from packages.evals.product.spec import Expectation, Phase, ProductScenario
from packages.storage.database import create_session_factory
from packages.storage.models import Base, EvidenceRequirementRow, IncidentRow

WATCHED = {"sre-demo", "observability", "chaos-mesh"}


def _backend(commands: Commands, **fields: Any) -> LiveBackend:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    defaults: dict[str, Any] = {
        "root": ROOT,
        "control_port": _control(Commands(), []),
        "evidence_port": LiveEvidenceReader(create_session_factory(engine)),
        "run": commands,
        "spawn": lambda argv, env: _Process(),
        "sleep": lambda _: None,
        "port_ready": lambda port: True,
    }
    defaults.update(fields)
    return LiveBackend(**defaults)


def _argvs(commands: Commands) -> list[str]:
    return [" ".join(argv) for argv, _ in commands.calls]


def _index(argvs: list[str], needle: str) -> int:
    return next(i for i, argv in enumerate(argvs) if needle in argv)


# --- deploy order ----------------------------------------------------------------------------


def test_deploy_follows_make_deploy_order() -> None:
    commands = Commands()
    backend = _backend(commands)
    backend.deploy_observability_and_workload()
    backend.wait_workload_ready()
    argvs = _argvs(commands)
    workload = _index(argvs, "workload.yaml")
    migration = _index(argvs, "db-migration.yaml")
    kafka_ready = _index(argvs, "rollout status deployment/kafka")
    topic = _index(argvs, "--create --if-not-exists --topic orders.created")
    restart = _index(argvs, "rollout restart")
    # The migration job reads the workload ConfigMap, so workload.yaml comes first.
    assert workload < migration
    assert kafka_ready < topic < migration < restart
    assert all(f"deployment/{name}" in argvs[restart] for name in WORKLOAD_DEPLOYMENTS)
    for name in OBSERVABILITY_DEPLOYMENTS:
        index = _index(argvs, f"-n observability rollout status deployment/{name}")
        assert index > restart
    assert all(f"kind-{CLUSTER}" in argv for argv in argvs if "kubectl" in argv)


def test_the_topic_waits_for_kafka_and_fails_loudly() -> None:
    class Flaky(Commands):
        def __init__(self, failures: int) -> None:
            super().__init__()
            self.failures = failures

        def __call__(self, argv: Sequence[str], env: Mapping[str, str], stdin: str | None) -> str:
            if "--list" in argv and self.failures:
                self.failures -= 1
                raise subprocess.CalledProcessError(1, list(argv))
            return super().__call__(argv, env, stdin)

    flaky = Flaky(2)
    _backend(flaky)._create_topic("orders.created")
    assert sum("--create" in argv for argv in _argvs(flaky)) == 1
    with pytest.raises(RuntimeError, match="Kafka"):
        _backend(Flaky(1000))._create_topic("orders.created")


# --- traffic -------------------------------------------------------------------------------------


def test_traffic_is_in_cluster_through_the_service() -> None:
    namespace, pod = traffic_manifest(timedelta(hours=2))
    assert namespace["kind"] == "Namespace" and namespace["metadata"]["name"] == TRAFFIC_NAMESPACE
    assert TRAFFIC_NAMESPACE not in WATCHED  # harness traffic is never evidence
    assert pod["metadata"]["namespace"] == TRAFFIC_NAMESPACE
    (container,) = pod["spec"]["containers"]
    command = container["command"]
    arguments = dict(zip(command[3::2], command[4::2], strict=True))
    assert command[:3] == ["python", "-m", "workload.load_generator"]
    # Service DNS: routing follows Endpoints (readiness, selectors), never a port-forward.
    assert arguments["--base-url"] == "http://order-service.sre-demo.svc.cluster.local:8000"
    assert arguments == {
        "--base-url": "http://order-service.sre-demo.svc.cluster.local:8000",
        "--rate": "2",
        "--duration": "7200",
        "--seed": "42",
        "--concurrency": "1",
    }
    assert pod["spec"]["restartPolicy"] == "Never"
    assert container["image"] == "agentic-sre/order-service:dev"


def test_traffic_on_applies_and_waits_off_deletes_and_off_first_is_a_no_op() -> None:
    commands = Commands()
    backend = _backend(commands)
    backend.set_traffic(False)
    assert commands.calls == []
    backend.set_traffic(True)
    (apply, stdin), (wait, _) = commands.calls
    assert apply[-3:] == ["apply", "-f", "-"]
    assert [doc["kind"] for doc in yaml.safe_load_all(stdin or "")] == ["Namespace", "Pod"]
    assert "--for=condition=Ready" in wait
    backend.set_traffic(False)
    backend.set_traffic(False)
    deletes = [argv for argv in _argvs(commands) if " delete pod " in argv]
    assert len(deletes) == 1 and TRAFFIC_NAMESPACE in deletes[0]


# --- port-forwards -----------------------------------------------------------------------


def test_reads_are_port_forwarded_and_awaited() -> None:
    spawned: list[list[str]] = []
    waited: list[int] = []

    def ready(port: int) -> bool:
        waited.append(port)
        return True

    def spawn(argv: Sequence[str], env: Mapping[str, str]) -> _Process:
        spawned.append(list(argv))
        return _Process()

    backend = _backend(
        Commands(),
        spawn=spawn,
        control_plane_port=18000,
        postgres_port=15432,
        prometheus_port=19090,
        port_ready=ready,
    )
    backend.start_fresh_db_and_control_plane()
    forwards = {(argv[argv.index("-n") + 1], argv[-2], argv[-1]) for argv in spawned}
    assert forwards == {
        ("sre-demo", "service/control-plane", "18000:8000"),
        ("sre-demo", "service/postgres", "15432:5432"),
        ("observability", "service/prometheus", "19090:9090"),
    }
    assert set(waited) == {18000, 15432, 19090}
    assert backend._control_plane() == "http://127.0.0.1:18000"


def test_a_forward_that_never_listens_fails() -> None:
    backend = _backend(Commands(), control_plane_port=18000, port_ready=lambda port: False)
    with pytest.raises(RuntimeError, match="18000 never listened"):
        backend.start_fresh_db_and_control_plane()


def test_evidence_is_captured_before_the_cluster_is_deleted_and_never_blocks_it() -> None:
    commands = Commands()
    order: list[str] = []

    def capture(backend: LiveBackend) -> None:
        order.append(f"capture after {len(commands.calls)} commands")
        raise RuntimeError("capture failed")

    _backend(commands, before_teardown=capture).cluster_down()
    assert order == ["capture after 0 commands"]
    assert _argvs(commands)[-1] == f"kind delete cluster --name {CLUSTER}"


# --- NoR1 ---------------------------------------------------------------------------------------


class _NoIncidents:
    def __init__(self, count: int = 0) -> None:
        self.count = count

    def incidents(self) -> Sequence[Any]:
        from packages.evals.product.revisions import IncidentView  # noqa: PLC0415

        return [IncidentView(f"i{n}", DRY_RUN_EPOCH) for n in range(self.count)]

    def revisions(self, incident_id: str) -> Sequence[Any]:
        return []


def test_no_r1_is_typed_and_counts_the_incidents_seen() -> None:
    control = RecordingControl([])
    reads: Any = _NoIncidents()
    with pytest.raises(NoR1) as raised:
        discover_r1(reads, None, control, t0=DRY_RUN_EPOCH, config=ScheduleConfig())  # type: ignore[arg-type]
    assert raised.value.incidents == 0
    one: Any = _NoIncidents(1)
    with pytest.raises(NoR1) as raised:
        discover_r1(one, None, RecordingControl([]), t0=DRY_RUN_EPOCH, config=ScheduleConfig())  # type: ignore[arg-type]
    assert raised.value.incidents == 1


def test_an_error_result_names_its_type_and_keeps_the_verified_timeline() -> None:
    from test_product_runner import _ready_evidence  # noqa: PLC0415

    class Quiet(RecordingBackend):
        def await_r1(self, scenario: ProductScenario) -> None:
            super().await_r1(scenario)
            raise NoR1(0)

    scenario = ProductScenario("s", (Phase(timedelta(0), (READINESS,)),), Expectation())
    result = ProductRunner(Quiet(_ready_evidence("dry-run-order-service-0"))).run(scenario)
    assert (result.status, result.error_type) == (RunStatus.ERROR, "NoR1")
    assert [item.action for item in result.timeline] == [READINESS]


# --- smokes -----------------------------------------------------------------------------------


def test_smokes_are_not_dev_scenarios_and_use_only_existing_actions() -> None:
    assert set(SMOKES) == {"smoke-noop", "smoke-readiness", "smoke-chain"}
    assert all(item.tier is None and item.expectation.proofs == () for item in SMOKES.values())
    assert SMOKES["smoke-noop"].phases == ()
    assert SMOKES["smoke-readiness"].phases == (Phase(timedelta(0), (READINESS,)),)
    assert SMOKES["smoke-chain"].phases == (
        Phase(timedelta(minutes=-10), (READINESS,)),
        Phase(timedelta(0), (CHAIN_ROOT,)),
    )
    assert READINESS.service == "order-service" and READINESS.duration == timedelta(seconds=40)


def test_the_chain_root_is_the_protocols_qualified_pr03_root() -> None:
    protocol = (ROOT / "docs/architecture/m19-product-resolution-protocol.md").read_text()
    assert (
        'PatchService(name="payment-service", spec={"selector": {"app": "payment-service-retired"}})'
        in protocol
    )
    assert (CHAIN_ROOT.service, dict(CHAIN_ROOT.selector)) == (
        "payment-service",
        {"app": "payment-service-retired"},
    )


def _evidence(**overrides: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "incidents": [],
        "diagnoses": [],
        "requirements": [],
        "readiness": [{"type": "READY_FALSE"}, {"type": "READY_TRUE"}],
    }
    evidence.update(overrides)
    return evidence


NO_R1 = RunResult("s", RunStatus.ERROR, "no R1", error_type="NoR1")
CHAIN = [
    {"diagnosis_id": 1, "revision_number": 1, "trigger": "INITIAL"},
    {"diagnosis_id": 2, "revision_number": 2, "trigger": "MANUAL"},
    {"diagnosis_id": 3, "revision_number": 3, "trigger": "EVIDENCE_DEADLINE"},
]


@pytest.mark.parametrize(
    ("scenario_id", "result", "evidence", "accepted"),
    [
        ("smoke-noop", NO_R1, _evidence(), True),
        ("smoke-readiness", NO_R1, _evidence(), True),
        ("smoke-noop", NO_R1, _evidence(incidents=[{"id": "i"}]), False),
        (
            "smoke-noop",
            RunResult("s", RunStatus.ERROR, "x", error_type="RevisionScheduleError"),
            _evidence(),
            False,
        ),
        ("smoke-noop", RunResult("s", RunStatus.RUN_OK), _evidence(), False),
        ("smoke-readiness", NO_R1, _evidence(readiness=[{"type": "READY_FALSE"}]), False),
        ("smoke-noop", NO_R1, None, False),
    ],
)
def test_incident_free_smokes_accept_only_no_r1_without_incidents(
    scenario_id: str, result: RunResult, evidence: Any, accepted: bool
) -> None:
    assert accept(scenario_id, result, evidence, None).accepted is accepted


def test_the_chain_smoke_needs_the_chain_a_product_requirement_and_an_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "a.json"
    artifact.write_text("{}")
    ok = RunResult("smoke-chain", RunStatus.RUN_OK)
    good = _evidence(diagnoses=CHAIN, requirements=[{"diagnosis_id": 1}])
    assert accept("smoke-chain", ok, good, artifact).accepted
    for evidence, path in (
        (_evidence(diagnoses=CHAIN, requirements=[{"diagnosis_id": 2}]), artifact),
        (_evidence(diagnoses=CHAIN[:2], requirements=[{"diagnosis_id": 1}]), artifact),
        (good, tmp_path / "missing.json"),
    ):
        assert not accept("smoke-chain", ok, evidence, path).accepted
    assert not accept("smoke-chain", NO_R1, good, artifact).accepted


def test_evidence_capture_only_reads() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    at = datetime(2026, 9, 26, 12, tzinfo=UTC)
    incident = uuid4()
    with factory() as session:
        session.add(
            IncidentRow(
                incident_id=incident,
                status="OPEN",
                severity="CRITICAL",
                source="ALERTMANAGER",
                title="t",
                description=None,
                created_at=at,
                updated_at=at,
                correlation_id=uuid4(),
            )
        )
        session.add(
            EvidenceRequirementRow(
                requirement_key="k" * 64,
                incident_id=incident,
                diagnosis_id=1,
                hypothesis_key="h",
                rule_id="m16.ended-manifestation-episode",
                rule_version="v1",
                kind="STATUS_CONTINUITY",
                targets=[],
                not_before=at,
                status="OPEN",
            )
        )
        session.commit()

    def counts() -> dict[str, int]:
        with factory() as session:
            return {
                table.name: session.scalar(select(func.count()).select_from(table)) or 0
                for table in Base.metadata.sorted_tables
            }

    before = counts()
    urls: list[tuple[str, str]] = []

    def http(method: str, url: str, headers: Mapping[str, str]) -> Any:
        urls.append((method, url))
        return {"status": "success"}

    evidence = capture_evidence(
        factory,
        http,
        "http://prom",
        since=at - timedelta(minutes=1),
        until=at,
    )
    assert counts() == before
    assert [item["id"] for item in evidence["incidents"]] == [str(incident)]
    assert evidence["requirements"][0]["kind"] == "STATUS_CONTINUITY"
    ((method, url),) = urls
    assert method == "GET" and url.startswith("http://prom/api/v1/query_range?")


def test_the_cli_refuses_an_unknown_smoke_before_touching_anything() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/product_benchmark.py", "--smoke", "nope"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2 and "unknown smoke: nope" in result.stderr


def test_the_cli_opens_the_evidence_database_read_only() -> None:
    source = (ROOT / "scripts/product_benchmark.py").read_text()
    assert '"options": "-c default_transaction_read_only=on"' in source
