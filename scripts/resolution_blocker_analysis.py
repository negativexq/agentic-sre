"""Offline blocker reports from saved diagnosis JSON/JSONL or read-only testbed databases.

Examples:
  python -m scripts.resolution_blocker_analysis diagnoses.json --output blockers.json
  python -m scripts.resolution_blocker_analysis --database testbed_holdout23_config_b_config_image_payment_2 --output blockers.json
No diagnosis is recomputed, and no provider or planner is invoked.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from packages.evals.resolution_blockers import analyze_resolution_blockers, summarize_blockers


def documents(value: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if isinstance(value, list):
        return [row for item in value for row in documents(item)]
    if not isinstance(value, dict):
        raise ValueError("Expected diagnosis objects or a list of exported diagnoses")
    if "document" in value:
        return [
            (
                value["document"],
                {
                    k: value[k]
                    for k in ("run_id", "revision_number", "engine_version", "assessment_kind")
                    if k in value
                },
            )
        ]
    if "resolution" in value:
        return [(value, {})]
    raise ValueError("Input has no diagnosis document")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument(
        "--database", action="append", default=[], help="Read-only local testbed database name"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.inputs and not args.database:
        parser.error("Supply diagnosis files or --database")
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for path in args.inputs:
        content = path.read_text()
        values = (
            [json.loads(line) for line in content.splitlines() if line.strip()]
            if path.suffix == ".jsonl"
            else [json.loads(content)]
        )
        rows.extend(row for value in values for row in documents(value))
    for database in args.database:
        from sqlalchemy import text

        from scripts.dependency_coverage_recalculation import readonly_engine

        engine = readonly_engine(database)
        try:
            with engine.connect() as conn:
                if conn.execute(text("show transaction_read_only")).scalar() != "on":
                    raise RuntimeError("Blocker analysis requires a read-only database transaction")
                for row in conn.execute(
                    text(
                        "select document, run_id, revision_number, engine_version from diagnoses order by incident_id, revision_number"
                    )
                ):
                    rows.append(
                        (
                            row[0],
                            {
                                "run_id": str(row[1]) if row[1] else None,
                                "revision_number": row[2],
                                "engine_version": row[3],
                            },
                        )
                    )
        finally:
            engine.dispose()
    reports = [analyze_resolution_blockers(document, **metadata) for document, metadata in rows]
    payload = {
        "summary": summarize_blockers(reports),
        "revisions": [r.model_dump(mode="json") for r in reports],
    }
    output = json.dumps(payload, indent=2) + "\n"
    if args.output:
        if args.output.resolve() in {p.resolve() for p in args.inputs}:
            parser.error("Output must not overwrite an input diagnosis file")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        print(json.dumps(payload["summary"], indent=2))
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
