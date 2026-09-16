#!/usr/bin/env python3
"""Provider-free E10 prediction/seal/grade-local command boundary.

The default prediction path requires explicit live environment values, but
this module never invokes a judge and never loads ground truth while
predicting.  Use ``grade-local`` only after a complete prediction seal.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.evals.itbench.e10_official import (
    E10_DEFAULT_PREDICTIONS_ROOT,
    E10_DEFAULT_SEAL,
    E10_OFFICIAL_RELEVANT_PATHS,
    load_e10_manifest,
    predict_e10,
    seal_e10_predictions,
)

ROOT = Path(__file__).resolve().parents[1]


def _path(value: str) -> Path:
    return ROOT / value if not Path(value).is_absolute() else Path(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prediction-first E10 official runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    predict = subparsers.add_parser("predict")
    predict.add_argument("--manifest", required=True)
    predict.add_argument("--ledger", required=True)
    predict.add_argument("--predictions-root", default=E10_DEFAULT_PREDICTIONS_ROOT)
    predict.add_argument("--seal", default=E10_DEFAULT_SEAL)

    seal = subparsers.add_parser("seal")
    seal.add_argument("--manifest", required=True)
    seal.add_argument("--ledger", required=True)
    seal.add_argument("--predictions-root", default=E10_DEFAULT_PREDICTIONS_ROOT)
    seal.add_argument("--seal", default=E10_DEFAULT_SEAL)

    grade = subparsers.add_parser("grade-local")
    grade.add_argument("--manifest", required=True)
    grade.add_argument("--predictions-root", default=E10_DEFAULT_PREDICTIONS_ROOT)
    grade.add_argument("--seal", default=E10_DEFAULT_SEAL)
    grade.add_argument("--result", required=True)

    args = parser.parse_args()
    if args.command == "predict":
        checkpoints = predict_e10(
            ROOT,
            manifest_path=_path(args.manifest),
            ledger_path=_path(args.ledger),
            predictions_root=_path(args.predictions_root),
            seal_path=_path(args.seal),
            relevant_paths=E10_OFFICIAL_RELEVANT_PATHS,
        )
        print(json.dumps({"status": "E10_PREDICTIONS_COMPLETE", "count": len(checkpoints)}))
        return 0
    if args.command == "seal":
        payload = seal_e10_predictions(
            ROOT,
            manifest_path=_path(args.manifest),
            predictions_root=_path(args.predictions_root),
            seal_path=_path(args.seal),
            ledger_path=_path(args.ledger),
        )
        print(
            json.dumps({"status": "E10_PREDICTIONS_SEALED", "count": payload["completion_count"]})
        )
        return 0
    from packages.evals.itbench.e10_local_grading import grade_local_e10

    manifest = load_e10_manifest(_path(args.manifest))
    payload = grade_local_e10(
        ROOT,
        manifest=manifest,
        manifest_path=_path(args.manifest),
        predictions_root=_path(args.predictions_root),
        seal_path=_path(args.seal),
        result_path=_path(args.result),
    )
    print(json.dumps({"status": "E10_LOCAL_GRADE_COMPLETE", "count": payload["scenario_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
