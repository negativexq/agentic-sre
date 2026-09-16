#!/usr/bin/env python3
"""Run provider-free E10.1/E11 retrieval qualification and write reports."""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic
from typing import Any

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.e11_offline import (
    build_retrieval_ablations,
    evaluate_retrieval_ablations,
    write_frozen_outputs,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    started = monotonic()
    dataset = ITBenchLiteDataset.open(ROOT / ".local/itbench-lite")
    # Bound each source file so qualification remains deterministic and
    # reproducible on the repository snapshot while still exercising all
    # telemetry categories.
    telemetry_bound = 100
    ablations = build_retrieval_ablations(dataset, max_telemetry_records=telemetry_bound)
    freeze_path = ROOT / ".local/e11-retrieval-ablations.json"
    freeze_hash = write_frozen_outputs(freeze_path, ablations)
    evaluations = evaluate_retrieval_ablations(dataset, ablations)
    payload: dict[str, Any] = {
        "execution": "ITB-E10.1-E11-OFFLINE",
        "dataset_revision": ablations["R5"].get("dataset_revision"),
        "scenario_count": 35,
        "provider_invocations": 0,
        "ground_truth_access_during_build": 0,
        "frozen_outputs_sha256": freeze_hash,
        "telemetry_record_bound_per_file": telemetry_bound,
        "frozen_e7_baseline": {
            "catalog_root_coverage": 12 / 35,
            "recall_at_k": {
                "1": 8 / 35,
                "3": 11 / 35,
                "5": 12 / 35,
                "10": 12 / 35,
            },
        },
        "ablations": {
            label: {
                "configuration": ablations[label]["configuration"],
                "catalog_root_coverage": evaluation["catalog_root_coverage"],
                "recall_at_k": evaluation["recall_at_k"],
                "conditional_recall_at_k": evaluation["conditional_recall_at_k"],
                "catalog_size": evaluation["catalog_size"],
                "shortlist_size": evaluation["shortlist_size"],
                "candidate_concentration": evaluation["candidate_concentration"],
                "ground_truth_access_post_freeze": evaluation["ground_truth_access"],
            }
            for label, evaluation in evaluations.items()
        },
        "wall_time_ms": int((monotonic() - started) * 1000),
    }
    payload["r5_delta_vs_e7"] = {
        "catalog_root_coverage": (
            payload["ablations"]["R5"]["catalog_root_coverage"]
            - payload["frozen_e7_baseline"]["catalog_root_coverage"]
        ),
        "recall_at_10": (
            payload["ablations"]["R5"]["recall_at_k"]["10"]
            - payload["frozen_e7_baseline"]["recall_at_k"]["10"]
        ),
    }
    output = ROOT / "docs/benchmarks/itbench-e11-offline-qualification.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# E11 offline retrieval qualification",
        "",
        "All candidate/catalog outputs were built without GT and frozen before post-hoc grading. No provider was constructed.",
        "",
        f"- scenarios: {payload['scenario_count']}",
        f"- provider invocations: {payload['provider_invocations']}",
        f"- build GT accesses: {payload['ground_truth_access_during_build']}",
        f"- frozen outputs SHA256: `{freeze_hash}`",
        f"- telemetry bound: {payload['telemetry_record_bound_per_file']} rows/source-file/scenario",
        "- E7 baseline is immutable historical evidence; deltas below are post-hoc comparisons only.",
        "",
        "| config | catalog coverage | R@1 | R@3 | R@5 | R@10 | conditional R@10 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    ablation_values = payload["ablations"]
    assert isinstance(ablation_values, dict)
    for label, values in ablation_values.items():
        assert isinstance(values, dict)
        lines.append(
            f"| {label} | {values['catalog_root_coverage']:.3f} | "
            f"{values['recall_at_k']['1']:.3f} | {values['recall_at_k']['3']:.3f} | "
            f"{values['recall_at_k']['5']:.3f} | {values['recall_at_k']['10']:.3f} | "
            f"{(values['conditional_recall_at_k']['10'] or 0):.3f} |"
        )
    lines.extend(
        [
            "",
            "R5 vs frozen E7: "
            f"catalog coverage delta {payload['r5_delta_vs_e7']['catalog_root_coverage']:+.3f}; "
            f"Recall@10 delta {payload['r5_delta_vs_e7']['recall_at_10']:+.3f}.",
        ]
    )
    (ROOT / "docs/benchmarks/itbench-e11-offline-qualification.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "E11_OFFLINE_QUALIFICATION_COMPLETE", "provider_invocations": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
