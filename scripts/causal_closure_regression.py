"""Seen-set regression: snapshot inventory, active bounded reads and offline tape replay.

No label enters prediction. Scores are entity-label matches, not mechanism truth.
Run: PYTHONPATH=. .venv/bin/python scripts/causal_closure_regression.py --out PATH
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from packages.evals.causal_recording import ReadTape, RecordedSeed
from packages.evals.itbench.benchmark import _matches, load_split
from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.rca.claims import actor_findings, symptom_links
from packages.rca.engine import RCA_ENGINE_VERSION, build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.state import SEED_FULL_SOURCE, InvestigationConfig
from packages.rca.root_cause_eligibility import episode_source_capable_initiating_findings
from packages.rca.source import ObservationSource


def run(job: tuple[str, str, str]) -> dict[str, Any]:
    dataset_path, sid, output = job
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    ds = ITBenchLiteDataset.open(Path(dataset_path))
    src = SnapshotSource(ds.scenario(sid))
    case = build_case(src)
    diagnosis = diagnose_case(case)
    trace = diagnosis.resolution_trace
    assert trace is not None
    audit = {a.hypothesis_id: a for a in trace.hypothesis_audits}
    rivals = []
    for h in case.hypotheses:
        if h.hypothesis_id not in {*trace.unresolved_hypotheses, *trace.plausible_hypotheses}:
            continue
        own = episode_source_capable_initiating_findings(h)
        bridges = [
            b
            for b in case.runtime_mechanism_bridges.bridges
            if b.target_entity == h.causal_actor or b.mechanism_entity == h.causal_actor
        ]
        rivals.append(
            {
                "claim": h.model_dump(mode="json"),
                "audit": audit[h.hypothesis_id].model_dump(mode="json"),
                "competes_with": [i for i in trace.plausible_hypotheses if i != h.hypothesis_id],
                "classification": "OWN_INITIATING_EVIDENCE_NOT_PROOF_OF_INDEPENDENCE"
                if own
                else "UNRESOLVED_ROLE_NOT_CONFIRMED_INDEPENDENT",
                "same_actor_episode_claims": [
                    o.hypothesis_id
                    for o in case.hypotheses
                    if o.hypothesis_id != h.hypothesis_id
                    and o.causal_actor == h.causal_actor
                    and o.episode_onset == h.episode_onset
                ],
                "incident_paths": [
                    [hop.model_dump(mode="json") for hop in path] for _, path in symptom_links(h)
                ],
                "local_evidence_ids": sorted(
                    {e for f in actor_findings(h) for e in f.evidence_ids}
                ),
                "observed_bridges": [b.model_dump(mode="json") for b in bridges],
                "needed_evidence": "actor-instance operation/execution record tied to the observed failure; structural bridge alone is insufficient",
                "available_in_record": "STRUCTURAL_BRIDGE_ONLY"
                if bridges
                else "NO_EXECUTION_ATTRIBUTION_IDENTIFIED",
                "readable_questions": [
                    g.model_dump(mode="json")
                    for g in diagnosis.information_gaps
                    if h.hypothesis_id in g.hypothesis_ids
                ],
            }
        )
    (out / f"{sid}-snapshot.json").write_text(
        json.dumps({"diagnosis": diagnosis.model_dump(mode="json"), "rivals": rivals})
    )
    # The source contains only the bounded initial view. FULL_SOURCE prevents a
    # second seed projection; RecordedSeed retains initial_observation_bounded.
    config = InvestigationConfig(seed_mode=SEED_FULL_SOURCE, max_wall_time_seconds=600)
    started = datetime.now(UTC)
    tape = ReadTape(src)
    result = investigate_diagnosis(
        cast(ObservationSource, RecordedSeed(tape)),
        policy=DeterministicIntentPolicy(),
        config=config,
        started_at=started,
    )
    tape_path = out / f"{sid}-reads.json"
    tape.save(tape_path)
    (out / f"{sid}-active.json").write_text(result.model_dump_json())
    # Freshly deserialize responses; no snapshot, provider or label is reachable.
    replay_tape = ReadTape.load(tape_path)
    assert result.replay_contract is not None
    replay = investigate_diagnosis(
        cast(ObservationSource, RecordedSeed(replay_tape)),
        policy=DeterministicIntentPolicy(),
        config=config,
        started_at=started,
        recorded_terminal=result.replay_contract.terminal,
    )
    replay_tape.assert_consumed()
    transitions = [
        (a.causal_decision_before, a.causal_decision_after) for a in result.action_audits
    ]
    replay_transitions = [
        (a.causal_decision_before, a.causal_decision_after) for a in replay.action_audits
    ]
    # Ground-truth boundary, after both predictions and offline replay.
    truth = ds.load_ground_truth(sid)
    observed = set(src.object_history())
    observed.update(e.entity for e in src.events())
    matches = sorted(e.canonical for e in observed if _matches(e.canonical, truth))
    roots = {h.hypothesis_id for h in case.hypotheses if _matches(h.causal_actor.canonical, truth)}
    final_trace = result.diagnosis.resolution_trace
    assert final_trace is not None
    return {
        "scenario": sid,
        "engine": RCA_ENGINE_VERSION,
        "scoreable": bool(matches),
        "matching_observed_entities": matches,
        "root_preserved": bool(roots),
        "root_admitted": bool(roots.intersection(trace.admitted_hypotheses)),
        "matching_support": len(roots.intersection(trace.plausible_hypotheses)),
        "nonmatching_support": len(set(trace.plausible_hypotheses) - roots),
        "matching_mechanism": len(roots.intersection(trace.mechanism_verified_hypotheses)),
        "nonmatching_mechanism": len(set(trace.mechanism_verified_hypotheses) - roots),
        "diagnosis": trace.diagnosis_status,
        "resolution": diagnosis.resolution.value,
        "explained_claims": len(trace.explained_hypotheses),
        "unresolved_role_claims": len(trace.unresolved_hypotheses),
        "frontier_answered": sum(
            a.state == "ANSWERED_ROLE_TRANSFERRED" for a in trace.frontier_answers
        ),
        "frontier_open": sum(a.state == "OPEN" for a in trace.frontier_answers),
        "active_initial": result.initial_diagnosis.resolution_trace.diagnosis_status
        if result.initial_diagnosis and result.initial_diagnosis.resolution_trace
        else None,
        "active_final": final_trace.diagnosis_status,
        "active_resolution": result.diagnosis.resolution.value,
        "queries": result.tool_calls,
        "decision_changing_queries": sum(
            bool(a.decision_state_changed) for a in result.action_audits
        ),
        "provider_calls": 0,
        "dollar_cost": 0,
        "stop": result.stop_reason.value,
        "source_replay_parity": diagnosis_epistemic_digest(result.diagnosis)
        == diagnosis_epistemic_digest(replay.diagnosis),
        "transition_replay_parity": transitions == replay_transitions,
        "recorded_calls": len(tape.rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=".local/itbench-lite")
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--scenario")
    args = parser.parse_args()
    ids = (
        [args.scenario]
        if args.scenario
        else [s for split in ("dev", "test") for s in load_split(split)]
    )
    rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(run, (args.dataset, s, args.out)) for s in ids]):
            row = future.result()
            rows.append(row)
            Path(args.out, "summary.json").write_text(
                json.dumps(sorted(rows, key=lambda r: r["scenario"]), indent=2)
            )
            print(
                row["scenario"],
                row["diagnosis"],
                row["queries"],
                row["source_replay_parity"],
                flush=True,
            )


if __name__ == "__main__":
    main()
