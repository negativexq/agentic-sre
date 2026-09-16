"""Build and evaluate clean, provider-free E11 retrieval ablations.

The script freezes all observable outputs before opening ground truth.  It is
an engineering qualification tool, not a live runner and never constructs a
model provider.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.e11_offline import (
    build_clean_retrieval_ablations,
    evaluate_retrieval_ablations,
    write_frozen_outputs,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".local/itbench-e11-retrieval-qualification-v2.json"),
    )
    parser.add_argument("--max-telemetry-records", type=int, default=100)
    args = parser.parse_args()

    dataset = ITBenchLiteDataset.open(args.dataset_root)
    ablations = build_clean_retrieval_ablations(
        dataset, max_telemetry_records=args.max_telemetry_records
    )
    frozen_hashes: dict[str, str] = {}
    serialized: dict[str, Any] = {}
    for label, output in ablations.items():
        path = args.output.with_name(f"{args.output.stem}-{label}.json")
        frozen_hashes[label] = write_frozen_outputs(path, output)
        serialized[label] = output
    evaluated = evaluate_retrieval_ablations(dataset, serialized)
    result = {
        "execution": "ITB-E11-OFFLINE-RETRIEVAL-V2",
        "provider_invocations": 0,
        "ground_truth_access_during_build": 0,
        "frozen_output_sha256": frozen_hashes,
        "ablations": evaluated,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
