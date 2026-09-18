#!/usr/bin/env python3
"""Print the deterministic full/seed/fixed-point active/search control matrix."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.rca.investigation.control_matrix import (
    ControlMatrixResult,
    aggregate_classifications,
    run_control_matrix,
)

SCENARIOS = (
    "Scenario-14",
    "Scenario-19",
    "Scenario-25",
    "Scenario-35",
    "Scenario-80",
    "Scenario-81",
    "Scenario-83",
)


def _attempt_counts(result: ControlMatrixResult) -> dict[str, int]:
    audits = result.promotion_audits
    return dict(sorted(Counter(item.attempt_outcome for item in audits).items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--max-states", type=int, default=256)
    parser.add_argument("--max-rounds", type=int, default=16)
    parser.add_argument("--max-observations", type=int, default=4096)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    dataset = ITBenchLiteDataset.open(args.root)
    results = tuple(
        run_control_matrix(
            SnapshotSource(dataset.scenario(name)),
            max_depth=3,
            max_states=args.max_states,
            max_rounds=args.max_rounds,
            max_observations=args.max_observations,
        )
        for name in (tuple(args.scenarios) if args.scenarios else SCENARIOS)
    )
    if args.json:
        print(json.dumps([result.as_dict() for result in results], indent=2, sort_keys=True))
        return 0

    print("Scenario | Full | Seed | Exhaustive Active | D1 | D2 | D3 | Investigation")
    print("---|---|---|---|---|---|---|---")
    for result in results:
        search = result.search
        print(
            f"{result.incident_id} | {result.full_diagnosis.resolution.value} | "
            f"{result.seed_diagnosis.resolution.value} | "
            f"{result.exhaustive_diagnosis.resolution.value} | "
            f"{search.depth_status(1)} | {search.depth_status(2)} | "
            f"{search.depth_status(3)} | {result.exhaustive_diagnosis.investigation_status.value}"
        )
        print(
            "  semantic full/shared/active/full-only/active-only="
            f"{len(result.full_semantic_findings)}/{len(result.shared_semantic_findings)}/"
            f"{len(result.active_semantic_findings)}/{len(result.full_only_semantic_findings)}/"
            f"{len(result.active_only_semantic_findings)}"
        )
        print(
            "  raw full/shared/active-accessible/missing="
            f"{len(result.full_refs)}/{len(result.shared_refs)}/"
            f"{len(result.active_accessible_refs)}/{len(result.missing_full_refs)}"
        )
        print(f"  query outcomes={_attempt_counts(result)}")
        print(
            "  unique returned/new/finding/no-finding="
            f"{len(result.exhaustive.unique_returned_refs)}/"
            f"{len(result.exhaustive.unique_new_refs)}/"
            f"{len(result.exhaustive.unique_refs_that_produced_findings)}/"
            f"{len(result.exhaustive.unique_new_refs_with_no_finding)}"
        )
        frontier = result.frontier_state
        print(
            "  frontier raw/shapes/targets/unexplored/partial/queried-no-finding/promoted="
            f"{frontier.raw_alternatives}/{len(frontier.structural_shape_groups)}/"
            f"{frontier.observation_targets}/{frontier.unexplored}/{frontier.partial}/"
            f"{frontier.queried_no_finding}/{frontier.promoted}"
        )
        print(
            f"  closure rounds/observations/truncated/reason="
            f"{result.exhaustive.rounds}/{len(result.exhaustive.attempted_observations)}/"
            f"{result.exhaustive.truncated}/{result.exhaustive.termination_reason}"
        )
        print(
            f"  bounded-search-truncated={result.search.truncated}; "
            f"depth={result.search.truncation_depth}"
        )
        print(
            f"  conclusions fidelity={result.active_fidelity}; "
            f"full={result.full_control_resolvability}; "
            f"planning={result.planning_opportunity}; "
            f"reason={result.planning_reason}"
        )
        print(
            f"  full-only reasons={dict(sorted(Counter(result.full_only_reasons.values()).items()))}"
        )
    print(f"Promotion classifications: {aggregate_classifications(results)}")
    print(
        "Ceilings: "
        f"D1 resolved={sum(item.search.depth_status(1) == 'RESOLVED' for item in results)}/{len(results)} "
        f"D2 resolved={sum(item.search.depth_status(2) == 'RESOLVED' for item in results)}/{len(results)} "
        f"D3 resolved={sum(item.search.depth_status(3) == 'RESOLVED' for item in results)}/{len(results)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
