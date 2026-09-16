#!/usr/bin/env python3
"""Execute the separately approved single live smoke after fail-closed preflight."""

from __future__ import annotations

import argparse
from pathlib import Path

from packages.evals.itbench.live_smoke_execution import execute_live_smoke


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    execute_live_smoke(
        args.root,
        args.manifest,
        args.ledger,
        args.result,
        args.run_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
