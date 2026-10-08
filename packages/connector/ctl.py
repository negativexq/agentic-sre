"""``connectorctl``: operator commands for a Connector (roadmap A8; connector-install-design.md).

``python -m packages.connector.ctl preflight [--json]`` checks, with the Connector's environment, that every backend
is reachable and every permission is exactly right; it exits 1 when a configured check fails.
"""

from __future__ import annotations

import argparse
import json
import sys

from packages.connector.preflight import as_dicts, failed, render, run_preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="connectorctl")
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight", help="probe every backend and permission")
    preflight.add_argument("--json", action="store_true", help="print the checks as JSON")
    preflight.add_argument(
        "--as",
        dest="impersonate",
        help="ask the Kubernetes API as this user, e.g. system:serviceaccount:connector:connector",
    )
    args = parser.parse_args(argv)
    checks = run_preflight(impersonate=args.impersonate)
    print(json.dumps(as_dicts(checks), indent=2) if args.json else render(checks))
    return 1 if failed(checks) else 0


if __name__ == "__main__":
    sys.exit(main())
