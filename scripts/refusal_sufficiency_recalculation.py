"""Read-only paired replay for m21 §36.5 (2): every stored run, without §36 (2.14.0) against the current engine.

Every persisted diagnosis run plus each incident's widest evidence window is diagnosed twice from its frozen inputs;
only ``dependency_refusal_sufficient`` differs. Hard: no change at all in any run without the captures.
A run's own frozen captures are used as recorded (§36.8 applies to runs recorded from 2.17.0 on).
Usage: ``python -m scripts.refusal_sufficiency_recalculation OUT.jsonl [DATABASES]``.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from packages.rca import refusal_sufficiency
from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.replay import ReplaySource
from scripts.dependency_coverage_recalculation import decision, readonly_engine


def without_s36(*args: Any, **kwargs: Any) -> None:
    """2.14.0: the rule does not exist."""
    return None


def one(database: str) -> list[dict[str, Any]]:
    engine = readonly_engine(database)
    rows: list[dict[str, Any]] = []
    try:
        with engine.connect() as connection:
            widest = {
                str(r[0])
                for r in connection.execute(
                    text(
                        "select distinct on (incident_id) payload->>'run_id' from incident_events"
                        " where event_type = 'EVIDENCE_GATHERED'"
                        " order by incident_id, (payload->>'window_end') desc, event_id desc"
                    )
                )
            }
            recorded = {
                str(r[0])
                for r in connection.execute(
                    text("select run_id from diagnoses where run_id is not null")
                )
            }
        factory = sessionmaker(engine)
        for run in sorted(recorded | widest):
            try:
                source = ReplaySource.from_run(run, session_factory=factory)
                case = build_case(source, EngineConfig())
                with patch.object(
                    refusal_sufficiency, "dependency_refusal_sufficient", without_s36
                ):
                    before = decision(diagnose_case(case, config=EngineConfig()))
                after = decision(diagnose_case(case, config=EngineConfig()))
                rows.append(
                    {
                        "database": database,
                        "run": run,
                        "widest": run in widest,
                        "captured": bool(source.symptom_captures()),
                        "baseline": before,
                        "variant": after,
                        "changed": [key for key in before if before[key] != after[key]],
                    }
                )
            except Exception as error:
                rows.append({"database": database, "run": run, "error": repr(error)[:300]})
    finally:
        engine.dispose()
    return rows


def main() -> None:
    engine = readonly_engine("postgres")
    with engine.connect() as connection:
        databases = [
            r[0]
            for r in connection.execute(
                text("select datname from pg_database where datname like 'testbed_%' order by 1")
            )
        ]
    engine.dispose()
    if len(sys.argv) > 2:
        wanted = set(open(sys.argv[2]).read().split())
        databases = [d for d in databases if d in wanted]
    counts: Counter[str] = Counter()
    with open(sys.argv[1], "w") as output, ProcessPoolExecutor(4) as pool:
        for number, future in enumerate(as_completed([pool.submit(one, d) for d in databases]), 1):
            for row in future.result():
                output.write(json.dumps(row, default=str) + "\n")
                counts["rows"] += 1
                if "error" in row:
                    counts["errors"] += 1
                    continue
                counts["captured"] += row["captured"]
                counts["changed"] += bool(row["changed"])
                counts["changed_uncaptured"] += bool(row["changed"]) and not row["captured"]
                counts[f"{row['baseline']['resolution']} -> {row['variant']['resolution']}"] += 1
            if number % 25 == 0 or number == len(databases):
                print(number, len(databases), dict(counts), flush=True)


if __name__ == "__main__":
    main()
