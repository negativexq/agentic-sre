#!/usr/bin/env python3
"""Run the internal live scenario suite against a running demo cluster."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict
from pathlib import Path

from packages.evals.live.actions import Context, port_forwards
from packages.evals.live.runner import RunOptions, restore_namespace, run_suite
from packages.evals.live.scenarios import SCENARIOS, Tier, scenarios_for


def _list_scenarios() -> int:
    width = max(len(item.id) for item in SCENARIOS)
    for scenario in SCENARIOS:
        flags = ",".join(
            filter(None, (scenario.tier.value, "demo" if scenario.demo else "")),
        )
        print(
            f"{scenario.id.ljust(width)}  {flags.ljust(13)} "
            f"alert={scenario.alert.ljust(30)} expect={scenario.expectation.label}"
        )
    root_cause = sum(item.expects_root_cause for item in SCENARIOS)
    print(
        f"\n{len(SCENARIOS)} scenarios: {root_cause} expect a root cause, "
        f"{len(SCENARIOS) - root_cause} expect abstention"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the suite and exit")
    parser.add_argument("--scenario", action="append", dest="scenarios", default=[])
    parser.add_argument("--tier", choices=[item.value for item in Tier])
    parser.add_argument("--demo-only", action="store_true")
    parser.add_argument("--namespace", default="sre-demo")
    parser.add_argument("--control-plane-url", default="http://localhost:18080")
    parser.add_argument("--order-url", default="http://localhost:18000")
    parser.add_argument("--payment-url", default="http://localhost:18001")
    parser.add_argument("--api-token", default="")
    parser.add_argument("--keep-fault", action="store_true", help="skip teardown after each run")
    parser.add_argument("--restore", action="store_true", help="undo leftovers and exit")
    parser.add_argument("--incident-timeout", type=float, default=300.0)
    parser.add_argument("--json", type=Path, help="write results as JSON")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--no-forward",
        action="store_true",
        help="assume the service ports are already reachable on localhost",
    )
    args = parser.parse_args()

    if args.list:
        return _list_scenarios()

    context = Context(
        namespace=args.namespace,
        order_url=args.order_url,
        payment_url=args.payment_url,
        control_plane_url=args.control_plane_url,
    )

    forwarding: AbstractContextManager[None] = nullcontext() if args.no_forward else port_forwards()

    if args.restore:
        with forwarding:
            restore_namespace(context)
        print("namespace restored")
        return 0

    selected = scenarios_for(
        tier=Tier(args.tier) if args.tier else None,
        demo_only=args.demo_only,
        ids=tuple(args.scenarios),
    )
    if not selected:
        print("no scenarios selected", file=sys.stderr)
        return 2

    options = RunOptions(
        incident_timeout_seconds=args.incident_timeout,
        api_token=args.api_token,
        keep_fault=args.keep_fault,
    )
    with forwarding:
        report = run_suite(
            selected,
            context,
            options,
            on_event=(lambda message: None) if args.quiet else (lambda message: print(message)),
        )
    print()
    print(report.render())

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([asdict(item) for item in report.results], indent=2, default=str) + "\n"
        )
        print(f"\nwrote {args.json}")

    return 0 if report.correct == len(report.graded) else 1


if __name__ == "__main__":
    raise SystemExit(main())
