"""Check C answers against stored calls available by the diagnosis cutoff.

This checks a finite stored population, not exhaustive source instrumentation.
Rows outside the frozen input are diagnostic only and never fed to the resolver.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from packages.rca.engine import EngineConfig, build_case
from packages.rca.replay import ReplaySource
from packages.rca.topology import WORKLOAD_KINDS, declared_hosts
from packages.storage.models import TraceObservationRow
from scripts.dependency_coverage_recalculation import readonly_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--dependency", default="kafka")
    args = parser.parse_args()
    catalog = set()
    for path in Path(".local/testbed").rglob("diagnoses.json"):
        for row in json.loads(path.read_text()):
            for alternative in row["document"].get("structural_alternatives", []):
                if alternative["actor"]["name"] == args.dependency:
                    catalog.add(alternative["alternative_id"])
    rows = [json.loads(line) for line in (args.directory / "rows.jsonl").read_text().splitlines()]
    candidates = [
        r
        for r in rows
        if "error" not in r
        and any(
            a["state"] == "ANSWERED_DECLARED_UNUSED" and a["alternative_id"] in catalog
            for a in r["baseline"]["answers"]
        )
    ]
    output = []
    for row in candidates:
        engine = readonly_engine(row["database"])
        try:
            factory = sessionmaker(engine)
            source = ReplaySource.from_run(row["run"], session_factory=factory)
            case = build_case(source, EngineConfig())
            seen = {(s.trace_id, s.span_id) for s in source.trace_observations()}
            checks = []
            closed_ids = {
                a["alternative_id"]
                for a in row["baseline"]["answers"]
                if a["state"] == "ANSWERED_DECLARED_UNUSED"
            }
            for alternative in case.structural_alternatives:
                if (
                    alternative.alternative_id not in closed_ids
                    or alternative.actor.name != args.dependency
                ):
                    continue
                hosts = {
                    caller.name: declared_hosts(
                        case.context.topology.latest, caller, alternative.actor
                    )
                    for caller in alternative.affected_entities
                    if caller.kind in WORKLOAD_KINDS
                }
                with factory() as session:
                    stored = session.scalars(
                        select(TraceObservationRow).where(
                            TraceObservationRow.incident_id == row["incident"],
                            TraceObservationRow.observed_at <= source.observation_cutoff(),
                            TraceObservationRow.event_at <= case.symptoms.onset,
                        )
                    ).all()
                calls = []
                for observation in stored:
                    span = observation.span
                    if (
                        span.get("service") in hosts
                        and span.get("span_kind") not in ("SERVER", "CONSUMER")
                        and span.get("semantic_attributes", {}).get("server.address")
                        in hosts[span["service"]]
                    ):
                        calls.append(
                            {
                                "observation_id": observation.observation_id,
                                "start_at": span.get("start_at"),
                                "observed_at": observation.observed_at.isoformat(),
                                "in_frozen_input": (span["trace_id"], span["span_id"]) in seen,
                            }
                        )
                checks.append({"alternative": alternative.alternative_id, "stored_calls": calls})
            output.append({"database": row["database"], "run": row["run"], "checks": checks})
        except Exception as error:
            output.append(
                {"database": row["database"], "run": row["run"], "error": type(error).__name__}
            )
        finally:
            engine.dispose()
    (args.directory / f"{args.dependency}_presence.json").write_text(json.dumps(output, indent=2))
    print(
        json.dumps(
            {
                "checked": len(output),
                "errors": sum("error" in r for r in output),
                "diagnoses_with_stored_call": sum(
                    any(c["stored_calls"] for c in r.get("checks", [])) for r in output
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
