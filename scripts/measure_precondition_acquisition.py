"""Frozen paired DEV10 acquisition measurement; no labels or live providers."""

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
from packages.rca.model import InvestigationExecutionStatus, InvestigationResult, ObjectVersion
from packages.rca.source import ObservationSource
from scripts.measure_evidence_directed_research import (
    DEV10,
    aggregate,
    compare,
    research_metrics,
    write_json,
)

ARMS = ("registry_on", "registry_off")


def precondition_metrics(result: InvestigationResult) -> dict[str, Any]:
    executed = tuple(
        audit
        for audit in result.action_audits
        if audit.backend_execution_status is not InvestigationExecutionStatus.NOT_EXECUTED
    )
    direct = tuple(
        audit
        for audit in executed
        if audit.research_question
        and any(
            target.acquisition_class == "DIRECT"
            for read in audit.research_question.reads
            for target in read.evidence_targets
        )
    )
    offered = sum(
        any(
            target.acquisition_class == "DIRECT"
            for candidate in audit.selection_candidates
            for target in candidate.evidence_targets
        )
        for audit in executed
    )
    observations = {item.observation_id: item for item in result.observations}
    obtained = 0
    obtained_new = 0
    onset = result.initial_diagnosis.symptoms.onset if result.initial_diagnosis else None
    for audit in direct:
        observation = observations.get(audit.observation_id or "")
        if observation is None or onset is None:
            continue
        # Only the frozen first DIRECT contract supplies an object listing.
        raw = observation.payload.get("versions", ())
        if not isinstance(raw, (tuple, list)):
            continue
        versions = tuple(ObjectVersion.model_validate(item) for item in raw)
        eligible = tuple(
            version
            for version in versions
            if version.entity == audit.action.target and version.observed_at <= onset
        )
        obtained += bool(eligible)
        obtained_new += any(version.evidence_id in audit.new_evidence_refs for version in eligible)
    return {
        "turns_offering_direct_input": offered,
        "direct_input_reads": len(direct),
        "direct_listing_input_obtained": obtained,
        "direct_listing_new_input_obtained": obtained_new,
        "direct_reads_no_data": sum(
            audit.observation_outcome is not None and audit.observation_outcome.value == "NO_DATA"
            for audit in direct
        ),
        "remaining_acquisition_classes": dict(
            Counter(item.acquisition_class for item in result.recorded_evidence_deficits)
        ),
        "remaining_deficit_routing": dict(
            Counter(item.routing for item in result.recorded_evidence_deficits)
        ),
        "remaining_rule_reasons": dict(
            Counter(
                f"{item.rule_id}:{item.missing_precondition}"
                for item in result.recorded_evidence_deficits
            )
        ),
        "input_obtained_is_rule_satisfaction": False,
    }


def measure(root: Path, output: Path) -> None:
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
        "control": "Same 2.22.0 RCA kernel; registry functions disabled only in this process. Control acquisition is 2.21.0-compatible; complete historical output is not reproduced.",
        "policy": "DeterministicIntentPolicy",
        "config": asdict(config),
        "ordering": "One pair per scenario/seed, alternating arm order; shared source parsing warmed once per scenario.",
        "primary": "Activation, selected direct reads, exact listing input obtained, new evidence/findings, matched frontier answers, known-only/NO_DATA reads and local cost.",
        "non_claims": "No root-cause accuracy/false-resolution grading, production latency or billing estimate. Input acquisition is not condition satisfaction. No tuning from these results.",
        "no_labels_test_or_holdout": True,
    }
    write_json(output / "protocol.json", protocol)
    (output / "runner.py").write_bytes(runner)
    applicability = []
    for scenario_id in DEV10:
        source: ObservationSource = SnapshotSource(dataset.scenario(scenario_id))
        applicability.append(
            {
                "scenario_id": scenario_id,
                "object_coverage_recorded": source.evidence_coverage_record() is not None,
                "trace_reads_recorded": source.trace_read_record() is not None,
            }
        )
    write_json(output / "applicability.json", applicability)
    if not any(
        row["object_coverage_recorded"] or row["trace_reads_recorded"] for row in applicability
    ):
        write_json(
            output / "summary.json",
            {
                "protocol": protocol,
                "status": "INAPPLICABLE_CAPTURE_CHANNELS",
                "planned_runs": len(DEV10) * 4,
                "executed_runs": 0,
                "applicability": applicability,
                "reason": "All registered frontier input routes require object coverage or captured trace reads. SnapshotSource supplies neither; bounded views and overlays preserve that boundary.",
                "groups": {},
                "comparisons": [],
                "per_run": [],
            },
        )
        print("INAPPLICABLE_CAPTURE_CHANNELS: no outcome runs scheduled", flush=True)
        return
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
                    if arm == "registry_off":
                        stack.enter_context(
                            patch(
                                "packages.rca.investigation.preconditions.candidate_precondition_targets",
                                return_value=(),
                            )
                        )
                        stack.enter_context(
                            patch(
                                "packages.rca.investigation.preconditions.frontier_input_contract",
                                return_value=None,
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
                    "preconditions": precondition_metrics(result),
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
                            "direct_reads": row["preconditions"]["direct_input_reads"],
                        }
                    ),
                    flush=True,
                )
            comparisons.append(
                {
                    "scenario_id": scenario_id,
                    "seed_mode": seed,
                    **compare(results["registry_on"], results["registry_off"]),
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
                "turns_offering_direct_input",
                "direct_input_reads",
                "direct_listing_input_obtained",
                "direct_listing_new_input_obtained",
                "direct_reads_no_data",
            ):
                totals[key] = sum(row["preconditions"][key] for row in subset)
            for key in (
                "remaining_acquisition_classes",
                "remaining_deficit_routing",
                "remaining_rule_reasons",
            ):
                counts: Counter[str] = Counter()
                for row in subset:
                    counts.update(row["preconditions"][key])
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
