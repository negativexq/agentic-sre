"""Corroborate dependency log correlation against explicit frozen trace exports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.evals.frozen_request_binding import FrozenRequestEvidence, corroborate_frozen_requests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Output must not overwrite frozen source evidence")
    values = json.loads(args.input.read_text())
    rows = values if isinstance(values, list) else [values]
    evidence = [FrozenRequestEvidence.model_validate(row) for row in rows]
    reports = [corroborate_frozen_requests(row) for row in evidence]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"reports": [r.model_dump(mode="json") for r in reports]}, indent=2) + "\n"
    )
    print(json.dumps({"report_count": len(reports), "authority": "EXPLANATORY_ONLY"}))


if __name__ == "__main__":
    main()
