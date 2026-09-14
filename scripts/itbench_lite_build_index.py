#!/usr/bin/env python3
"""Build optional disk-conscious ITBench-Lite metadata indexes."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

from packages.evals.itbench import ITBenchLiteDataset, atomic_json_write
from packages.evals.itbench.sparse_index import build_trace_indexes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="optional path for the deterministic sidecar manifest",
    )
    args = parser.parse_args()
    dataset = ITBenchLiteDataset.open(args.root)
    result = build_trace_indexes(dataset)
    for scenario in result["scenarios"]:
        digest, _ = _digest(Path(scenario["index_path"]))
        scenario["index_sha256"] = digest
    report = args.report or args.root / ".itbench-sparse-index.json"
    atomic_json_write(report, result)
    print(json.dumps(result, sort_keys=True))


def _digest(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


if __name__ == "__main__":
    main()
