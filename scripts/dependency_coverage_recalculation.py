"""Paired, read-only recalculation: engine 2.11.0 with and without C closure.

This is an audit variant, not version-compatible replay or a production engine.
Every incident contributes its widest recorded evidence window. Only the negative
C answer is withheld; A, D1, witness rules and missing-input behavior are untouched.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from packages.evals.service_effect_shadow import PG
from packages.rca import frontier
from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.model import Diagnosis
from packages.rca.replay import ReplaySource


def readonly_engine(database: str):  # type: ignore[no-untyped-def]
    return create_engine(
        PG.format(database),
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


def without_negative_closure(*args, **kwargs):  # type: ignore[no-untyped-def]
    answer = ORIGINAL_C(*args, **kwargs)
    if answer is not None and answer.state == "ANSWERED_DECLARED_UNUSED":
        return answer.model_copy(
            update={
                "state": "OPEN",
                "blocked_reason": "TRACE_COVERAGE_NOT_PROVEN",
                "remaining_uncertainty": ("UPSTREAM_ROLE_UNDETERMINED",),
            }
        )
    return answer


ORIGINAL_C = frontier.dependency_declared_unused


def decision(diagnosis: Diagnosis) -> dict[str, Any]:
    trace = diagnosis.resolution_trace
    return {
        "resolution": diagnosis.resolution.value,
        "root": diagnosis.root_cause.canonical if diagnosis.root_cause else None,
        "display": diagnosis.leading_actor_display,
        "tier": diagnosis.leading_actor_tier,
        "digest": diagnosis_epistemic_digest(diagnosis),
        "support": sorted(trace.plausible_hypotheses) if trace else [],
        "strong": sorted(trace.mechanism_verified_hypotheses) if trace else [],
        "witnesses": [
            r.model_dump(mode="json")
            for a in trace.hypothesis_audits
            for r in a.root_support
            if r.support_kind == "OBSERVED_MECHANISM_CAUSE"
        ]
        if trace
        else [],
        "frontier": sorted(trace.material_frontier_ids) if trace else [],
        "answers": [a.model_dump(mode="json") for a in trace.frontier_answers] if trace else [],
    }


def one(database: str) -> list[dict[str, Any]]:
    engine = readonly_engine(database)
    rows = []
    try:
        with engine.connect() as connection:
            runs = [
                str(r[0])
                for r in connection.execute(
                    text(
                        "select distinct on (incident_id) payload->>'run_id' from incident_events"
                        " where event_type = 'EVIDENCE_GATHERED'"
                        " order by incident_id, (payload->>'window_end') desc, event_id desc"
                    )
                )
            ]
        factory = sessionmaker(engine)
        for run in runs:
            try:
                source = ReplaySource.from_run(run, session_factory=factory)
                config = EngineConfig()
                case = build_case(source, config)
                baseline = decision(diagnose_case(case, config=config))
                with patch.object(frontier, "dependency_declared_unused", without_negative_closure):
                    variant = decision(diagnose_case(case, config=config))
                changed = [key for key in baseline if baseline[key] != variant[key]]
                # Check repeatability after the scoped patch on every affected case.
                repeated = decision(diagnose_case(case, config=config)) if changed else baseline
                if repeated != baseline:
                    raise ValueError("baseline is not repeatable after the scoped variant")
                c_answers = [
                    a for a in baseline["answers"] if a["state"] == "ANSWERED_DECLARED_UNUSED"
                ]
                rows.append(
                    {
                        "database": database,
                        "run": run,
                        "incident": case.incident_id,
                        "alerts": sorted({a.name for a in source.alerts()}),
                        "changed": changed,
                        "baseline": baseline,
                        "variant": variant,
                        "trace_reads": [
                            r.model_dump(mode="json") for r in source.trace_read_record() or ()
                        ]
                        if c_answers
                        else [],
                        "trace_reads_present": source.trace_read_record() is not None,
                    }
                )
            except Exception as error:
                # Error text may contain connection credentials; retain only the class.
                rows.append({"database": database, "run": run, "error": type(error).__name__})
    finally:
        engine.dispose()
    return rows


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
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "kind": "paired_recalculation_not_version_compatible_replay",
        "population": "widest evidence window per incident in each testbed database",
        "database_count": len(databases),
        "workers": args.workers,
    }
    (args.output / "provenance.json").write_text(json.dumps(provenance, indent=2))
    counts: Counter[str] = Counter()
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
                counts["rows"] += 1
                if "error" in row:
                    counts["errors"] += 1
                else:
                    counts["digest_changed"] += "digest" in row["changed"]
                    counts["resolution_changed"] += "resolution" in row["changed"]
                    for key in ("root", "support", "strong", "witnesses", "display", "tier"):
                        counts[f"{key}_changed"] += key in row["changed"]
                    counts[
                        f"transition_{row['baseline']['resolution']}_to_{row['variant']['resolution']}"
                    ] += 1
            output.flush()
            (args.output / "summary.json").write_text(json.dumps(dict(counts), indent=2))
            print(
                f"{number}/{len(databases)} databases, {counts['rows']} rows, "
                f"{counts['errors']} errors, {counts['resolution_changed']} resolution changes",
                flush=True,
            )


if __name__ == "__main__":
    main()
