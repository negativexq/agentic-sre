"""Frozen paired DEV10 execution-input acquisition measurement; no labels or live providers."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from packages.evals.investigation_metrics import derive_investigation_metrics
from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION, ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.rca.engine import RCA_ENGINE_VERSION, build_case
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.state import (
    SEED_BOUNDED_INITIAL_VIEW,
    SEED_FULL_SOURCE,
    InvestigationConfig,
)
from packages.rca.model import InvestigationExecutionStatus, InvestigationResult
from scripts.measure_evidence_directed_research import (
    DEV10,
    aggregate,
    compare,
    research_metrics,
    write_json,
)

ARMS = ("execution_on", "execution_off")


def execution_metrics(result: InvestigationResult) -> dict[str, Any]:
    executed = tuple(
        audit
        for audit in result.action_audits
        if audit.backend_execution_status is not InvestigationExecutionStatus.NOT_EXECUTED
    )
    selected = tuple(
        audit
        for audit in executed
        if audit.research_question
        and any(
            target.input_scope == "RULE_INPUT"
            for read in audit.research_question.reads
            for target in read.evidence_targets
        )
    )
    initial = result.initial_diagnosis
    initial_records = {
        (audit.hypothesis_id, support.rule_id, support.rule_version): support.status
        for audit in (
            initial.resolution_trace.hypothesis_audits
            if initial and initial.resolution_trace
            else ()
        )
        for support in audit.root_support
    }
    final_records = {
        (audit.hypothesis_id, support.rule_id, support.rule_version): support.status
        for audit in (
            result.diagnosis.resolution_trace.hypothesis_audits
            if result.diagnosis.resolution_trace
            else ()
        )
        for support in audit.root_support
    }
    return {
        "turns_offering_rule_input": sum(
            any(
                target.input_scope == "RULE_INPUT"
                for candidate in audit.selection_candidates
                for target in candidate.evidence_targets
            )
            for audit in executed
        ),
        "rule_input_reads": len(selected),
        "rule_input_reads_new_evidence": sum(bool(audit.new_evidence_refs) for audit in selected),
        "rule_input_reads_new_findings": sum(
            bool(audit.normalized_finding_ids) for audit in selected
        ),
        "rule_input_reads_no_data": sum(
            audit.observation_outcome is not None and audit.observation_outcome.value == "NO_DATA"
            for audit in selected
        ),
        "selected_input_contracts": dict(
            Counter(
                target.contract_id
                for audit in selected
                if audit.research_question is not None
                for read in audit.research_question.reads
                for target in read.evidence_targets
                if target.input_scope == "RULE_INPUT"
            )
        ),
        "matched_root_rule_transitions_to_fired": sum(
            status.value == "FIRED" and initial_records[key].value != "FIRED"
            for key, status in final_records.items()
            if key in initial_records
        ),
        "remaining_acquisition_classes": dict(
            Counter(item.acquisition_class for item in result.recorded_evidence_deficits)
        ),
        "remaining_rule_reasons": dict(
            Counter(
                f"{item.rule_id}:{item.missing_precondition}"
                for item in result.recorded_evidence_deficits
            )
        ),
        "input_obtained_is_rule_satisfaction": False,
        "exact_input_receipt": "NOT_GRADED; new references/findings are acquisition proxies, not proof of the complete required predicate",
    }


def without_execution_goals(case: Any, diagnosis: Any, capabilities: Any) -> Any:
    return diagnosis.model_copy(
        update={
            "information_gaps": tuple(
                gap for gap in diagnosis.information_gaps if gap.execution_input_target is None
            )
        }
    )


def measure(root: Path, output: Path) -> None:
    if RCA_ENGINE_VERSION != "2.23.0":
        raise ValueError("This frozen execution-input measurement requires engine 2.23.0.")
    if output.exists():
        raise ValueError("Fresh output directory required")
    dataset = ITBenchLiteDataset.open(root)
    config = InvestigationConfig()
    runner = Path(__file__).read_bytes()
    protocol = {
        "created_at": datetime.now(UTC).isoformat(),
        "engine_version": RCA_ENGINE_VERSION,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "runner_sha256": hashlib.sha256(runner).hexdigest(),
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "dataset_manifest_sha256": hashlib.sha256(dataset.manifest_path.read_bytes()).hexdigest(),
        "scenario_ids": DEV10,
        "seed_modes": (SEED_FULL_SOURCE, SEED_BOUNDED_INITIAL_VIEW),
        "arms": ARMS,
        "control": "Same 2.23.0 kernel; only execution goal projection is disabled in this process. Frontier contracts stay enabled in both arms. This is not historical 2.22.0 output.",
        "policy": "DeterministicIntentPolicy",
        "config": asdict(config),
        "ordering": "One pair per scenario/seed, alternating arm order; shared source parsing warmed once per scenario.",
        "primary": "Activation, selected rule-input reads, new evidence/findings, matched rule/frontier transitions, NO_DATA reads and local cost. Exact predicate receipt is not graded.",
        "non_claims": "No root-cause accuracy/false-resolution grading, production latency or billing estimate. Input acquisition is not condition satisfaction. No tuning from these results.",
        "no_labels_test_or_holdout": True,
    }
    write_json(output / "protocol.json", protocol)
    (output / "runner.py").write_bytes(runner)
    rows: list[dict[str, Any]] = []
    comparisons = []
    for index, scenario_id in enumerate(DEV10):
        source = SnapshotSource(dataset.scenario(scenario_id))
        start = time.perf_counter()
        full_case = build_case(source)
        warmup = time.perf_counter() - start
        print(json.dumps({"scenario": scenario_id, "warmup_seconds": warmup}), flush=True)
        for seed_index, seed in enumerate((SEED_FULL_SOURCE, SEED_BOUNDED_INITIAL_VIEW)):
            results: dict[str, InvestigationResult] = {}
            arms = ARMS if (index + seed_index) % 2 == 0 else tuple(reversed(ARMS))
            for arm in arms:
                tick = time.perf_counter()
                with ExitStack() as stack:
                    if arm == "execution_off":
                        stack.enter_context(
                            patch(
                                "packages.rca.investigation.execution_inputs.project_execution_input_gaps",
                                side_effect=without_execution_goals,
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
                row: dict[str, Any] = {
                    "scenario_id": scenario_id,
                    "seed_mode": seed,
                    "arm": arm,
                    "seconds": seconds,
                    "input_warmup_seconds": warmup,
                    "initial_resolution": result.initial_resolution.value,
                    "final_resolution": result.final_resolution.value,
                    "stop_reason": result.stop_reason.value,
                    "runtime": derive_investigation_metrics(result).model_dump(mode="json"),
                    "research": research_metrics(result),
                    "execution_inputs": execution_metrics(result),
                }
                rows.append(row)
                write_json(
                    output / seed / scenario_id / f"{arm}.json", result.model_dump(mode="json")
                )
                write_json(output / seed / scenario_id / f"{arm}-metrics.json", row)
                print(
                    json.dumps(
                        {
                            "scenario": scenario_id,
                            "seed": seed,
                            "arm": arm,
                            "seconds": seconds,
                            "calls": result.tool_calls,
                            "resolution": result.final_resolution.value,
                            "rule_input_reads": row["execution_inputs"]["rule_input_reads"],
                        }
                    ),
                    flush=True,
                )
            comparisons.append(
                {
                    "scenario_id": scenario_id,
                    "seed_mode": seed,
                    **compare(results["execution_on"], results["execution_off"]),
                }
            )
            write_json(output / "progress.json", {"per_run": rows, "comparisons": comparisons})
    groups: dict[str, dict[str, Any]] = {}
    for seed in (SEED_FULL_SOURCE, SEED_BOUNDED_INITIAL_VIEW):
        groups[seed] = {}
        for arm in ARMS:
            subset = [row for row in rows if row["seed_mode"] == seed and row["arm"] == arm]
            totals = aggregate(subset)
            for key in (
                "turns_offering_rule_input",
                "rule_input_reads",
                "rule_input_reads_new_evidence",
                "rule_input_reads_new_findings",
                "rule_input_reads_no_data",
                "matched_root_rule_transitions_to_fired",
            ):
                totals[key] = sum(row["execution_inputs"][key] for row in subset)
            for key in (
                "remaining_acquisition_classes",
                "selected_input_contracts",
                "remaining_rule_reasons",
            ):
                counts: Counter[str] = Counter()
                for row in subset:
                    counts.update(row["execution_inputs"][key])
                totals[key] = dict(counts)
            groups[seed][arm] = totals
    write_json(
        output / "summary.json",
        {"protocol": protocol, "groups": groups, "comparisons": comparisons, "per_run": rows},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    measure(args.root, args.output)


if __name__ == "__main__":
    main()
