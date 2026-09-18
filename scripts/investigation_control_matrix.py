#!/usr/bin/env python3
"""Print the deterministic full/seed/active/search control matrix."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.rca.investigation.control_matrix import (
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--max-states", type=int, default=256)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    dataset = ITBenchLiteDataset.open(args.root)
    results = tuple(
        run_control_matrix(
            SnapshotSource(dataset.scenario(name)),
            max_depth=3,
            max_states=args.max_states,
        )
        for name in (tuple(args.scenarios) if args.scenarios else SCENARIOS)
    )
    if args.json:
        print(json.dumps([result.as_dict() for result in results], indent=2, sort_keys=True))
        return 0
    print("Scenario | Full | Seed | Exhaustive Active | Depth-3 | Main blocker")
    print("---|---|---|---|---|---")
    for result in results:
        search = result.search
        if result.full_diagnosis.resolution.value == "RESOLVED":
            blocker = "none/full control resolved"
        elif result.exhaustive_diagnosis.resolution.value == "RESOLVED":
            blocker = (
                "frontier completeness"
                if result.exhaustive_diagnosis.investigation_status.value == "OPEN"
                else "none"
            )
        elif result.full_only_findings:
            blocker = "telemetry/query coverage"
        elif any(item.classification == "NOVEL_RAW_NO_FINDING" for item in result.promotion_audits):
            blocker = "normalization/promotion"
        else:
            blocker = "causal hypothesis semantics"
        print(
            f"{result.incident_id} | {result.full_diagnosis.resolution.value} | "
            f"{result.seed_diagnosis.resolution.value} | "
            f"{result.exhaustive_diagnosis.resolution.value} | "
            f"{any(len(path.steps) == 3 for path in search.solutions)} | {blocker}"
        )
        material = result.frontier_materiality
        print(
            f"  findings full/active={len(result.full_case.findings)}/"
            f"{len(result.exhaustive_case.findings)}; missing-active={len(result.full_only_findings)}; "
            f"frontier raw/material/targets={material.raw_alternatives}/"
            f"{material.material_alternatives}/{material.observation_targets}"
        )
        print(
            f"  lifecycle={material.lifecycle_counts}; promotion="
            f"{dict(sorted({key: sum(a.classification == key for a in result.promotion_audits) for key in {a.classification for a in result.promotion_audits}}.items()))}"
        )
        print(
            f"  full-only reasons={dict(sorted(Counter(result.full_only_reasons.values()).items()))}"
        )
    print(f"Promotion classifications: {aggregate_classifications(results)}")
    print(
        "Ceilings: "
        f"1-step={sum(any(len(path.steps) == 1 for path in item.search.solutions) for item in results)}/{len(results)} "
        f"2-step={sum(any(len(path.steps) == 2 for path in item.search.solutions) for item in results)}/{len(results)} "
        f"3-step={sum(any(len(path.steps) == 3 for path in item.search.solutions) for item in results)}/{len(results)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
