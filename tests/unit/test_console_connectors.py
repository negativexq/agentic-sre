"""The console's Connections page API (docs/ui/connect-cluster-design.md, D4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.control_plane.console.connectors import create_connectors_router
from packages.connector.client import in_process_transport
from packages.connector.enrollment import MemoryStore, Registry
from packages.connector.pki import new_ca, new_key_and_request
from packages.connector.preflight import Check
from packages.connector.service import Connector

World = tuple[Registry, TestClient]

AUTH = {"Authorization": "Bearer console-secret"}


class Gateway:
    """The parts of ConnectorGateway the page reads."""

    def __init__(self, connector: Connector, connected: set[str]) -> None:
        self._connector, self._connected = connector, connected

    def connected(self) -> frozenset[str]:
        return frozenset(self._connected)

    def certificate_expiry(self, connector_id: str) -> datetime | None:
        return datetime(2027, 1, 7, tzinfo=UTC) if connector_id in self._connected else None

    def transport(self, connector_id: str):  # type: ignore[no-untyped-def]
        return in_process_transport(self._connector)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    for name in ("SRE_CONNECTOR_PUBLIC_ENDPOINT", "SRE_CONNECTOR_PUBLIC_ENROLL_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SRE_API_TOKEN", "console-secret")
    registry = Registry(MemoryStore(), new_ca())
    connector = Connector(preflight_runner=lambda: [Check("loki", "failed", "HTTP 503")])
    gateway = Gateway(connector, {"prod-eu"})
    app = FastAPI()
    app.include_router(create_connectors_router(registry, gateway))  # type: ignore[arg-type]
    return registry, TestClient(app)


def test_a_token_is_returned_once_and_never_listed(world: World) -> None:
    registry, client = world
    created = client.post("/api/v1/console/connectors", json={"id": "prod-eu"}, headers=AUTH)
    assert created.status_code == 201
    body = created.json()
    assert (
        body["token"].startswith("prod-eu.")
        and "--set enrollment.token=" in body["install_command"]
    )
    assert (
        "<control-plane-host>:8443" in body["install_command"] and not body["endpoints_configured"]
    )
    listing = client.get("/api/v1/console/connectors").json()
    assert listing["available"] and listing["writable"]
    assert [c["status"] for c in listing["connectors"]] == ["pending"]
    assert body["token"] not in client.get("/api/v1/console/connectors").text


def test_without_an_api_token_the_console_cannot_hand_out_or_withdraw_access(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client = world
    monkeypatch.delenv("SRE_API_TOKEN")
    assert client.post("/api/v1/console/connectors", json={"id": "x"}).status_code == 403
    assert client.post("/api/v1/console/connectors/x/disable").status_code == 403
    assert client.get("/api/v1/console/connectors").json()["writable"] is False


def test_a_wrong_bearer_token_is_refused(world: World) -> None:
    _, client = world
    wrong = {"Authorization": "Bearer nope"}
    assert (
        client.post("/api/v1/console/connectors", json={"id": "x"}, headers=wrong).status_code
        == 401
    )


def test_bad_or_repeated_ids_are_refused(world: World) -> None:
    registry, client = world
    assert (
        client.post("/api/v1/console/connectors", json={"id": "Prod EU"}, headers=AUTH).status_code
        == 422
    )
    registry.enroll(str(registry.create("prod-eu")), new_key_and_request("prod-eu")[1])
    assert (
        client.post("/api/v1/console/connectors", json={"id": "prod-eu"}, headers=AUTH).status_code
        == 409
    )


def test_an_active_connected_connector_shows_its_certificate_and_renewal_and_can_be_disabled(
    world: World,
) -> None:
    registry, client = world
    registry.enroll(str(registry.create("prod-eu")), new_key_and_request("prod-eu")[1])
    (row,) = client.get("/api/v1/console/connectors").json()["connectors"]
    assert row["status"] == "active" and row["connected"] is True
    assert row["certificate_valid_until"].startswith("2027-01-07")
    enrolled = datetime.fromisoformat(row["enrolled_at"])
    renews = datetime.fromisoformat(row["renews_after"])
    assert timedelta(days=55) < renews - enrolled < timedelta(days=65)
    assert (
        client.post("/api/v1/console/connectors/prod-eu/disable", headers=AUTH).status_code == 204
    )
    assert not registry.is_allowed("prod-eu")
    assert client.post("/api/v1/console/connectors/nobody/disable", headers=AUTH).status_code == 404


def test_preflight_is_relayed_for_a_connected_connector_only(world: World) -> None:
    _, client = world
    checks = client.post("/api/v1/console/connectors/prod-eu/preflight", headers=AUTH).json()
    assert checks == [{"name": "loki", "status": "failed", "detail": "HTTP 503"}]
    assert (
        client.post("/api/v1/console/connectors/other/preflight", headers=AUTH).status_code == 409
    )


def test_the_install_command_carries_the_public_endpoints_when_set(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client = world
    monkeypatch.setenv("SRE_CONNECTOR_PUBLIC_ENDPOINT", "sre.example.com:8443")
    monkeypatch.setenv("SRE_CONNECTOR_PUBLIC_ENROLL_ENDPOINT", "sre.example.com:8444")
    body = client.post("/api/v1/console/connectors", json={"id": "prod-us"}, headers=AUTH).json()
    assert body["endpoints_configured"]
    assert "controlPlane.endpoint=sre.example.com:8443" in body["install_command"]


def test_without_a_registry_the_page_says_so() -> None:
    app = FastAPI()
    app.include_router(create_connectors_router(None, None))
    body = TestClient(app).get("/api/v1/console/connectors").json()
    assert body["available"] is False and body["connectors"] == []
