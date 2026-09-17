"""Command-line entry point: diagnose incidents and run benchmarks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.rca.engine import Investigator
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
    if diagnosis.causal_path:
        print("Causal path")
        for hop in diagnosis.causal_path:
            print(f"  {hop.source} --{hop.relation}--> {hop.target}")
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


def _dataset(root: Path) -> ITBenchLiteDataset:
    return ITBenchLiteDataset.open(root)


def _investigator(args: argparse.Namespace) -> Investigator | None:
    """Build the LLM investigator only when asked; the client still requires opt-in env."""
    if not getattr(args, "llm", False):
        return None
    from packages.rca.agent import LLMInvestigator
    from packages.rca.llm import OpenAIClient

    client = OpenAIClient(model=args.model)
    problem = client.readiness_problem()
    if problem:
        raise SystemExit(f"--llm: {problem}")
    return LLMInvestigator(client)


def cmd_diagnose(args: argparse.Namespace) -> int:
    from packages.evals.itbench.source import SnapshotSource
    from packages.rca.engine import diagnose

    dataset = _dataset(args.dataset)
    diagnosis = diagnose(
        SnapshotSource(dataset.scenario(args.scenario)), investigator=_investigator(args)
    )
    _emit(diagnosis, args)
    return 0


def _emit(diagnosis: Diagnosis, args: argparse.Namespace) -> None:
    if args.html:
        from packages.rca.report import diagnosis_html

        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(diagnosis_html(diagnosis), encoding="utf-8")
    if args.json:
        print(json.dumps(diagnosis.model_dump(mode="json"), indent=2))
    else:
        _print_diagnosis(diagnosis)
        if args.html:
            print(f"\nHTML report: {args.html}")


def cmd_demo(args: argparse.Namespace) -> int:
    from packages.rca.demo import demo_source
    from packages.rca.engine import diagnose

    _emit(diagnose(demo_source(), investigator=_investigator(args)), args)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("apps.control_plane.main:app", host=args.host, port=args.port)
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
    manifest = predict(
        dataset,
        ids,
        args.out,
        split=args.split,
        investigator=_investigator(args),
        progress=print,
    )
    if manifest["git_dirty"] and args.split == "test":
        print("warning: predictions were made from a dirty tree", file=sys.stderr)
    if manifest["investigator_errors"]:
        print(
            f"warning: the investigator failed in {manifest['investigator_errors']} scenario(s); "
            "those answers are the engine's",
            file=sys.stderr,
        )
    report = grade(dataset, args.out)
    print((args.out / "report.md").read_text(encoding="utf-8"))
    return 0 if report["scenarios"] else 1


def cmd_grade(args: argparse.Namespace) -> int:
    from packages.evals.itbench.benchmark import grade

    grade(_dataset(args.dataset), args.out)
    print((args.out / "report.md").read_text(encoding="utf-8"))
    return 0


def cmd_benchmark_qualify(args: argparse.Namespace) -> int:
    from packages.evals.itbench.benchmark import load_split
    from packages.evals.itbench.qualification import qualify_dataset

    dataset = _dataset(args.dataset)
    report = qualify_dataset(dataset, load_split(args.split), args.out)
    print((args.out / "qualification.md").read_text(encoding="utf-8"))
    return 0 if report["scenarios"] else 1


def _add_output_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the diagnosis as JSON")
    parser.add_argument("--html", type=Path, default=None, help="also write an HTML report")


def _add_llm_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm",
        action="store_true",
        help="let the LLM investigator review the ranking "
        "(needs SRE_LLM_ENABLED=true, SRE_LLM_MAX_CALLS, OPENAI_API_KEY)",
    )
    parser.add_argument("--model", default=None, help="override SRE_LLM_MODEL")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-sre", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    diagnose_cmd = sub.add_parser("diagnose", help="diagnose one ITBench-Lite snapshot")
    diagnose_cmd.add_argument("scenario", help="scenario id, e.g. Scenario-4")
    diagnose_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    _add_output_flags(diagnose_cmd)
    _add_llm_flags(diagnose_cmd)

    demo_cmd = sub.add_parser("demo", help="diagnose the built-in bad-rollout incident offline")
    _add_output_flags(demo_cmd)
    _add_llm_flags(demo_cmd)
    demo_cmd.set_defaults(handler=cmd_demo)

    serve_cmd = sub.add_parser("serve", help="run the control plane API and web UI")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8000)
    serve_cmd.set_defaults(handler=cmd_serve)
    diagnose_cmd.set_defaults(handler=cmd_diagnose)

    eval_cmd = sub.add_parser("eval", help="predict, seal, and grade a split")
    eval_cmd.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    eval_cmd.add_argument("--out", type=Path, required=True)
    eval_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    eval_cmd.add_argument("--confirm-test", action="store_true")
    _add_llm_flags(eval_cmd)
    eval_cmd.set_defaults(handler=cmd_eval)

    grade_cmd = sub.add_parser("grade", help="re-grade a sealed run")
    grade_cmd.add_argument("--out", type=Path, required=True)
    grade_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    grade_cmd.set_defaults(handler=cmd_grade)

    qualify_cmd = sub.add_parser(
        "benchmark-qualify",
        help="diagnose-only qualification of ground-truth entity contracts",
    )
    qualify_cmd.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    qualify_cmd.add_argument("--out", type=Path, required=True)
    qualify_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    qualify_cmd.set_defaults(handler=cmd_benchmark_qualify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
