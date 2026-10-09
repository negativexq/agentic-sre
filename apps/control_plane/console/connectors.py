"""The console's Connections page: the Connector registry, enrollment and preflight (docs/ui/connect-cluster-design.md).

It only reads and calls what A8 provides (registry, one-time token, revocation, the ``preflight`` wire op). Creating
and disabling a Connector hand out or withdraw cluster access, so they are refused unless the control plane has an
API token configured (decision 1); the CLI keeps working either way.
"""

from __future__ import annotations

import os
import re
import shlex
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apps.control_plane.auth import API_TOKEN_ENV, require_api_token
from packages.connector.client import ConnectorClient, ConnectorError
from packages.connector.enrollment import TOKEN_TTL, Registry
from packages.connector.transport import ConnectorGateway

_ID = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
CHART = "charts/agentic-sre-connector"


class ConnectorView(BaseModel):
    id: str
    status: str
    connected: bool
    certificate_valid_until: datetime | None = None
    renews_after: datetime | None = None
    enrolled_at: datetime | None = None
    created_at: datetime


class ConnectorsView(BaseModel):
    available: bool  # the control plane holds its CA's key: the registry exists
    writable: bool  # SRE_API_TOKEN is set: creating and disabling are allowed (decision 1)
    endpoints_configured: bool  # the public endpoints for the install command are set (decision 2)
    connectors: list[ConnectorView] = Field(default_factory=list)


class CreateConnector(BaseModel):
    id: str


class CreatedConnector(BaseModel):
    id: str
    token: str  # shown once; never listed afterwards
    expires_at: datetime
    install_command: str
    endpoints_configured: bool


class PreflightCheck(BaseModel):
    name: str
    status: str
    detail: str = ""


def install_command(connector_id: str, token: str) -> tuple[str, bool]:
    """The exact `helm` command for this Connector, with placeholders where the operator must fill in."""
    endpoint = os.getenv("SRE_CONNECTOR_PUBLIC_ENDPOINT", "")
    enroll = os.getenv("SRE_CONNECTOR_PUBLIC_ENROLL_ENDPOINT", "")
    server_name = os.getenv("SRE_CONNECTOR_PUBLIC_SERVER_NAME", "")
    configured = bool(endpoint and enroll)
    parts = [
        "helm upgrade --install",
        shlex.quote(connector_id),
        CHART,
        "--namespace agentic-sre-connector --create-namespace",
        f"--set controlPlane.endpoint={shlex.quote(endpoint or '<control-plane-host>:8443')}",
        f"--set controlPlane.enrollEndpoint={shlex.quote(enroll or '<control-plane-host>:8444')}",
        *([f"--set controlPlane.serverName={shlex.quote(server_name)}"] if server_name else []),
        f"--set enrollment.token={shlex.quote(token)}",
        "--set 'watch.namespaces={<your-namespace>}'",
        "--set backends.alertmanager.url=<alertmanager-url>",
        "--set backends.prometheus.url=<prometheus-url>",
        "--set backends.loki.url=<loki-url>",
        "--set backends.tempo.url=<tempo-url>",
    ]
    return " \\\n  ".join(parts), configured


def _writable() -> bool:
    return bool(os.environ.get(API_TOKEN_ENV))


def _require_writable() -> None:
    if not _writable():
        raise HTTPException(
            status_code=403,
            detail="managing Connectors from the console needs SRE_API_TOKEN; use `agentic-sre connector` instead",
        )


def create_connectors_router(
    registry: Registry | None, gateway: ConnectorGateway | None
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/console/connectors", tags=["console"])

    def need_registry() -> Registry:
        if registry is None:
            raise HTTPException(
                status_code=404, detail="no Connector registry: the control plane holds no CA key"
            )
        return registry

    @router.get("", response_model=ConnectorsView)
    def connectors() -> ConnectorsView:
        connected = gateway.connected() if gateway is not None else frozenset()
        if registry is None:
            return ConnectorsView(
                available=False,
                writable=_writable(),
                endpoints_configured=install_command("x", "x")[1],
            )
        views = []
        for record in registry.store.all():
            renews = None
            if record.cert_not_after is not None and record.token_used_at is not None:
                start = record.token_used_at
                renews = start + (record.cert_not_after - start) * 2 / 3
            views.append(
                ConnectorView(
                    id=record.connector_id,
                    status=record.status,
                    connected=record.connector_id in connected,
                    certificate_valid_until=(
                        gateway.certificate_expiry(record.connector_id)
                        if gateway is not None
                        else None
                    )
                    or record.cert_not_after,
                    renews_after=renews,
                    enrolled_at=record.token_used_at,
                    created_at=record.created_at,
                )
            )
        return ConnectorsView(
            available=True,
            writable=_writable(),
            endpoints_configured=install_command("x", "x")[1],
            connectors=views,
        )

    @router.post(
        "",
        response_model=CreatedConnector,
        status_code=201,
        dependencies=[Depends(require_api_token)],
    )
    def create(body: CreateConnector) -> CreatedConnector:
        _require_writable()
        reg = need_registry()
        if not _ID.match(body.id):
            raise HTTPException(
                status_code=422, detail="a Connector id is a DNS label: a-z, 0-9 and '-'"
            )
        try:
            token = reg.create(body.id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        command, configured = install_command(body.id, str(token))
        record = reg.store.get(body.id)
        expires = (
            record.token_expires_at
            if record is not None and record.token_expires_at
            else datetime.now(UTC) + TOKEN_TTL
        )
        return CreatedConnector(
            id=body.id,
            token=str(token),
            expires_at=expires,
            install_command=command,
            endpoints_configured=configured,
        )

    @router.post(
        "/{connector_id}/disable", status_code=204, dependencies=[Depends(require_api_token)]
    )
    def disable(connector_id: str) -> None:
        _require_writable()
        try:
            need_registry().disable(connector_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.post(
        "/{connector_id}/preflight",
        response_model=list[PreflightCheck],
        dependencies=[Depends(require_api_token)],
    )
    def preflight(connector_id: str) -> list[PreflightCheck]:
        if gateway is None or connector_id not in gateway.connected():
            raise HTTPException(status_code=409, detail=f"{connector_id} is not connected")
        try:
            answer: dict[str, Any] = ConnectorClient(gateway.transport(connector_id)).call(
                "preflight"
            )
        except ConnectorError as error:
            raise HTTPException(status_code=502, detail=f"preflight failed: {error}") from error
        return [PreflightCheck.model_validate(check) for check in answer.get("checks", [])]

    return router


__all__ = ["create_connectors_router", "install_command"]
