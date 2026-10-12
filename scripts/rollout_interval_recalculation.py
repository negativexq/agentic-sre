"""Read-only paired recalculation for m21 §35: §28's interval for chaos witnesses only (2.13.0) against the engine's.

Every persisted diagnosis run plus each incident's widest evidence window is diagnosed twice from its frozen inputs;
only ``_execution_interval`` differs. Each strongly supported claim's symptoms left uncovered are recorded on both
sides. Usage: ``python -m scripts.rollout_interval_recalculation OUT.jsonl [DATABASES]``.
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

from packages.rca import resolution
from packages.rca.engine import Case, EngineConfig, _selected_pods, build_case, diagnose_case
from packages.rca.model import (
    CausalWitness,
    Diagnosis,
    EntityRef,
    ObjectVersion,
    RootSupportStatus,
)
from packages.rca.replay import ReplaySource
from scripts.dependency_coverage_recalculation import decision, readonly_engine

ENGINE_INTERVAL = resolution._execution_interval
History = Mapping[EntityRef, Sequence[ObjectVersion]] | None


def chaos_only(
    witness: CausalWitness, history: History = None, cutoff: datetime | None = None
) -> tuple[datetime, datetime] | None:
    """2.13.0: an interval only from a fault's own execution records."""
    return ENGINE_INTERVAL(witness)


def uncovered(
    case: Case, source: ReplaySource, diagnosis: Diagnosis, journal: bool
) -> dict[str, list[str]]:
    trace = diagnosis.resolution_trace
    if trace is None:
        return {}
    selects = _selected_pods(case.context.topology)
    owned = {e.source: e.target for e in case.context.topology.edges if e.relation == "owned_by"}
    statuses = source.pod_status_observations()
    history = source.object_history() if journal else None
    cutoff = source.observation_cutoff() if journal else None
    holders = {h.hypothesis_id: h for h in case.hypotheses}
    result: dict[str, list[str]] = {}
    for audit in trace.hypothesis_audits:
        if audit.hypothesis_id not in trace.mechanism_verified_hypotheses:
            continue
        witnesses = [
            w
            for r in audit.root_support
            if r.status is RootSupportStatus.FIRED and r.support_kind == "OBSERVED_MECHANISM_CAUSE"
            for w in r.witnesses
        ]
        covered = resolution._covered_symptoms(witnesses, owned, selects, statuses, history, cutoff)
        result[audit.hypothesis_id] = sorted(
            e.canonical for e in holders[audit.hypothesis_id].symptom_entities if e not in covered
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
                source = ReplaySource.from_run(run, session_factory=factory)
                case = build_case(source, EngineConfig())
                with patch.object(resolution, "_execution_interval", chaos_only):
                    first = diagnose_case(case, config=EngineConfig())
                    before, open_before = decision(first), uncovered(case, source, first, False)
                second = diagnose_case(case, config=EngineConfig())
                after, open_after = decision(second), uncovered(case, source, second, True)
                rows.append(
                    {
                        "database": database,
                        "run": run,
                        "widest": run in widest,
                        "baseline": before,
                        "variant": after,
                        "changed": [key for key in before if before[key] != after[key]],
                        "uncovered_before": open_before,
                        "uncovered_after": open_after,
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
                coverage = row["uncovered_before"] != row["uncovered_after"]
                counts["coverage_changed"] += coverage
                counts["coverage_changed_widest"] += coverage and row["widest"]
            if number % 25 == 0 or number == len(databases):
                print(number, len(databases), dict(counts), flush=True)


if __name__ == "__main__":
    main()
