"""M19-6.2: ``not_ready`` and ``memory_ballast_mb`` workload test faults.

Ballast is checked through the buffer the application owns, never through
process RSS, which the allocator may not return immediately.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_workload_readiness import Broken, _order, _payment
from workload.common.faults import MIB, MemoryBallast
from workload.order_service.app import OrderFaultConfig
from workload.payment_service.app import FaultConfig

Build = Callable[[Broken], FaultConfig | FastAPI]
BUILDERS = [pytest.param(_order, id="order"), pytest.param(_payment, id="payment")]


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_TEST_FAULTS", "true")


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _ballast(app: FastAPI) -> MemoryBallast:
    ballast = app.state.memory_ballast
    assert isinstance(ballast, MemoryBallast)
    return ballast


def _held(app: FastAPI) -> int:
    buffer = _ballast(app).buffer
    return len(buffer) if buffer is not None else 0


def _set(client: TestClient, **faults: Any) -> Any:
    return client.post("/__faults", json=faults)


def _alive(client: TestClient) -> None:
    health = client.get("/health")
    assert (health.status_code, health.json()) == (200, {"status": "ok"})
    assert client.get("/metrics").status_code == 200  # the process still serves


def _counting(app: FastAPI) -> list[int]:
    sizes: list[int] = []

    def allocate(size: int) -> bytearray:
        sizes.append(size)
        return bytearray(size)

    _ballast(app).allocate = allocate
    return sizes


# --- defaults and compatibility ---------------------------------------------------


@pytest.mark.parametrize("model", [OrderFaultConfig, FaultConfig])
def test_new_fault_fields_default_to_inactive(model: type[Any]) -> None:
    config = model()
    assert config.not_ready is False
    assert config.memory_ballast_mb == 0


@pytest.mark.parametrize(
    ("build", "legacy"),
    [
        pytest.param(_order, {"delay_ms": 5, "error": False, "db_query_delay_ms": 0}, id="order"),
        pytest.param(
            _payment,
            {"delay_ms": 5, "error": False, "db_hold_ms": 0, "db_query_delay_ms": 0},
            id="payment",
        ),
    ],
)
def test_pre_m19_payloads_still_apply_and_reset_the_new_faults(
    enabled: None, build: Any, legacy: dict[str, Any]
) -> None:
    app = build(Broken())
    client = _client(app)
    assert _set(client, **legacy, not_ready=True, memory_ballast_mb=1).status_code == 200
    response = _set(client, **legacy)
    assert response.status_code == 200
    assert response.json() == {**legacy, "not_ready": False, "memory_ballast_mb": 0}
    assert client.get("/ready").status_code == 200 and _held(app) == 0


# --- not_ready ----------------------------------------------------------------


@pytest.mark.parametrize("build", BUILDERS)
def test_not_ready_fails_readiness_only(enabled: None, build: Any) -> None:
    app = build(Broken())
    client = _client(app)
    assert client.get("/ready").status_code == 200
    assert _set(client, not_ready=True).status_code == 200
    for _ in range(2):
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "not ready (test fault)"}
        _alive(client)
    assert _set(client, not_ready=False).status_code == 200
    assert client.get("/ready").json() == {"status": "ready"}


@pytest.mark.parametrize("build", BUILDERS)
def test_readiness_fault_stays_dependency_free(enabled: None, build: Any) -> None:
    broken = Broken()
    client = _client(build(broken))
    assert client.get("/ready").status_code == 200
    assert _set(client, not_ready=True).status_code == 200
    assert client.get("/ready").status_code == 503
    assert broken.touched == []  # neither answer consulted a dependency


def test_payment_with_faults_disabled_never_applies_the_new_faults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_TEST_FAULTS", "false")
    app = _payment(Broken())
    client = _client(app)
    assert _set(client, not_ready=True, memory_ballast_mb=1).status_code == 404
    assert client.get("/ready").status_code == 200
    assert _held(app) == 0
    _alive(client)


def test_order_readiness_fault_follows_enablement_and_disable_releases_ballast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order reads ENABLE_TEST_FAULTS per request (its existing convention)."""
    monkeypatch.setenv("ENABLE_TEST_FAULTS", "true")
    app = _order(Broken())
    client = _client(app)
    assert _set(client, not_ready=True, memory_ballast_mb=2).status_code == 200
    assert client.get("/ready").status_code == 503 and _held(app) == 2 * MIB
    monkeypatch.setenv("ENABLE_TEST_FAULTS", "false")
    assert client.get("/ready").status_code == 200  # stored not_ready has no effect
    assert _set(client, memory_ballast_mb=2).status_code == 404
    assert _held(app) == 0  # no deliberate ballast while faults are off
    monkeypatch.setenv("ENABLE_TEST_FAULTS", "true")
    assert client.get("/ready").status_code == 503  # the stored fault applies again
    assert _set(client, not_ready=True, memory_ballast_mb=2).status_code == 200
    assert _held(app) == 2 * MIB


