#!/usr/bin/env python3
"""Product-resolution benchmark: each scenario on a fresh cluster (M19)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from packages.evals.product.scenarios import scenarios


def _list_scenarios() -> int:
    registered = scenarios()
    for scenario in registered:
        proofs = ",".join(proof.value for proof in scenario.expectation.proofs) or "-"
        print(f"{scenario.scenario_id}  phases={len(scenario.phases)} proofs={proofs}")
    print(f"{len(registered)} product scenarios registered")
    return 0


def _dry_run(selected: Sequence[str]) -> int:
    by_id = {scenario.scenario_id: scenario for scenario in scenarios()}
    unknown = [item for item in selected if item not in by_id]
    if unknown:
        print(f"unknown product scenario: {', '.join(unknown)}", file=sys.stderr)
        return 2
    from packages.evals.product.runner import ProductRunner, RecordingBackend  # noqa: PLC0415

    worst = 0
    for scenario_id in selected:
        backend = RecordingBackend()
        result = ProductRunner(backend).run(by_id[scenario_id])
        print(f"{scenario_id}: {result.status.value} (dry-run)")
        for event in backend.events:
            print("  " + " ".join(str(part) for part in event))
        worst = max(worst, 0 if result.status.value == "RUN_OK" else 1)
    return worst


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the scenarios and exit")
    parser.add_argument("--scenario", action="append", default=[], help="scenario id (repeatable)")
    parser.add_argument(
        "--dry-run", action="store_true", help="record the fresh-cluster lifecycle; no effects"
    )
    args = parser.parse_args(argv)
    if args.list:
        return _list_scenarios()
    if args.dry_run:
        if not args.scenario:
            print("--dry-run needs at least one --scenario", file=sys.stderr)
            return 2
        return _dry_run(args.scenario)
    print(
        "live product scenario execution is not implemented yet (accepted with M19-6.12)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
