"""Paired, prediction-only DEV10 research instrumentation measurement.

No labels, HOLDOUT, live providers or LLM calls. Metadata-off is a measurement
control in this process, not a product switch or an alternate resolver.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from packages.evals.investigation_metrics import derive_investigation_metrics
from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION, ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.evals.resolution_blockers import analyze_resolution_blockers
from packages.rca.engine import RCA_ENGINE_VERSION, build_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.frontier import ANSWERED_STATES
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.state import (
    SEED_BOUNDED_INITIAL_VIEW,
    SEED_FULL_SOURCE,
    InvestigationConfig,
)
from packages.rca.model import InvestigationExecutionStatus, InvestigationResult

DEV10 = (
    "Scenario-4",
    "Scenario-7",
    "Scenario-8",
    "Scenario-11",
    "Scenario-12",
    "Scenario-17",
    "Scenario-21",
    "Scenario-29",
    "Scenario-34",
    "Scenario-91",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def research_metrics(result: InvestigationResult) -> dict[str, Any]:
    """Observed research proxies, never inferred question closure or correctness."""
    executed = tuple(
        audit
        for audit in result.action_audits
        if audit.backend_execution_status is not InvestigationExecutionStatus.NOT_EXECUTED
    )
    questions = tuple(audit for audit in executed if audit.research_question is not None)
    initial_answers = {
        answer.alternative_id: answer
        for answer in (
            result.initial_diagnosis.resolution_trace.frontier_answers
            if result.initial_diagnosis and result.initial_diagnosis.resolution_trace
            else ()
        )
    }
    final_answers = (
        result.diagnosis.resolution_trace.frontier_answers
        if result.diagnosis.resolution_trace
        else ()
    )
    newly_answered = tuple(
        answer
        for answer in final_answers
        if answer.state in ANSWERED_STATES
        and answer.alternative_id in initial_answers
        and initial_answers[answer.alternative_id].state not in ANSWERED_STATES
    )
    return {
        "executed_reads": len(executed),
        "reads_with_research_question": len(questions),
        "distinct_asked_gap_ids": len({audit.action.gap_id for audit in executed}),
        "reads_with_new_evidence": sum(bool(audit.new_evidence_refs) for audit in executed),
        "reads_with_normalized_findings": sum(
            bool(audit.normalized_finding_ids) for audit in executed
        ),
        "reads_with_decision_change": sum(
            audit.decision_state_changed is True for audit in executed
        ),
        "reads_supporting": sum(
            audit.observation_outcome is not None and audit.observation_outcome.value == "SUPPORTS"
            for audit in executed
        ),
        "reads_contradicting": sum(
            audit.observation_outcome is not None
            and audit.observation_outcome.value == "CONTRADICTS"
            for audit in executed
        ),
        "reads_no_data": sum(
            audit.observation_outcome is not None and audit.observation_outcome.value == "NO_DATA"
            for audit in executed
        ),
        "reads_unknown": sum(
            audit.observation_outcome is not None and audit.observation_outcome.value == "UNKNOWN"
            for audit in executed
        ),
        "asked_dimensions": dict(
            Counter(audit.gap_dimension.value for audit in executed if audit.gap_dimension)
        ),
        "remaining_gap_count": len(result.diagnosis.information_gaps),
        "initial_open_frontier_questions_answered": len(newly_answered),
        "frontier_answer_rules": dict(Counter(answer.rule_id for answer in newly_answered)),
        "remaining_question_count": len(result.remaining_research_questions),
        "remaining_availability": dict(
            Counter(question.availability for question in result.remaining_research_questions)
        ),
        "remaining_dimensions": dict(
            Counter(question.dimension.value for question in result.remaining_research_questions)
        ),
        "observation_document_bytes": sum(
            len(item.model_dump_json().encode()) for item in result.observations
        ),
        "result_document_bytes": len(result.model_dump_json().encode()),
        "question_answered_count": None,
        "question_answered_count_reason": "No authority to infer that a finding, decision change or disappearing gap completely answers a scoped question.",
    }


def compare(left: InvestigationResult, right: InvestigationResult) -> dict[str, bool]:
    return {
        "same_decision_digest": diagnosis_epistemic_digest(left.diagnosis)
        == diagnosis_epistemic_digest(right.diagnosis),
        "same_actions": tuple(audit.action for audit in left.action_audits)
        == tuple(audit.action for audit in right.action_audits),
        "same_observations": left.observations == right.observations,
        "same_ledger": left.ledger == right.ledger,
        "same_tool_calls": left.tool_calls == right.tool_calls,
        "same_stop_reason": left.stop_reason == right.stop_reason,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    numeric_keys = (
        "executed_reads",
        "reads_with_research_question",
        "distinct_asked_gap_ids",
        "reads_with_new_evidence",
        "reads_with_normalized_findings",
        "reads_with_decision_change",
        "reads_supporting",
        "reads_contradicting",
        "reads_no_data",
        "reads_unknown",
        "remaining_gap_count",
        "initial_open_frontier_questions_answered",
        "remaining_question_count",
        "result_document_bytes",
        "observation_document_bytes",
    )
    result: dict[str, Any] = {
        key: sum(row["research"][key] for row in rows) for key in numeric_keys
    }
    for key in (
        "remaining_availability",
        "remaining_dimensions",
        "asked_dimensions",
        "frontier_answer_rules",
    ):
        counts: Counter[str] = Counter()
        for row in rows:
            counts.update(row["research"][key])
        result[key] = dict(counts)
    result.update(
        {
            "runs": len(rows),
            "seconds": sum(row["seconds"] for row in rows),
            "model_calls": sum(row["runtime"]["model_calls"] for row in rows),
            "tool_calls": sum(row["runtime"]["tool_calls"] for row in rows),
            "duplicate_reads": sum(row["runtime"]["duplicate_read_count"] for row in rows),
            "useful_calls_m14": sum(row["runtime"]["useful_call_count"] for row in rows),
            "rejected_actions": sum(row["runtime"]["invalid_actions"] for row in rows),
            "initial_resolutions": dict(Counter(row["initial_resolution"] for row in rows)),
            "final_resolutions": dict(Counter(row["final_resolution"] for row in rows)),
            "stop_reasons": dict(Counter(row["stop_reason"] for row in rows)),
            "question_answered_count": None,
        }
    )
    return result


def measure(root: Path, output: Path) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Fresh output directory required")
    dataset = ITBenchLiteDataset.open(root)
    config = InvestigationConfig()
    protocol = {
        "created_at": datetime.now(UTC).isoformat(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "engine_version": RCA_ENGINE_VERSION,
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "dataset_manifest_sha256": hashlib.sha256(dataset.manifest_path.read_bytes()).hexdigest(),
        "scenario_ids": DEV10,
        "seed_modes": (SEED_FULL_SOURCE, SEED_BOUNDED_INITIAL_VIEW),
        "policy": "DeterministicIntentPolicy",
        "config": {
            "max_turns": config.max_turns,
            "max_tool_calls": config.max_tool_calls,
            "max_tool_calls_per_gap": config.max_tool_calls_per_gap,
            "max_wall_time_seconds": config.max_wall_time_seconds,
        },
        "arms": ("metadata_on", "metadata_off"),
        "ordering": "Alternating paired order per scenario and seed; one pair, descriptive warm-source wall time only.",
        "primary": "Recorded research coverage and acquisition outcomes, not RESOLVED count.",
        "no_ground_truth_or_holdout": True,
        "question_closure": "Not inferred from gap disappearance, new findings or decision changes.",
        "cost": "Local tool attempts, serialized observation/result bytes and wall time; bytes are not network traffic or billing.",
    }
    write_json(output / "protocol.json", protocol)
    rows: list[dict[str, Any]] = []
    comparisons = []
    for index, scenario_id in enumerate(DEV10):
        source = SnapshotSource(dataset.scenario(scenario_id))
        start = time.perf_counter()
        full_case = build_case(source)  # Warm input parsing; not charged to either arm.
        load_seconds = time.perf_counter() - start
        for seed_index, seed in enumerate((SEED_FULL_SOURCE, SEED_BOUNDED_INITIAL_VIEW)):
            arms = (
                ("metadata_on", "metadata_off")
                if (index + seed_index) % 2 == 0
                else ("metadata_off", "metadata_on")
            )
            results: dict[str, InvestigationResult] = {}
            for arm in arms:
                tick = time.perf_counter()
                with ExitStack() as stack:
                    if arm == "metadata_off":
                        stack.enter_context(
                            patch(
                                "packages.rca.investigation.graph.research_question",
                                return_value=None,
                            )
                        )
                        stack.enter_context(
                            patch(
                                "packages.rca.investigation.graph.research_questions",
                                return_value=(),
                            )
                        )
                    result = investigate_diagnosis(
                        source,
                        policy=DeterministicIntentPolicy(),
                        config=InvestigationConfig(seed_mode=seed),
                        initial_case=full_case if seed == SEED_FULL_SOURCE else None,
                    )
                seconds = time.perf_counter() - tick
                results[arm] = result
                row = {
                    "scenario_id": scenario_id,
                    "seed_mode": seed,
                    "arm": arm,
                    "seconds": seconds,
                    "input_warmup_seconds": load_seconds,
                    "initial_resolution": result.initial_resolution.value,
                    "final_resolution": result.final_resolution.value,
                    "root_cause": result.diagnosis.root_cause.canonical
                    if result.diagnosis.root_cause
                    else None,
                    "stop_reason": result.stop_reason.value,
                    "runtime": derive_investigation_metrics(result).model_dump(mode="json"),
                    "research": research_metrics(result),
                }
                rows.append(row)
                write_json(
                    output / seed / scenario_id / f"{arm}.json", result.model_dump(mode="json")
                )
                write_json(output / seed / scenario_id / f"{arm}-metrics.json", row)
            comparison = {
                "scenario_id": scenario_id,
                "seed_mode": seed,
                **compare(results["metadata_on"], results["metadata_off"]),
            }
            comparisons.append(comparison)
            print(
                json.dumps(
                    {
                        **comparison,
                        "tool_calls": results["metadata_on"].tool_calls,
                        "resolution": results["metadata_on"].final_resolution.value,
                    }
                ),
                flush=True,
            )
    summary = {
        "protocol": protocol,
        "groups": {
            seed: {
                arm: aggregate(
                    [row for row in rows if row["seed_mode"] == seed and row["arm"] == arm]
                )
                for arm in ("metadata_on", "metadata_off")
            }
            for seed in (SEED_FULL_SOURCE, SEED_BOUNDED_INITIAL_VIEW)
        },
        "comparisons": comparisons,
        "all_paired_invariants_pass": all(
            all(value for key, value in comparison.items() if key.startswith("same_"))
            for comparison in comparisons
        ),
        "per_run": rows,
    }
    write_json(output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="Derive acquisition counts independently of whether question metadata was recorded; preserve all raw run artifacts.",
    )
    args = parser.parse_args()
    if not args.reanalyze:
        measure(args.root, args.output)
    summary = reanalyze(args.output)
    print(
        json.dumps(
            {
                "groups": summary["groups"],
                "all_paired_invariants_pass": summary["all_paired_invariants_pass"],
            },
            indent=2,
        )
    )


def reanalyze(output: Path) -> dict[str, Any]:
    """Corrected acquisition accounting over raw runs; no engine or source rerun.

    The pilot runner counted acquisition outcomes only on question-tagged reads.
    Metadata-off controls must also retain their actual acquisition outcomes.
    Raw predictions, timings, protocol and pilot summary remain untouched.
    """
    raw_path = output / "summary.json"
    summary: dict[str, Any] = json.loads(raw_path.read_text())
    rows = []
    comparisons = []
    pending: dict[tuple[str, str], dict[str, InvestigationResult]] = {}
    record_checks = []
    condition_counts: dict[str, Counter[tuple[str, str]]] = {}
    for recorded in summary["per_run"]:
        row = dict(recorded)
        artifact = output / row["seed_mode"] / row["scenario_id"] / f"{row['arm']}.json"
        result = InvestigationResult.model_validate_json(artifact.read_text())
        row["research"] = research_metrics(result)
        rows.append(row)
        key = (row["seed_mode"], row["scenario_id"])
        paired = pending.setdefault(key, {})
        paired[row["arm"]] = result
        if len(paired) == 2:
            comparisons.append(
                {
                    "scenario_id": key[1],
                    "seed_mode": key[0],
                    **compare(paired["metadata_on"], paired["metadata_off"]),
                }
            )
            del pending[key]
        if row["arm"] == "metadata_on":
            executed = tuple(
                audit
                for audit in result.action_audits
                if audit.backend_execution_status is not InvestigationExecutionStatus.NOT_EXECUTED
            )
            binding_failures = sum(
                audit.research_question is None
                or audit.research_question.gap_id != audit.action.gap_id
                or not any(
                    read.capability == audit.action.capability
                    and read.target == audit.action.target
                    and read.query == audit.action.query
                    for read in audit.research_question.reads
                )
                for audit in executed
            )
            record_checks.append(
                {
                    "seed_mode": key[0],
                    "scenario_id": key[1],
                    "executed_read_count": len(executed),
                    "selected_read_binding_failures": binding_failures,
                    "all_current_gaps_recorded": {
                        gap.gap_id for gap in result.diagnosis.information_gaps
                    }
                    == {q.gap_id for q in result.remaining_research_questions},
                }
            )
            report = analyze_resolution_blockers(
                result.diagnosis.model_dump(mode="json"), engine_version=RCA_ENGINE_VERSION
            )
            counts = condition_counts.setdefault(key[0], Counter())
            counts.update(
                (condition.condition, condition.status) for condition in report.conditions
            )
            write_json(
                output / "blocker-projections" / key[0] / f"{key[1]}.json",
                report.model_dump(mode="json"),
            )
    if pending:
        raise ValueError("Unpaired run artifacts in raw summary")
    summary["per_run"] = rows
    summary["comparisons"] = comparisons
    summary["all_paired_invariants_pass"] = all(
        all(value for key, value in pair.items() if key.startswith("same_")) for pair in comparisons
    )
    summary["research_record_checks"] = record_checks
    summary["blocker_condition_distribution"] = {
        seed: [
            {"condition": key[0], "status": key[1], "count": value}
            for key, value in sorted(counts.items())
        ]
        for seed, counts in condition_counts.items()
    }
    summary["groups"] = {
        seed: {
            arm: aggregate([row for row in rows if row["seed_mode"] == seed and row["arm"] == arm])
            for arm in ("metadata_on", "metadata_off")
        }
        for seed in (SEED_FULL_SOURCE, SEED_BOUNDED_INITIAL_VIEW)
    }
    summary["analysis_version"] = "research-acquisition.v2"
    summary["raw_summary_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    summary["accounting_note"] = (
        "Acquisition outcomes count every executed read in both arms, independently of research-question annotations. No prediction, timing or comparison changed."
    )
    write_json(output / "analysis.json", summary)
    return summary


if __name__ == "__main__":
    main()
