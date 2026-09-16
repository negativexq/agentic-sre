#!/usr/bin/env python3
"""Offline E11 preparation boundary.

This command intentionally exposes only provider-free qualification operations
in the development tree.  A separately authorized live adapter must inject a
provider after the typed preflight; this script never enables one implicitly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.evals.itbench.e11_canary import (
    run_e11_fake_provider_canary,
    run_e11_snapshot_runtime_canary,
)
from packages.evals.itbench.e11_official import (
    E11_OFFICIAL_RELEVANT_PATHS,
    build_e11_manifest,
    load_e11_manifest,
    seal_e11_predictions,
    validate_e11_preflight,
    verify_e11_seal,
)

ROOT = Path(__file__).resolve().parents[1]


def _path(value: str) -> Path:
    return ROOT / value if not Path(value).is_absolute() else Path(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Provider-free E11 preparation boundary")
    sub = parser.add_subparsers(dest="command", required=True)
    canary = sub.add_parser("canary")
    canary.add_argument("--count", type=int, default=35)
    runtime_canary = sub.add_parser("runtime-canary")
    runtime_canary.add_argument("--dataset-root", default=".local/itbench-lite")
    manifest = sub.add_parser("manifest-template")
    manifest.add_argument("--output", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--manifest", required=True)
    preflight.add_argument("--ledger", required=True)
    seal = sub.add_parser("seal")
    seal.add_argument("--manifest", required=True)
    seal.add_argument("--predictions-root", required=True)
    seal.add_argument("--seal", required=True)
    verify = sub.add_parser("verify-seal")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--predictions-root", required=True)
    verify.add_argument("--seal", required=True)
    args = parser.parse_args()
    if args.command == "canary":
        result = run_e11_fake_provider_canary(
            tuple(f"Scenario-{i}" for i in range(1, args.count + 1))
        )
    elif args.command == "runtime-canary":
        result = run_e11_snapshot_runtime_canary(_path(args.dataset_root))
    elif args.command == "manifest-template":
        result = build_e11_manifest(ROOT).model_dump(mode="json")
        _path(args.output).write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "preflight":
        typed = load_e11_manifest(_path(args.manifest))
        result = validate_e11_preflight(
            ROOT,
            typed,
            relevant_paths=E11_OFFICIAL_RELEVANT_PATHS,
            ledger=json.loads(_path(args.ledger).read_text(encoding="utf-8")),
        )
    elif args.command == "seal":
        typed = load_e11_manifest(_path(args.manifest))
        result = seal_e11_predictions(typed, _path(args.predictions_root), _path(args.seal))
    else:
        typed = load_e11_manifest(_path(args.manifest))
        result = verify_e11_seal(typed, _path(args.predictions_root), _path(args.seal))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
