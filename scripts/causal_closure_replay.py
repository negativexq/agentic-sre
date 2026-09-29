"""Verify saved active trajectories using only serialized seed/read responses."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import cast

from packages.evals.causal_recording import ReadTape, RecordedSeed
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.state import investigation_config_from_document
from packages.rca.model import InvestigationResult
from packages.rca.source import ObservationSource


def replay(path: Path) -> dict[str, object]:
    expected = InvestigationResult.model_validate_json(path.read_text())
    sid = path.name.removesuffix("-active.json")
    tape = ReadTape.load(path.with_name(sid + "-reads.json"))
    assert expected.replay_contract is not None
    actual = investigate_diagnosis(
        cast(ObservationSource, RecordedSeed(tape)),
        policy=DeterministicIntentPolicy(),
        config=investigation_config_from_document(expected.replay_contract.config),
        recorded_terminal=expected.replay_contract.terminal,
    )
    tape.assert_consumed()
    expected_transitions = [
        (a.causal_decision_before, a.causal_decision_after) for a in expected.action_audits
    ]
    actual_transitions = [
        (a.causal_decision_before, a.causal_decision_after) for a in actual.action_audits
    ]
    final = diagnosis_epistemic_digest(expected.diagnosis) == diagnosis_epistemic_digest(
        actual.diagnosis
    )
    transitions = expected_transitions == actual_transitions
    assert final and transitions, f"causal replay divergence: {sid}"
    return {
        "scenario": sid,
        "final_digest_equal": final,
        "all_transitions_equal": transitions,
        "recorded_calls_consumed": tape.cursor,
        "live_source_access": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    output = args.directory / "offline-replay.json"
    rows = []
    files = sorted(args.directory.glob("*-active.json"))
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(replay, p) for p in files]):
            row = future.result()
            rows.append(row)
            output.write_text(json.dumps(sorted(rows, key=lambda r: r["scenario"]), indent=2))
            print(row["scenario"], "PASS", flush=True)


if __name__ == "__main__":
    main()
