"""Read only exact frozen LOG/TRACE members of one local testbed run."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from packages.evals.manifest_request_evidence import load_manifest_request_evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--episode-start", type=datetime.fromisoformat, required=True)
    parser.add_argument("--episode-end", type=datetime.fromisoformat, required=True)
    parser.add_argument("--caller-service", required=True)
    parser.add_argument("--configured-peer", required=True)
    parser.add_argument("--claim-id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args()
    if args.evidence_output and args.output.resolve() == args.evidence_output.resolve():
        parser.error("Report and frozen evidence outputs must be separate")
    from scripts.dependency_coverage_recalculation import readonly_engine

    engine = readonly_engine(args.database)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            result = load_manifest_request_evidence(
                connection,
                run_id=args.run_id,
                episode_start=args.episode_start,
                episode_end=args.episode_end,
                caller_service=args.caller_service,
                configured_peer=args.configured_peer,
                claim_id=args.claim_id,
            )
    finally:
        engine.dispose()
    payload = result.report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    if args.evidence_output:
        args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_output.write_text(result.evidence.model_dump_json(indent=2) + "\n")
    print(
        json.dumps(
            {"run_id": args.run_id, "manifest_membership": payload["report"]["manifest_membership"]}
        )
    )


if __name__ == "__main__":
    main()
