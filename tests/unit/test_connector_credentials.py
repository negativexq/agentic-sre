"""A Connector in Kubernetes keeps its identity in one Secret of its own namespace (design §4 decision 3)."""

from __future__ import annotations

import os
from pathlib import Path

from packages.connector.credentials import restore, save


class Secret:
    def __init__(self, data: dict[str, bytes] | None = None) -> None:
        self.data = dict(data or {})
        self.writes = 0

    def read(self) -> dict[str, bytes]:
        return dict(self.data)

    def write(self, data: dict[str, bytes]) -> None:
        self.data = dict(data)
        self.writes += 1


def env_in(directory: Path) -> dict[str, str]:
    return {
        "SRE_CONNECTOR_TLS_CERT": str(directory / "tls" / "tls.crt"),
        "SRE_CONNECTOR_TLS_KEY": str(directory / "tls" / "tls.key"),
        "SRE_CONNECTOR_TLS_CA": str(directory / "tls" / "ca.crt"),
    }


def test_an_empty_secret_restores_nothing(tmp_path: Path) -> None:
    env = env_in(tmp_path)
    assert restore(Secret(), env) is False
    assert not Path(env["SRE_CONNECTOR_TLS_CERT"]).exists()


def test_a_saved_identity_is_restored_after_a_restart_with_an_owner_only_key(
    tmp_path: Path,
) -> None:
    env = env_in(tmp_path)
    for variable, content in zip(env.values(), (b"CERT", b"KEY", b"CA"), strict=True):
        Path(variable).parent.mkdir(parents=True, exist_ok=True)
        Path(variable).write_bytes(content)
    secret = Secret()
    save(secret, env)
    assert secret.data == {"tls.crt": b"CERT", "tls.key": b"KEY", "ca.crt": b"CA"}
    fresh = env_in(tmp_path / "restarted")  # a new pod: an empty in-memory volume
    assert restore(secret, fresh) is True
    key = Path(fresh["SRE_CONNECTOR_TLS_KEY"])
    assert key.read_bytes() == b"KEY" and oct(os.stat(key).st_mode & 0o777) == "0o600"


def test_a_partial_secret_is_not_trusted(tmp_path: Path) -> None:
    assert restore(Secret({"tls.crt": b"CERT"}), env_in(tmp_path)) is False
