"""Bring-up helpers for the testbed lab (docs/architecture/testbed-control-plane-design.md §5, §4).

``python -m packages.connector.lab pki --out DIR`` writes the lab's static certificates (existing ones
are kept, so a running control plane is never invalidated by a re-run); ``python -m packages.connector.lab
alertmanager ...`` prints the lab's Alertmanager configuration, pointed at the connector's local
receiver with a bearer token. Neither touches a cluster; keys are written only under ``DIR``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from packages.connector.pki import (
    issue_connector,
    issue_server,
    new_ca,
    write_identity,
)

CONTROL_PLANE_NAMES = ("host.docker.internal", "localhost", "127.0.0.1")
CONNECTOR_ID = "lab"
RECEIVER = "connector"


def lab_pki(
    out: Path,
    *,
    hostnames: Sequence[str] = CONTROL_PLANE_NAMES,
    connector_id: str = CONNECTOR_ID,
) -> dict[str, Path]:
    """CA, control-plane server certificate and one connector certificate under ``out``.

    Files that already exist are kept. If any of the three identities is missing they are all
    issued together, so a half-present set never mixes authorities.
    """
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "ca": out / "ca.crt",
        "server_crt": out / "server.crt",
        "server_key": out / "server.key",
        "client_crt": out / "client.crt",
        "client_key": out / "client.key",
    }
    if all(path.exists() for path in paths.values()) and (out / "ca.key").exists():
        return paths
    ca = new_ca()
    write_identity(ca, out, "ca")
    write_identity(issue_server(ca, list(hostnames)), out, "server")
    write_identity(issue_connector(ca, connector_id), out, "client")
    return paths


def render_alertmanager(source_text: str, *, url: str, token: str) -> str:
    """The lab's Alertmanager configuration: one receiver, the connector's local webhook, a token.

    Everything else in the source (routing, grouping) is kept; only the receivers are replaced.
    """
    config: dict[str, Any] = yaml.safe_load(source_text)
    route = dict(config.get("route") or {})
    route["receiver"] = RECEIVER
    config["route"] = route
    config["receivers"] = [
        {
            "name": RECEIVER,
            "webhook_configs": [
                {
                    "url": url,
                    "send_resolved": True,
                    "http_config": {"authorization": {"type": "Bearer", "credentials": token}},
                }
            ],
        }
    ]
    return yaml.safe_dump(config, sort_keys=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="packages.connector.lab")
    commands = parser.add_subparsers(dest="command", required=True)
    pki = commands.add_parser("pki")
    pki.add_argument("--out", type=Path, required=True)
    alertmanager = commands.add_parser("alertmanager")
    alertmanager.add_argument("--source", type=Path, required=True)
    alertmanager.add_argument("--url", required=True)
    alertmanager.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "pki":
        for name, path in lab_pki(args.out).items():
            print(f"{name} {path}")
        return 0
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("the webhook token file is empty")
    print(render_alertmanager(args.source.read_text(encoding="utf-8"), url=args.url, token=token))
    return 0


__all__ = ["lab_pki", "main", "render_alertmanager"]


if __name__ == "__main__":
    raise SystemExit(main())
