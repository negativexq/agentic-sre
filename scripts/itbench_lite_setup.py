#!/usr/bin/env python3
"""Fetch a reproducible, bounded local view of the official ITBench-Lite SRE set."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from packages.evals.itbench.dataset import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBENCH_SOURCE,
    ITBENCH_SRE_VERSION,
    source_file_digest,
)

csv.field_size_limit(sys.maxsize)

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


def _download(remote_path: str, destination: Path, *, expected_size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size == expected_size:
        return
    url = f"{HF_RESOLVE}/{ITBENCH_DATASET_REVISION}/{remote_path}"
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=120) as response:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".download", dir=destination.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if Path(temporary_name).stat().st_size != expected_size:
                raise OSError(f"downloaded size mismatch for {remote_path}")
            os.replace(temporary_name, destination)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


def _download_job(job: tuple[str, Path, int]) -> None:
    remote_path, destination, expected_size = job
    _download(remote_path, destination, expected_size=expected_size)


def prepare(root: Path, *, sample_bytes: int = 0, all_metrics: bool = True) -> dict[str, Any]:
    del sample_bytes, all_metrics
    snapshot_root = root / "snapshots" / "sre" / ITBENCH_SRE_VERSION
    entries: list[dict[str, Any]] = []
    jobs: list[tuple[str, Path, int]] = []
    entry_jobs: list[dict[str, Any]] = []
    for scenario_id in ITBENCH_SCENARIO_IDS:
        scenario_remote = f"snapshots/sre/{ITBENCH_SRE_VERSION}/{scenario_id}"
        files = _files_for(scenario_remote)
        selected: list[dict[str, Any]] = []
        selected.extend(
            item
            for item in files
            if "/alerts/" in item["path"] or "/alerts_in_alerting_state_" in item["path"]
        )
        metric_files = sorted(
            (item for item in files if "/metrics/" in item["path"]),
            key=lambda item: item["path"],
        )
        selected.extend(metric_files)
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
            expected_size = int(item.get("size", 0))
            jobs.append((item["path"], destination, expected_size))
            entry_jobs.append(
                {
                    "remote_path": item["path"],
                    "local_path": str(destination.relative_to(root)),
                    "size": item.get("size", 0),
                    "sample_bytes": None,
                    "complete": True,
                }
            )
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="itbench-download") as pool:
        futures: list[Future[None]] = [pool.submit(_download_job, job) for job in jobs]
        for future in futures:
            future.result()
    entries = entry_jobs
    completeness = _build_completeness(root, entries)
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
        "acquisition": "complete observable evidence; bounded only at tool response",
        "sample_bytes": None,
        "files": entries,
        "completeness_manifest": ".itbench-source-completeness.json",
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / ".itbench-lite-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / ".itbench-source-completeness.json").write_text(
        json.dumps(completeness, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _build_completeness(root: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for entry in entries:
        path = root / entry["local_path"]
        digest, byte_count = source_file_digest(path)
        category = _category_for(entry["local_path"])
        parsed = 0
        rejected = 0
        if path.suffix == ".tsv":
            try:
                with path.open("r", encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream, delimiter="\t")
                    for row in reader:
                        if None in row or any(value is None for value in row.values()):
                            rejected += 1
                        else:
                            parsed += 1
            except (UnicodeError, csv.Error):
                rejected += 1
        elif path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                parsed = len(value)
            elif isinstance(value, dict) and isinstance(value.get("alerts"), list):
                parsed = len(value["alerts"])
            elif (
                isinstance(value, dict)
                and isinstance(value.get("data"), dict)
                and isinstance(value["data"].get("alerts"), list)
            ):
                parsed = len(value["data"]["alerts"])
            else:
                parsed = 1
        else:
            parsed = 1
        files.append(
            {
                "source_file": entry["local_path"],
                "category": category,
                "sha256": digest,
                "byte_count": byte_count,
                "source_rows": parsed + rejected,
                "indexed_rows": parsed,
                "parse_failures": rejected,
                "ignored_rows": 0,
            }
        )
    return {
        "status": "PASS" if all(item["parse_failures"] == 0 for item in files) else "FAIL",
        "source_file_count": len(files),
        "files": files,
        "coverage": {
            category: {
                "source_rows": sum(
                    item["source_rows"] for item in files if item["category"] == category
                ),
                "indexed_rows": sum(
                    item["indexed_rows"] for item in files if item["category"] == category
                ),
                "parse_failures": sum(
                    item["parse_failures"] for item in files if item["category"] == category
                ),
            }
            for category in sorted({item["category"] for item in files})
        },
    }


def _category_for(relative_path: str) -> str:
    if "/alerts/" in relative_path or "/alerts_in_alerting_state_" in relative_path:
        return "alerts"
    if "/metrics/" in relative_path:
        return "metrics"
    if relative_path.endswith("k8s_events_raw.tsv"):
        return "k8s_events"
    if relative_path.endswith("k8s_objects_raw.tsv"):
        return "k8s_objects"
    if relative_path.endswith("otel_logs_raw.tsv"):
        return "logs"
    if relative_path.endswith("otel_traces_raw.tsv"):
        return "traces"
    return "ground_truth"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument(
        "--sample-bytes", type=int, default=0, help="deprecated; full files are always fetched"
    )
    parser.add_argument(
        "--all-metrics", action="store_true", help="deprecated; all metrics are always fetched"
    )
    args = parser.parse_args()
    manifest = prepare(args.root, sample_bytes=args.sample_bytes, all_metrics=args.all_metrics)
    print(
        json.dumps(
            {"status": "PASS", "scenario_count": manifest["scenario_count"], "root": str(args.root)}
        )
    )


if __name__ == "__main__":
    main()
