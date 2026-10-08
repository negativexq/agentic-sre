"""The settings screen reports the evidence backends as the system status does (connector mode included)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.control_plane.console.router import create_console_router
from apps.control_plane.console.settings import read_settings


@pytest.fixture(autouse=True)
def _no_backend_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PROMETHEUS_URL", "SRE_LOKI_URL", "TEMPO_URL"):
        monkeypatch.delenv(name, raising=False)


def backends(view: object) -> tuple[bool, bool, bool]:
    return (view.prometheus_configured, view.loki_configured, view.tempo_configured)  # type: ignore[attr-defined]


def test_through_a_connector_the_backends_come_from_its_capabilities() -> None:
    view = read_settings(frozenset({"resource_pressure", "logs", "runtime_traces", "changes"}))
    assert backends(view) == (True, True, True)
    partial = read_settings(frozenset({"traffic", "changes"}))
    assert backends(partial) == (True, False, False)


def test_a_connector_that_reports_nothing_configures_nothing_whatever_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    assert backends(read_settings(frozenset())) == (False, False, False)


def test_without_a_connector_the_environment_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    assert backends(read_settings()) == (False, False, False)
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("TEMPO_URL", "http://tempo:3200")
    assert backends(read_settings()) == (True, False, True)


def test_the_settings_endpoint_reads_the_connector_capabilities() -> None:
    app = FastAPI()
    app.include_router(
        create_console_router(
            lambda: iter(()),  # the settings endpoint needs no session
            lambda _session: None,  # type: ignore[arg-type,return-value]
            None,  # type: ignore[arg-type]
            connector_capabilities=lambda: frozenset({"logs"}),
        )
    )
    body = TestClient(app).get("/api/v1/console/settings").json()
    assert (body["prometheus_configured"], body["loki_configured"], body["tempo_configured"]) == (
        False,
        True,
        False,
    )
