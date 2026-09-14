#!/usr/bin/env python3
"""Fetch a reproducible, bounded local view of the official ITBench-Lite SRE set."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any

from packages.evals.itbench.dataset import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBENCH_SOURCE,
    ITBENCH_SRE_VERSION,
)

HF_API = "https://huggingface.co/api/datasets/ibm-research/ITBench-Lite/tree"
HF_RESOLVE = "https://huggingface.co/datasets/ibm-research/ITBench-Lite/resolve"


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def _files_for(path: str) -> list[dict[str, Any]]:
    items = _get_json(
        f"{HF_API}/{ITBENCH_DATASET_REVISION}/{path}?recursive=true&expand=false&limit=1000"
    )
    return [item for item in items if item["type"] == "file"]


def _download(remote_path: str, destination: Path, *, max_bytes: int | None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    url = f"{HF_RESOLVE}/{ITBENCH_DATASET_REVISION}/{remote_path}"
    request = urllib.request.Request(url)
    if max_bytes is not None:
        request.add_header("Range", f"bytes=0-{max_bytes - 1}")
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(max_bytes) if max_bytes is not None else response.read()
    destination.write_bytes(data)


def prepare(root: Path, *, sample_bytes: int, all_metrics: bool = False) -> dict[str, Any]:
    snapshot_root = root / "snapshots" / "sre" / ITBENCH_SRE_VERSION
    entries: list[dict[str, Any]] = []
    for scenario_id in ITBENCH_SCENARIO_IDS:
        scenario_remote = f"snapshots/sre/{ITBENCH_SRE_VERSION}/{scenario_id}"
        files = _files_for(scenario_remote)
        selected: list[dict[str, Any]] = []
        alert_candidates = [
            item
            for item in files
            if "/alerts/" in item["path"] or "/alerts_in_alerting_state_" in item["path"]
        ]
        alerting = [
            item for item in alert_candidates if "/alerts_in_alerting_state_" in item["path"]
        ]
        if alerting:
            selected.append(sorted(alerting, key=lambda item: item["path"])[-1])
        elif alert_candidates:
            # The first Prometheus snapshot is the stable alert-state sample;
            # later snapshots commonly represent the post-recovery state.
            selected.append(sorted(alert_candidates, key=lambda item: item["path"])[0])
        metric_files = sorted(
            (item for item in files if "/metrics/" in item["path"]),
            key=lambda item: item["path"],
        )
        # E0 needs a representative metric surface, not the multi-gigabyte
        # raw export.  ``--all-metrics`` is available for a complete local
        # snapshot when a future external run requires it.
        selected.extend(metric_files if all_metrics else metric_files[:2])
        selected.extend(
            item
            for item in files
            if item["path"].endswith(
                (
                    "ground_truth.yaml",
                    "k8s_events_raw.tsv",
                    "k8s_objects_raw.tsv",
                    "otel_logs_raw.tsv",
                    "otel_traces_raw.tsv",
                )
            )
        )
        for item in sorted(
            {item["path"]: item for item in selected}.values(), key=lambda value: value["path"]
        ):
            relative = item["path"].split(f"/{scenario_id}/", 1)[1]
            destination = snapshot_root / scenario_id / relative
            full = (
                relative == "ground_truth.yaml"
                or relative.startswith("alerts/")
                or relative.startswith("alerts_in_alerting_state_")
            )
            _download(item["path"], destination, max_bytes=None if full else sample_bytes)
            entries.append(
                {
                    "remote_path": item["path"],
                    "local_path": str(destination.relative_to(root)),
                    "size": item.get("size", 0),
                    "sample_bytes": None if full else sample_bytes,
                    "complete": full,
                }
            )
    manifest = {
        "benchmark": "ITBench-Lite",
        "organization": "IBM Research",
        "domain": "SRE",
        "source": ITBENCH_SOURCE,
        "revision": ITBENCH_DATASET_REVISION,
        "sre_version": ITBENCH_SRE_VERSION,
        "license": "Apache-2.0",
        "scenario_ids": list(ITBENCH_SCENARIO_IDS),
        "scenario_count": len(ITBENCH_SCENARIO_IDS),
        "acquisition": "bounded evidence prefixes; ground truth and selected alerts complete",
        "sample_bytes": sample_bytes,
        "files": entries,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / ".itbench-lite-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--sample-bytes", type=int, default=65_536)
    parser.add_argument("--all-metrics", action="store_true")
    args = parser.parse_args()
    if args.sample_bytes < 4096:
        parser.error("--sample-bytes must be at least 4096")
    manifest = prepare(args.root, sample_bytes=args.sample_bytes, all_metrics=args.all_metrics)
    print(
        json.dumps(
            {"status": "PASS", "scenario_count": manifest["scenario_count"], "root": str(args.root)}
        )
    )


if __name__ == "__main__":
    main()
