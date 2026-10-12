"""Read-only 2.11.0 versus fail-closed sole-ready recalculation of frozen runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from packages.rca import resolution
from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.replay import ReplaySource
from scripts.dependency_coverage_recalculation import decision, readonly_engine
from scripts.sole_ready_policy import baseline_sole_ready, readiness_gaps


def one(database: str) -> list[dict[str, Any]]:
    engine = readonly_engine(database)
    results = []
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
                str(r[0]): {"version": r[1], "resolution": r[2]}
                for r in connection.execute(
                    text(
                        "select run_id, engine_version, document->>'resolution' from diagnoses"
                        " where run_id is not null order by run_id"
                    )
                )
            }
        factory = sessionmaker(engine)
        for run in sorted(set(recorded) | widest):
            try:
                source = ReplaySource.from_run(run, session_factory=factory)
                config = EngineConfig()
                case = build_case(source, config)
                with patch.object(resolution, "_sole_ready", baseline_sole_ready):
                    before = decision(diagnose_case(case, config=config))
                denied: list[dict[str, Any]] = []

                def candidate(
                    pod: Any,
                    others: Any,
                    statuses: Any,
                    start: Any,
                    end: Any,
                    _denied: Any = denied,
                ) -> bool:
                    if not baseline_sole_ready(pod, others, statuses, start, end):
                        return False
                    gaps = readiness_gaps(pod, others, statuses, start, end)
                    if gaps:
                        _denied.append({"witnessed_pod": pod.canonical, "other_pods": gaps})
                    return not gaps

                with patch.object(resolution, "_sole_ready", candidate):
                    after = decision(diagnose_case(case, config=config))
                changes = [key for key in before if before[key] != after[key]]
                if changes:
                    with patch.object(resolution, "_sole_ready", baseline_sole_ready):
                        assert decision(diagnose_case(case, config=config)) == before
                unique_denials = {json.dumps(d, sort_keys=True): d for d in denied}
                results.append(
                    {
                        "database": database,
                        "run": run,
                        "incident": case.incident_id,
                        "widest": run in widest,
                        "recorded": recorded.get(run),
                        "alerts": sorted({a.name for a in source.alerts()}),
                        "baseline": before,
                        "variant": after,
                        "changed": changes,
                        "denials": list(unique_denials.values()),
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "database": database,
                        "run": run,
                        "widest": run in widest,
                        "recorded": recorded.get(run),
                        "error": type(error).__name__,
                    }
                )
    finally:
        engine.dispose()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    engine = readonly_engine("postgres")
    with engine.connect() as connection:
        databases = [
            r[0]
            for r in connection.execute(
                text(
                    "select datname from pg_database where datname like 'testbed_%' order by datname"
                )
            )
        ]
    engine.dispose()
    if args.limit:
        databases = databases[: args.limit]
    args.output.mkdir(parents=True, exist_ok=True)
    provenance = {
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "kind": "paired_cross_version_recalculation_not_ordinary_replay",
        "population": "all persisted diagnosis runs plus widest evidence windows",
        "databases": len(databases),
        "baseline": "frozen sole-ready predicate of 2.11.0",
        "database_access": "default_transaction_read_only=on",
        "scripts": {
            path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in (
                "scripts/sole_ready_policy.py",
                "scripts/sole_ready_recalculation.py",
            )
        },
    }
    (args.output / "provenance.json").write_text(json.dumps(provenance, indent=2))
    counters: dict[str, Counter[str]] = {"all": Counter(), "widest": Counter()}
    losses = []
    with (
        (args.output / "rows.jsonl").open("w") as output,
        ProcessPoolExecutor(args.workers) as pool,
    ):
        futures = {pool.submit(one, db): db for db in databases}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                rows = future.result()
            except Exception as error:
                rows = [{"database": futures[future], "error": type(error).__name__}]
            for row in rows:
                output.write(json.dumps(row, default=str) + "\n")
                for population in ("all", "widest") if row.get("widest") else ("all",):
                    count = counters[population]
                    count["rows"] += 1
                    if "error" in row:
                        count["errors"] += 1
                        continue
                    count["coverage_denials"] += bool(row["denials"])
                    for key in row["changed"]:
                        count[f"changed_{key}"] += 1
                    count[f"{row['baseline']['resolution']} -> {row['variant']['resolution']}"] += 1
                if (
                    "error" not in row
                    and row["baseline"]["resolution"] == "RESOLVED"
                    and row["variant"]["resolution"] != "RESOLVED"
                ):
                    losses.append(row)
            output.flush()
            (args.output / "summary.json").write_text(
                json.dumps({k: dict(v) for k, v in counters.items()}, indent=2)
            )
            (args.output / "lost_resolutions.json").write_text(
                json.dumps(losses, indent=2, default=str)
            )
            if number % 10 == 0 or number == len(databases):
                print(
                    f"{number}/{len(databases)} databases; {counters['all']['rows']} runs; "
                    f"{counters['all']['errors']} errors; {len(losses)} lost resolutions",
                    flush=True,
                )


if __name__ == "__main__":
    main()