# --- memory ballast -----------------------------------------------------------


@pytest.mark.parametrize("build", BUILDERS)
def test_ballast_holds_exact_mib_resizes_and_releases(enabled: None, build: Any) -> None:
    app = build(Broken())
    client = _client(app)
    allocations = _counting(app)
    assert _held(app) == 0
    assert _set(client, memory_ballast_mb=2).json()["memory_ballast_mb"] == 2
    assert _held(app) == 2 * 1024 * 1024
    first = _ballast(app).buffer
    assert _set(client, memory_ballast_mb=2).status_code == 200
    assert _held(app) == 2 * MIB and _ballast(app).buffer is first  # same value: no growth
    assert _set(client, memory_ballast_mb=1).status_code == 200
    assert _set(client, memory_ballast_mb=3).status_code == 200
    assert _held(app) == 3 * MIB  # replaced, not 1 + 3
    assert _set(client, memory_ballast_mb=0).status_code == 200
    assert _ballast(app).buffer is None and _held(app) == 0
    assert allocations == [2 * MIB, 1 * MIB, 3 * MIB]


@pytest.mark.parametrize("build", BUILDERS)
def test_readiness_and_health_polls_never_allocate(enabled: None, build: Any) -> None:
    app = build(Broken())
    client = _client(app)
    assert _set(client, memory_ballast_mb=1, not_ready=True).status_code == 200
    allocations = _counting(app)
    held = _ballast(app).buffer
    for _ in range(5):
        client.get("/ready")
        client.get("/health")
    assert allocations == [] and _ballast(app).buffer is held


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("bad", [-1, True, "2", 1.5, None])
def test_invalid_ballast_is_rejected_and_state_stays_coherent(
    enabled: None, build: Any, bad: Any
) -> None:
    app = build(Broken())
    client = _client(app)
    assert _set(client, memory_ballast_mb=2).status_code == 200
    assert _set(client, memory_ballast_mb=bad, not_ready=True).status_code == 422
    assert _held(app) == 2 * MIB
    assert client.get("/ready").status_code == 200  # the rejected update applied nothing


@pytest.mark.parametrize("build", BUILDERS)
def test_failed_allocation_applies_nothing(enabled: None, build: Any) -> None:
    app = build(Broken())
    client = _client(app)
    assert _set(client, memory_ballast_mb=1).status_code == 200
    held = _ballast(app).buffer

    def exhausted(size: int) -> bytearray:
        raise MemoryError(size)

    _ballast(app).allocate = exhausted
    failed = _set(client, memory_ballast_mb=3, not_ready=True)
    assert failed.status_code == 503
    assert failed.json() == {"detail": "memory ballast allocation failed"}
    assert _ballast(app).buffer is held and _held(app) == 1 * MIB
    assert client.get("/ready").status_code == 200  # not_ready was not applied either
    _ballast(app).allocate = bytearray
    # A size no process can address fails the same way, without touching memory.
    assert _set(client, memory_ballast_mb=10**13).status_code == 503
    assert _held(app) == 1 * MIB


@pytest.mark.parametrize("build", BUILDERS)
def test_both_faults_coexist_and_health_stays_ok(enabled: None, build: Any) -> None:
    app = build(Broken())
    client = _client(app)
    for faults in (
        {},
        {"not_ready": True},
        {"memory_ballast_mb": 2},
        {"not_ready": True, "memory_ballast_mb": 2},
    ):
        assert _set(client, **faults).status_code == 200
        _alive(client)
    assert client.get("/ready").status_code == 503
    assert _held(app) == 2 * MIB


def test_ballast_is_owned_per_application(enabled: None) -> None:
    first, second, order = _payment(Broken()), _payment(Broken()), _order(Broken())
    assert _set(_client(first), memory_ballast_mb=2).status_code == 200
    assert (_held(first), _held(second), _held(order)) == (2 * MIB, 0, 0)
    assert _set(_client(order), not_ready=True).status_code == 200
    assert _client(second).get("/ready").status_code == 200


def test_existing_fault_fields_keep_their_meaning(enabled: None) -> None:
    client = _client(_payment(Broken()))
    response = _set(client, delay_ms=0, error=True, db_hold_ms=0, db_query_delay_ms=0)
    assert response.json()["error"] is True
    assert (
        client.post(
            "/payments",
            json={
                "order_id": "11111111-1111-4111-8111-111111111111",
                "amount_cents": 1,
                "currency": "USD",
            },
        ).status_code
        == 500
    )  # the existing error fault still fires first
    assert client.get("/ready").status_code == 200  # and is not a readiness fault
