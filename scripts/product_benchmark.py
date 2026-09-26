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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the scenarios and exit")
    args = parser.parse_args(argv)
    if args.list:
        return _list_scenarios()
    print("product scenario execution is not implemented yet (M19-6.7)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
