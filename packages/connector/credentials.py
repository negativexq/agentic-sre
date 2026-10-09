"""Where a Connector in Kubernetes keeps its key and certificate (connector-install-design.md §4 decision 3).

One Secret in the Connector's **own** namespace, created empty by the chart; the Connector may only ``get`` and
``update`` that one Secret (RBAC by ``resourceNames``). On start its contents are written to the files the session
reads; after an enrollment or a renewal the files are written back. Secrets of the watched namespaces are never read.
"""

from __future__ import annotations

import base64
import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
# Secret key -> the environment variable naming the file the session reads
KEYS = {
    "tls.crt": "SRE_CONNECTOR_TLS_CERT",
    "tls.key": "SRE_CONNECTOR_TLS_KEY",
    "ca.crt": "SRE_CONNECTOR_TLS_CA",
}


class SecretApi(Protocol):
    def read(self) -> dict[str, bytes]: ...

    def write(self, data: dict[str, bytes]) -> None: ...


class KubernetesSecret:
    """``SecretApi`` over one Secret, in-cluster, with get and update only."""

    def __init__(self, name: str, namespace: str | None = None) -> None:
        kubernetes: Any = importlib.import_module("kubernetes")
        importlib.import_module("kubernetes.config")
        kubernetes.config.load_incluster_config()
        self._core = kubernetes.client.CoreV1Api()
        self._name = name
        self._namespace = namespace or NAMESPACE_FILE.read_text().strip()

    def read(self) -> dict[str, bytes]:
        secret = self._core.read_namespaced_secret(self._name, self._namespace)
        return {key: base64.b64decode(value) for key, value in (secret.data or {}).items()}

    def write(self, data: dict[str, bytes]) -> None:
        secret = self._core.read_namespaced_secret(self._name, self._namespace)
        secret.data = {key: base64.b64encode(value).decode() for key, value in data.items()}
        self._core.replace_namespaced_secret(self._name, self._namespace, secret)


def restore(secret: SecretApi, env: Mapping[str, str]) -> bool:
    """Write a stored identity to the session's files; returns whether one was stored."""
    data = secret.read()
    if not all(data.get(key) for key in KEYS):
        return False
    for key, variable in KEYS.items():
        path = Path(env[variable])
        path.parent.mkdir(parents=True, exist_ok=True)
        if key == "tls.key":
            path.touch(mode=0o600)
        path.write_bytes(data[key])
    return True


def save(secret: SecretApi, env: Mapping[str, str]) -> None:
    """Store the session's current identity (after an enrollment or a renewal)."""
    secret.write({key: Path(env[variable]).read_bytes() for key, variable in KEYS.items()})


__all__ = ["KEYS", "KubernetesSecret", "SecretApi", "restore", "save"]
