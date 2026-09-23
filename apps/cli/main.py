"""Command-line entry point: diagnose incidents and run benchmarks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.rca.engine import Investigator
from packages.rca.hypotheses import summarize_diagnoses
from packages.rca.investigation import (
    InvestigationConfig,
    LLMInvestigationPolicy,
    ScriptedInvestigationPolicy,
    investigate_diagnosis,
)
from packages.rca.investigation.state import InvestigationPolicy
from packages.rca.model import Diagnosis, InvestigationAction, InvestigationResult
from packages.rca.resolution import summarize_resolution_audit, summarize_resolutions

DEFAULT_DATASET = Path(os.environ.get("ITBENCH_LITE_ROOT", ".local/itbench-lite"))


def _print_diagnosis(diagnosis: Diagnosis) -> None:
    print(f"Incident     {diagnosis.incident_id}")
    print(f"Root cause   {diagnosis.root_cause or '-'}")
    print(f"Confidence   {diagnosis.confidence.value}")
    print(f"Resolution   {diagnosis.resolution.value}")
    print(f"Summary      {diagnosis.summary}")
    symptoms = diagnosis.symptoms
    print(
        f"Symptoms     {', '.join(symptoms.alert_names) or '-'} on {', '.join(symptoms.services[:6]) or '-'}"
    )
    if diagnosis.hypothesis:
        hypothesis = diagnosis.hypothesis
        print("Causal hypothesis")
        print(f"  Actor          {hypothesis.causal_actor}")
        if hypothesis.manifestations:
            print("  Manifestations")
            for entity in hypothesis.manifestations[:6]:
                print(f"    - {entity}")
        for title, findings in (
            ("Initiating evidence", hypothesis.initiating_findings),
            ("Supporting evidence", hypothesis.supporting_findings),
            ("Contradictory evidence", hypothesis.contradictory_findings),
        ):
            if findings:
                print(f"  {title}")
                for finding in findings[:6]:
                    print(f"    - [{finding.kind.value}] {finding.summary}")
    if diagnosis.resolution.value == "AMBIGUOUS":
        print("Leading hypotheses")
        for hypothesis in diagnosis.ambiguous_hypotheses:
            print(f"  - {hypothesis.causal_actor} ({hypothesis.hypothesis_id})")
        if diagnosis.resolution_trace:
            print(f"  {diagnosis.resolution_trace.rationale}")
    if diagnosis.information_gaps:
        print("Information gaps")
        for gap in diagnosis.information_gaps[:8]:
            tools = ", ".join(gap.candidate_tools) or "none"
            print(
                f"  - {gap.dimension.value}: {gap.missing_fact} "
                f"[{gap.resolvability.value}; capabilities: {tools}]"
            )
    if diagnosis.causal_path:
        print("Causal path")
        for hop in diagnosis.causal_path:
            print(f"  {hop.source} --{hop.relation}--> {hop.target}")
    elif diagnosis.causal_explanation == "DIRECT":
        print("Causal explanation  direct evidence on the symptom entity")
    if diagnosis.evidence:
        print("Evidence")
        for finding in diagnosis.evidence:
            when = finding.at.isoformat(timespec="seconds") if finding.at else "-"
            print(f"  - [{finding.kind.value}] {when} {finding.summary}")
    if diagnosis.remediation:
        title = (
            "Possible remediation (hypothesis-specific; not executed)"
            if diagnosis.resolution.value == "AMBIGUOUS"
            else "Proposed remediation (not executed)"
        )
        print(title)
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


def cmd_investigate(args: argparse.Namespace) -> int:
    from packages.evals.itbench.source import SnapshotSource

    dataset = _dataset(args.dataset)
    source = SnapshotSource(dataset.scenario(args.scenario))
    if args.llm and args.actions:
        raise SystemExit("--llm and --actions cannot be used together")
    if args.llm:
        if not args.authorize_live_model:
            raise SystemExit("--llm requires --authorize-live-model")
        from packages.rca.llm import OpenAIClient

        client = OpenAIClient(model=args.model)
        problem = client.readiness_problem()
        if problem:
            raise SystemExit(f"--llm: {problem}")
        policy: InvestigationPolicy = LLMInvestigationPolicy(client)
    else:
        actions: list[InvestigationAction | dict[str, object]] = []
        if args.actions:
            raw = json.loads(args.actions.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise SystemExit("--actions must contain a JSON array")
            actions = [item for item in raw if isinstance(item, dict)]
        policy = ScriptedInvestigationPolicy(actions)
    result = investigate_diagnosis(
        source,
        policy=policy,
        config=InvestigationConfig(
            max_turns=args.max_turns,
            max_model_calls=args.max_model_calls,
            max_tool_calls=args.max_tool_calls,
            max_no_progress_rounds=args.max_no_progress_rounds,
        ),
    )
    _emit_investigation(result, args)
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


def _emit_investigation(result: InvestigationResult, args: argparse.Namespace) -> None:
    if args.html:
        from packages.rca.report import diagnosis_html

        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(
            diagnosis_html(result.diagnosis, investigation=result), encoding="utf-8"
        )
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        _print_diagnosis(result.diagnosis)
        print("Investigation")
        print(f"  Initial resolution   {result.initial_resolution.value}")
        print(f"  Final resolution     {result.final_resolution.value}")
        print(f"  Turns                {result.turns}")
        print(f"  Model calls          {result.model_calls}")
        print(f"  Tool calls           {result.tool_calls}")
        print(f"  Unique observations  {result.unique_observations}")
        print(f"  New evidence         {result.unique_evidence_added}")
        print(f"  Stop reason          {result.stop_reason.value}")
        for entry in result.ledger:
            print(
                "  Query                "
                f"{entry.capability}({entry.target.canonical}) "
                f"returned={len(entry.returned_evidence_refs)} "
                f"new={len(entry.new_evidence_refs)} "
                f"known={len(entry.already_known_refs)} "
                f"findings={len(entry.normalized_finding_ids)}"
            )
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


def cmd_investigation_eval(args: argparse.Namespace) -> int:
    from packages.evals.itbench.benchmark import load_split
    from packages.evals.itbench.investigation_benchmark import predict_investigations
    from packages.rca.investigation.state import InvestigationConfig

    if args.split == "test" and not args.confirm_test:
        print(
            "The test split is frozen; use --confirm-test to write a prediction artifact.",
            file=sys.stderr,
        )
        return 2
    dataset = _dataset(args.dataset)
    manifest = predict_investigations(
        dataset,
        load_split(args.split),
        args.out,
        split=args.split,
        config=InvestigationConfig(
            max_turns=args.max_turns,
            max_model_calls=args.max_model_calls,
            max_tool_calls=args.max_tool_calls,
        ),
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def cmd_grade_investigation_eval(args: argparse.Namespace) -> int:
    from packages.evals.itbench.investigation_benchmark import grade_investigations

    result = grade_investigations(_dataset(args.dataset), args.out)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


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


def cmd_hypothesis_report(args: argparse.Namespace) -> int:
    """Summarize grouping diagnostics stored in a prediction/run directory."""
    prediction_dir = args.run / "predictions"
    paths = sorted(prediction_dir.glob("*.json"))
    if not paths:
        raise SystemExit(f"no prediction files found under {prediction_dir}")
    diagnoses = [
        Diagnosis.model_validate(json.loads(path.read_text(encoding="utf-8"))["diagnosis"])
        for path in paths
    ]
    grouping = summarize_diagnoses(diagnoses)
    resolution = summarize_resolutions(diagnoses)
    report = {
        "run": str(args.run),
        "scenario_count": len(diagnoses),
        "grouping": grouping,
        "resolution": resolution,
        "selection_changes": "not inferable from one run",
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Hypothesis grouping report: {args.run}")
        for key, value in grouping.items():
            print(f"{key}: {value}")
        for resolution_key, resolution_value in resolution.items():
            print(f"resolution_{resolution_key}: {resolution_value}")
        print("selection_changes: not inferable from one run")
    return 0


def cmd_resolution_audit(args: argparse.Namespace) -> int:
    """Write bounded resolution near-collision diagnostics for a stored run."""
    prediction_dir = args.run / "predictions"
    paths = sorted(prediction_dir.glob("*.json"))
    if not paths:
        raise SystemExit(f"no prediction files found under {prediction_dir}")
    diagnoses = [
        Diagnosis.model_validate(json.loads(path.read_text(encoding="utf-8"))["diagnosis"])
        for path in paths
    ]
    report = summarize_resolution_audit(diagnoses)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "# Resolution audit",
        "",
        f"Run: `{args.run}`",
        f"Diagnoses audited: {report['diagnoses_audited']}",
        f"Structural near-collisions: {report['structural_near_collisions']}",
        "",
        "## Classifications",
        "",
    ]
    classifications = report["classifications"]
    assert isinstance(classifications, dict)
    lines.extend(f"- {key}: {value}" for key, value in sorted(classifications.items()))
    lines += ["", "## Cases", ""]
    records = report["records"]
    assert isinstance(records, list)
    for record in records:
        selected = record["selected"]
        alternative = record["alternative"]
        assert isinstance(selected, dict) and isinstance(alternative, dict)
        lines.extend(
            [
                f"### {record['incident_id']} — {record['classification']}",
                "",
                f"- selected: `{selected['hypothesis_id']}`",
                f"- alternative: `{alternative['hypothesis_id']}`",
                f"- resolution leader: `{record['resolution_leader']['hypothesis_id']}`"
                if isinstance(record.get("resolution_leader"), dict)
                else "- resolution leader: `none`",
                f"- resolution: `{record['resolution']}`",
                f"- reason: {record['resolution_reason']}",
                f"- selected plausible: `{selected['plausible']}`",
                f"- alternative plausible: `{alternative['plausible']}`",
                f"- selected onset: `{selected['onset_relation']}`",
                f"- alternative onset: `{alternative['onset_relation']}`",
                "",
            ]
        )
    (args.out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print((args.out / "report.md").read_text(encoding="utf-8"))
    return 0


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


def _add_investigation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actions", type=Path, help="JSON array of scripted read-only actions")
    parser.add_argument("--llm", action="store_true", help="use the bounded LLM action policy")
    parser.add_argument("--authorize-live-model", action="store_true")
    parser.add_argument("--model", default=None, help="override SRE_LLM_MODEL")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--max-model-calls", type=int, default=6)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--max-no-progress-rounds", type=int, default=2)


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

    investigate_cmd = sub.add_parser(
        "investigate", help="run the bounded read-only investigation graph"
    )
    investigate_cmd.add_argument("scenario", help="scenario id, e.g. Scenario-4")
    investigate_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    _add_output_flags(investigate_cmd)
    _add_investigation_flags(investigate_cmd)
    investigate_cmd.set_defaults(handler=cmd_investigate)

    eval_cmd = sub.add_parser("eval", help="predict, seal, and grade a split")
    eval_cmd.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    eval_cmd.add_argument("--out", type=Path, required=True)
    eval_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    eval_cmd.add_argument("--confirm-test", action="store_true")
    _add_llm_flags(eval_cmd)
    eval_cmd.set_defaults(handler=cmd_eval)

    investigation_eval_cmd = sub.add_parser(
        "investigation-eval",
        help="predict and seal a bounded investigation evaluation without labels",
    )
    investigation_eval_cmd.add_argument("--split", choices=["dev", "test"], default="dev")
    investigation_eval_cmd.add_argument("--out", type=Path, required=True)
    investigation_eval_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    investigation_eval_cmd.add_argument("--confirm-test", action="store_true")
    investigation_eval_cmd.add_argument("--max-turns", type=int, default=6)
    investigation_eval_cmd.add_argument("--max-model-calls", type=int, default=6)
    investigation_eval_cmd.add_argument("--max-tool-calls", type=int, default=8)
    investigation_eval_cmd.set_defaults(handler=cmd_investigation_eval)

    grade_investigation_eval_cmd = sub.add_parser(
        "grade-investigation-eval", help="grade a sealed bounded investigation evaluation"
    )
    grade_investigation_eval_cmd.add_argument("--out", type=Path, required=True)
    grade_investigation_eval_cmd.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    grade_investigation_eval_cmd.set_defaults(handler=cmd_grade_investigation_eval)

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

    hypothesis_cmd = sub.add_parser(
        "hypothesis-report",
        help="summarize stored candidate-to-hypothesis grouping diagnostics",
    )
    hypothesis_cmd.add_argument("--run", type=Path, required=True)
    hypothesis_cmd.add_argument("--json", action="store_true")
    hypothesis_cmd.set_defaults(handler=cmd_hypothesis_report)

    audit_cmd = sub.add_parser(
        "resolution-audit",
        help="write bounded audits for structurally similar resolution alternatives",
    )
    audit_cmd.add_argument("--run", type=Path, required=True)
    audit_cmd.add_argument("--out", type=Path, required=True)
    audit_cmd.add_argument("--json", action="store_true")
    audit_cmd.set_defaults(handler=cmd_resolution_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
