"""Shadow of the service-level effect relation (m21 contract §12.4) on testbed runs with captured traces.

For every run: the engine's own execution witnesses (``fault_executions`` over the run's events), the run's spans and
its incident services, read from the widest manifest of the run's control-plane database; the relation is computed for
every witnessed target pod and every symptom service, over the pre-registered grid, and judged against the world's
chain. Read-only; nothing feeds back into the engine.

``python -m packages.evals.service_effect_shadow tr4-slice1 tr4-slice3 ...`` (suite ids under ``.local/testbed``).
"""

from __future__ import annotations

import itertools
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from packages.evals.live.ground_truth import RunRecord
from packages.evals.live.testbed_lab import database_name
from packages.rca.fault_execution import fault_executions
from packages.rca.replay import ReplaySource
from packages.rca.service_effect import EffectParameters, call_pairs, service_effect

ROOT = Path(__file__).resolve().parents[2] / ".local" / "testbed"
PG = "postgresql+psycopg://postgres:postgres@127.0.0.1:55433/{}"
GRID = tuple(
    EffectParameters(n, f, timedelta(seconds=d))
    for n, f, d in itertools.product((3, 5), (3.0, 10.0), (0.2, 0.5))
)


def workload(pod: str) -> str:
    parts = pod.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else pod


def judge_run(record: RunRecord, source: Any) -> list[dict[str, Any]]:
    """One row per (witnessed target, symptom service) with the relation's value at every grid point."""
    chain_actors = record.chain.actors()
    on_chain_services = {
        a.split("/")[-1] for a in chain_actors if "/Service/" in a or "/Deployment/" in a
    }
    decoys = {d.actor for d in record.chain.decoys}
    spans = list(source.trace_observations())
    services = sorted({a.service for a in source.alerts() if a.service})
    rows = []
    for execution in fault_executions(list(source.events())):
        experiment = execution.ref.entity.canonical
        for target in execution.targets:
            if target.applied_at is None:
                continue
            end = target.recovered_at or source.observation_cutoff()
            target_service = workload(target.pod.name)
            for symptom in services:
                if symptom == target_service:
                    continue  # the target is itself the symptom: the direct relation, not this one
                pairs = call_pairs(spans, symptom, target_service)
                values = [
                    service_effect(
                        pairs,
                        target_pod=target.pod.name,
                        start=target.applied_at,
                        end=end,
                        parameters=parameters,
                    )
                    for parameters in GRID
                ]
                on_chain = target.pod.canonical in chain_actors and symptom in on_chain_services
                rows.append(
                    {
                        "experiment": experiment,
                        "target": target.pod.canonical,
                        "symptom": symptom,
                        "truth": "decoy"
                        if experiment in decoys
                        else ("chain" if on_chain else "off-chain"),
                        "values": values,
                        "pairs": len(pairs),
                    }
                )
    return rows


def widest_runs(database: str) -> list[str]:
    """Each incident's widest diagnosis run (the latest window end): every incident is judged on its own."""
    engine = create_engine(PG.format(database))
    try:
        with engine.connect() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    text(
                        "select distinct on (incident_id) payload->>'run_id' from incident_events"
                        " where event_type = 'EVIDENCE_GATHERED'"
                        " order by incident_id, (payload->>'window_end') desc"
                    )
                )
            ]
    finally:
        engine.dispose()


def main(suites: Sequence[str]) -> int:
    totals: Counter[str] = Counter()
    for suite in suites:
        manifest = json.loads((ROOT / suite / "manifest.json").read_text())
        for spec in manifest["scenarios"]:
            for repeat in range(spec["repeats"]):
                directory = ROOT / suite / spec["scenario_id"] / str(repeat)
                if not (directory / "run.json").exists():
                    continue
                record = RunRecord.model_validate_json((directory / "run.json").read_bytes())
                if not record.valid:
                    continue
                database = database_name(f"{suite}-{spec['scenario_id']}-{repeat}")
                engine = create_engine(PG.format(database))
                rows = []
                try:
                    for run_id in widest_runs(database):
                        source = ReplaySource.from_run(run_id, session_factory=sessionmaker(engine))
                        rows += judge_run(record, source)
                finally:
                    engine.dispose()
                for row in rows:
                    print(
                        f"{suite}#{repeat} {row['truth']:9} {row['target'].split('/')[-1]:36} -> "
                        f"{row['symptom']:16} pairs={row['pairs']:4} "
                        + "".join({True: "+", False: "-", None: "?"}[v] for v in row["values"])
                    )
                    for index, value in enumerate(row["values"]):
                        totals[f"{index}:{row['truth']}:{value}"] += 1
    print("\ngrid (N, F, D): holds / not / unknown, per truth")
    for index, parameters in enumerate(GRID):
        cells = []
        for truth in ("chain", "off-chain", "decoy"):
            cells.append(
                f"{truth} {totals[f'{index}:{truth}:True']}/{totals[f'{index}:{truth}:False']}/"
                f"{totals[f'{index}:{truth}:None']}"
            )
        print(
            f"  N={parameters.calls} F={parameters.factor:g} D={parameters.floor.total_seconds():g}s  "
            + "  ".join(cells)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
