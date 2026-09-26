"""M19-6.1: workload ``/ready`` is process readiness only; ``/health`` is unchanged."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from workload.common.adapters import EventPublisher, PaymentGateway
from workload.order_service.app import create_app as create_order_app
from workload.payment_service.app import create_app as create_payment_app

ROOT = Path(__file__).resolve().parents[2]
APPS = {
    "order": ROOT / "workload" / "order_service" / "app.py",
    "payment": ROOT / "workload" / "payment_service" / "app.py",
}
# Everything a handler could reach a dependency through inside ``create_app``.
DEPENDENCY_NAMES = {
    "session_factory",
    "payment_gateway",
    "event_publisher",
    "gateway",
    "publisher",
    "service",
    "order_service",
    "payment_service",
    "telemetry",
    "registry",
}


class Broken:
    """A dependency that is down: every use raises and is recorded."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def _fail(self, name: str) -> Any:
        self.touched.append(name)
        raise ConnectionError(f"dependency unavailable: {name}")

    def __call__(self, *_: Any, **__: Any) -> Any:
        return self._fail("session")

    def charge(self, *_: Any, **__: Any) -> Any:
        return self._fail("payment")

    def publish(self, *_: Any, **__: Any) -> Any:
        return self._fail("publish")


def _order(broken: Broken) -> FastAPI:
    return create_order_app(
        cast(sessionmaker[Session], broken),
        payment_gateway=cast(PaymentGateway, broken),
        event_publisher=cast(EventPublisher, broken),
    )


def _payment(broken: Broken) -> FastAPI:
    return create_payment_app(cast(sessionmaker[Session], broken))


BUILDERS = [pytest.param(_order, id="order"), pytest.param(_payment, id="payment")]


@pytest.mark.parametrize("build", BUILDERS)
def test_ready_is_200_while_the_process_runs(build: Any) -> None:
    client = TestClient(build(Broken()))
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.parametrize("build", BUILDERS)
def test_health_is_unchanged(build: Any) -> None:
    client = TestClient(build(Broken()))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_order_ready_ignores_an_unavailable_database_payment_and_broker() -> None:
    broken = Broken()
    client = TestClient(_order(broken), raise_server_exceptions=False)
    # The dependencies really are down: the order route fails on them.
    failed = client.post(
        "/orders", json={"customer_id": "customer-0001", "amount_cents": 100, "currency": "USD"}
    )
    assert failed.status_code == 500 and broken.touched == ["session"]
    broken.touched.clear()
    for _ in range(3):
        assert client.get("/ready").status_code == 200
    assert broken.touched == []  # readiness never reached a dependency


def test_payment_ready_ignores_an_unavailable_database() -> None:
    broken = Broken()
    client = TestClient(_payment(broken), raise_server_exceptions=False)
    failed = client.post(
        "/payments",
        json={
            "order_id": "11111111-1111-4111-8111-111111111111",
            "amount_cents": 100,
            "currency": "USD",
        },
    )
    assert failed.status_code == 500 and broken.touched == ["session"]
    broken.touched.clear()
    for _ in range(3):
        assert client.get("/ready").status_code == 200
    assert broken.touched == []


def _handler(path: Path, name: str) -> ast.FunctionDef:
    (factory,) = [
        node
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    ]
    (handler,) = [
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    return handler


@pytest.mark.parametrize("service", sorted(APPS))
def test_ready_handler_references_no_dependency(service: str) -> None:
    handler = _handler(APPS[service], "ready")
    route = handler.decorator_list[0]
    assert isinstance(route, ast.Call) and isinstance(route.func, ast.Attribute)
    assert route.func.attr == "get" and ast.literal_eval(route.args[0]) == "/ready"
    referenced = {node.id for node in ast.walk(handler) if isinstance(node, ast.Name)}
    assert not referenced & DEPENDENCY_NAMES
    # /ready is its own handler; it neither calls nor is routed through /health.
    assert "health" not in referenced
