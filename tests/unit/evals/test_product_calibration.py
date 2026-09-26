"""M19-7.C: one caller-given candidate, one raw measurement — no search, tuning or correction."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_product_runner import ROOT

from packages.evals.product import calibration
from packages.evals.product.actions import PodIdentity, SetResources
from packages.evals.product.calibration import Candidate, Window, measure, write_record
from packages.evals.product.runner import DRY_RUN_EPOCH, RecordingControl
from packages.rca.investigation.prometheus import PrometheusMetricsReader
from packages.rca.model import EntityRef, ResourcePressure

T0 = DRY_RUN_EPOCH


def _pressure(container: str, resource: str, peak: float) -> ResourcePressure:
    return ResourcePressure(
        pod=EntityRef(kind="Pod", name="order-service-a", namespace="sre-demo"),
        container=container,
        resource=resource,
        baseline=0.01,
        peak=peak,
        at=T0,
        evidence_id=f"prometheus:{container}:{resource}",
        sample_count=40,
        sample_start=T0,
        sample_end=T0,
    )


class Control(RecordingControl):
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        super().__init__(events)
        self.service_urls = {"order-service": "http://o"}

    def post(self, url: str, payload: dict[str, Any]) -> None:
        self._events.append(("post", url, payload))

    def pod_of(self, deployment: str) -> PodIdentity:
        return PodIdentity(f"{deployment}-a", "uid-a")


class Backend:
    def __init__(self, fail_at: str | None = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self._control = Control(self.events)
        self.fail_at = fail_at
        self.urls: list[str] = []

    def _step(self, name: str) -> None:
        self.events.append((name,))
        if name == self.fail_at:
            raise RuntimeError(f"{name} failed")

    def cluster_up(self) -> None:
        self._step("cluster_up")

    def deploy_observability_and_workload(self) -> None:
        self._step("deploy")

    def wait_workload_ready(self) -> None:
        self._step("ready")

    def start_fresh_db_and_control_plane(self) -> None:
        self._step("control_plane")

    def control(self) -> Control:
        return self._control

    def evidence(self) -> Any:
        return object()

    def set_traffic(self, enabled: bool) -> None:
        self.events.append(("traffic", enabled))

    def http(self, method: str, url: str, headers: Any) -> Any:
        self.urls.append(url)
        return {"status": "success", "data": []}

    def cluster_down(self) -> None:
        self.events.append(("cluster_down",))


@pytest.fixture
def verified(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(calibration, "await_evidence", lambda *args: calls.append("await"))
    monkeypatch.setattr(
        SetResources, "verify", lambda self, evidence, receipt: calls.append("verify")
    )
    return calls


def _run(backend: Backend, candidate: Candidate, reads: list[Any]) -> dict[str, Any]:
    def reader(pod: EntityRef, start: Any, end: Any) -> tuple[ResourcePressure, ...]:
        reads.append((pod.canonical, start, end))
        return (
            _pressure("order-service", "cpu", 0.137),
            _pressure("order-service", "cpu", 0.412),
            _pressure("order-service", "memory", 0.9),
            _pressure("sidecar", "cpu", 0.99),
        )

    return measure(
        backend,  # type: ignore[arg-type]
        candidate,
        Window(settle=timedelta(minutes=2), measure=timedelta(minutes=10)),
        prometheus_url="http://prom",
        reader=reader,
        host_stats=lambda: [{"Name": "agentic-sre-control-plane", "CPUPerc": "250%"}],
        provenance={"code_commit": "a" * 40},
    )


def test_one_candidate_is_applied_verified_and_measured_raw(verified: list[str]) -> None:
    backend = Backend()
    reads: list[Any] = []
    record = _run(backend, Candidate("cpu", limit="50m", request="50m"), reads)
    names = [event[0] for event in backend.events]
    assert names[:4] == ["cluster_up", "deploy", "ready", "control_plane"]
    assert verified == ["await", "verify"]  # SetResources verified before traffic
    on = backend.events.index(("traffic", True))
    assert names.index("patch_resources") < on < len(backend.events) - 3
    assert [event for event in backend.events[on:] if event[0] == "wait"][:2] == [
        ("wait", timedelta(minutes=2)),
        ("wait", timedelta(minutes=10)),
    ]  # settle and window both run with traffic on
    assert (
        "patch_resources",
        "order-service",
        "order-service",
        {"cpu": "50m"},
        {"cpu": "50m"},
    ) in backend.events
    waits = [event[1] for event in backend.events if event[0] == "wait"]
    assert waits[-2:] == [timedelta(minutes=2), timedelta(minutes=10)]
    ((pod, start, end),) = reads
    assert pod == "sre-demo/Pod/order-service-a"
    assert end - start == timedelta(minutes=10)
    assert (record["window_start"], record["window_end"]) == (start.isoformat(), end.isoformat())
    # Only this container and resource, exactly as the product reader reported it.
    assert [item["peak"] for item in record["raw"]] == [0.137, 0.412]
    assert record["raw_peak"] == 0.412
    assert "container_cpu_cfs_throttled_periods_total" in record["query"]
    assert record["traffic"]["rate_per_second"] == 2
    assert record["host"]["before"][0]["CPUPerc"] == "250%"
    assert any("/api/v1/targets" in url for url in backend.urls)
    assert names[-2:] == ["traffic", "cluster_down"] and backend.events[-2] == ("traffic", False)


def test_a_ballast_goes_through_the_existing_fault_endpoint(verified: list[str]) -> None:
    backend = Backend()
    record = _run(backend, Candidate("memory", limit="256Mi", ballast_mb=200), [])
    (post,) = [event for event in backend.events if event[0] == "post"]
    assert post[1] == "http://o/__faults"
    assert post[2]["memory_ballast_mb"] == 200 and post[2]["not_ready"] is False
    assert record["raw_peak"] == 0.9
    assert "container_memory_working_set_bytes" in record["query"]


def test_a_failed_step_still_tears_the_cluster_down(verified: list[str]) -> None:
    backend = Backend(fail_at="deploy")
    with pytest.raises(RuntimeError, match="deploy failed"):
        _run(backend, Candidate("cpu", limit="50m"), [])
    assert backend.events[-1] == ("cluster_down",)
    assert not any(event[0] == "traffic" for event in backend.events)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resource": "cpu"},
        {"resource": "disk", "limit": "1"},
        {"resource": "cpu", "request": "50m"},
        {"resource": "memory", "ballast_mb": -1},
    ],
)
def test_invalid_candidates_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Candidate(**kwargs)


def test_the_window_is_bounded_by_the_readers_limit() -> None:
    with pytest.raises(ValueError):
        Window(measure=timedelta(hours=2))
    with pytest.raises(ValueError):
        Window(measure=timedelta(0))


def test_records_are_create_only(tmp_path: Path) -> None:
    record = {
        "candidate": {
            "resource": "cpu",
            "service": "order-service",
            "container": "order-service",
            "limit": "50m",
            "request": None,
            "ballast_mb": None,
        },
        "raw_peak": 0.2,
    }
    path = write_record(tmp_path, "20260926T120000000000Z", record)
    assert (
        path
        == tmp_path / "calibration" / "20260926T120000000000Z" / "cpu_order-service_limit-50m.json"
    )
    with pytest.raises(FileExistsError):
        write_record(tmp_path, "20260926T120000000000Z", record)


def test_the_measurement_uses_the_products_a2_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def query(self: Any, target: EntityRef, query: Any) -> tuple[ResourcePressure, ...]:
        seen.append((target.canonical, query.start, query.end, self.config.base_url))
        return (_pressure("order-service", "cpu", 0.2),)

    monkeypatch.setattr(PrometheusMetricsReader, "query_resource_pressure", query)
    pod = EntityRef(kind="Pod", name="p", namespace="sre-demo")
    records = calibration.product_reader("http://prom")(pod, T0, T0 + timedelta(minutes=5))
    # The provider boundary called the product reader once, unchanged.
    assert seen == [("sre-demo/Pod/p", T0, T0 + timedelta(minutes=5), "http://prom")]
    assert [item.peak for item in records] == [0.2]


def test_no_hidden_correction_or_search_in_the_harness() -> None:
    import ast  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    tree = ast.parse((ROOT / "packages/evals/product/calibration.py").read_text())
    arithmetic = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div | ast.Mult | ast.FloorDiv)
    ]
    assert arithmetic == []  # the observed peak is never scaled or corrected
    parameters = inspect.signature(measure).parameters
    assert parameters["candidate"].annotation in (Candidate, "Candidate")  # one candidate per run


def test_the_cli_refuses_an_invalid_candidate_before_touching_anything() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/product_benchmark.py", "--measure", "cpu"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2 and "invalid candidate" in result.stderr
