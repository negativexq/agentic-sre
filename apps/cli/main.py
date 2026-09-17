"""Command-line entry point: diagnose incidents and run benchmarks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from packages.rca.model import Diagnosis

DEFAULT_DATASET = Path(os.environ.get("ITBENCH_LITE_ROOT", ".local/itbench-lite"))


def _print_diagnosis(diagnosis: Diagnosis) -> None:
    print(f"Incident     {diagnosis.incident_id}")
    print(f"Root cause   {diagnosis.root_cause or '-'}")
    print(f"Confidence   {diagnosis.confidence.value}")
    print(f"Summary      {diagnosis.summary}")
    symptoms = diagnosis.symptoms
    print(
        f"Symptoms     {', '.join(symptoms.alert_names) or '-'} on {', '.join(symptoms.services[:6]) or '-'}"
    )
    if diagnosis.evidence:
        print("Evidence")
        for finding in diagnosis.evidence:
            when = finding.at.isoformat(timespec="seconds") if finding.at else "-"
            print(f"  - [{finding.kind.value}] {when} {finding.summary}")
    if diagnosis.remediation:
        print("Proposed remediation (not executed)")
        for item in diagnosis.remediation:
            print(f"  - {item.action}\n      $ {item.command}\n      risk: {item.risk}")
    if diagnosis.alternatives:
        print("Alternatives")
        for candidate in diagnosis.alternatives:
            print(
                f"  - {candidate.entity} score {candidate.score:.1f}: {candidate.findings[0].summary}"
            )
    print("Steps")
    for step in diagnosis.steps:
        print(f"  - {step.actor}/{step.action}: {step.detail}")


def _dataset(root: Path):  # type: ignore[no-untyped-def]
    from packages.evals.itbench.dataset import ITBenchLiteDataset

    return ITBenchLiteDataset.open(root)


def cmd_diagnose(args: argparse.Namespace) -> int:
    from packages.evals.itbench.source import SnapshotSource
    from packages.rca.engine import diagnose

    dataset = _dataset(args.dataset)
    diagnosis = diagnose(SnapshotSource(dataset.scenario(args.scenario)))
    if args.json:
        print(json.dumps(diagnosis.model_dump(mode="json"), indent=2))
    else:
        _print_diagnosis(diagnosis)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from packages.evals.itbench.benchmark import grade, load_split, predict

    if args.split == "test" and not args.confirm_test:
        print(
            "The test split is reported once per frozen release. "
            "Re-run with --confirm-test from a clean, tagged commit.",
            file=sys.stderr,
        )
        return 2
    dataset = _dataset(args.dataset)
    ids = load_split(args.split)
    manifest = predict(dataset, ids, args.out, split=args.split, progress=print)
    if manifest["git_dirty"] and args.split == "test":
        print("warning: predictions were made from a dirty tree", file=sys.stderr)
    report = grade(dataset, args.out)
    print((args.out / "report.md").read_text(encoding="utf-8"))
    return 0 if report["scenarios"] else 1


def cmd_grade(args: argparse.Namespace) -> int:
    from packages.evals.itbench.benchmark import grade

    grade(_dataset(args.dataset), args.out)
    print((args.out / "report.md").read_text(encoding="utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-sre", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    diagnose_cmd = sub.add_parser("diagnose", help="diagnose one ITBench-Lite snapshot")
    diagnose_cmd.add_argument("scenario", help="scenario id, e.g. Scenario-4")
    diagnose_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    diagnose_cmd.add_argument("--json", action="store_true")
    diagnose_cmd.set_defaults(handler=cmd_diagnose)

    eval_cmd = sub.add_parser("eval", help="predict, seal, and grade a split")
    eval_cmd.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    eval_cmd.add_argument("--out", type=Path, required=True)
    eval_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    eval_cmd.add_argument("--confirm-test", action="store_true")
    eval_cmd.set_defaults(handler=cmd_eval)

    grade_cmd = sub.add_parser("grade", help="re-grade a sealed run")
    grade_cmd.add_argument("--out", type=Path, required=True)
    grade_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    grade_cmd.set_defaults(handler=cmd_grade)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
