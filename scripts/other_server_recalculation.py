"""Read-only paired recalculation for m21 §34: §23's replica predicate of 2.12.0, frozen, against the engine's.

Every persisted diagnosis run plus each incident's widest evidence window is diagnosed twice from its frozen inputs;
only ``other_server_ready`` differs. Usage: ``python -m scripts.other_server_recalculation OUT.jsonl [DATABASES]``.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from typing import Any
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from packages.rca import causal_closure
from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.model import EntityRef, ObjectVersion, PodStatusObservation
from packages.rca.replay import ReplaySource
from packages.rca.service_effect import other_server_ready
from scripts.dependency_coverage_recalculation import decision, readonly_engine
from scripts.other_server_policy import baseline_other_ready

History = Mapping[EntityRef, Sequence[ObjectVersion]] | None
CALLS: list[dict[str, Any]] = []


def frozen(
    statuses: Sequence[PodStatusObservation],
    target_pod: str,
    namespace: str,
    start: datetime,
    end: datetime,
    history: History = None,
) -> bool | None:
    return baseline_other_ready(statuses, target_pod, namespace, start, end)


def corrected(
    statuses: Sequence[PodStatusObservation],
    target_pod: str,
    namespace: str,
    start: datetime,
    end: datetime,
    history: History = None,
) -> bool | None:
    result = other_server_ready(statuses, target_pod, namespace, start, end, history)
    CALLS.append(
        {
            "target": f"{namespace}/{target_pod}",
            "baseline": baseline_other_ready(statuses, target_pod, namespace, start, end),
            "variant": result,
        }
    )
    return result


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
                case = build_case(
                    ReplaySource.from_run(run, session_factory=factory), EngineConfig()
                )
                with patch.object(causal_closure, "other_server_ready", frozen):
                    before = decision(diagnose_case(case, config=EngineConfig()))
                CALLS.clear()
                with patch.object(causal_closure, "other_server_ready", corrected):
                    after = decision(diagnose_case(case, config=EngineConfig()))
                changed = [key for key in before if before[key] != after[key]]
                rows.append(
                    {
                        "database": database,
                        "run": run,
                        "widest": run in widest,
                        "baseline": before,
                        "variant": after,
                        "changed": changed,
                        "calls": [
                            c
                            for c in CALLS
                            if c["baseline"] != c["variant"] or c["baseline"] is False
                        ],
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
                counts[f"{row['baseline']['resolution']} -> {row['variant']['resolution']}"] += 1
                counts["changed"] += bool(row["changed"])
                counts["calls_differ"] += sum(c["baseline"] != c["variant"] for c in row["calls"])
            if number % 25 == 0 or number == len(databases):
                print(number, len(databases), dict(counts), flush=True)


if __name__ == "__main__":
    main()
